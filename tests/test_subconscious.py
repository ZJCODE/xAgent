"""Unit tests for optional diary notes and impulse enqueue."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from xagent.core.agent import Agent
from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.core.handlers.model import ChatToolCall, ModelStreamEvent
from xagent.core.runtime.subconscious import Impulse, SubconsciousLoop
from xagent.schemas import Message, RoleType


class AgentSubconsciousThoughtTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_subconscious_thought_appends_diary_without_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(client=object(), workspace=tmpdir)

            result = await agent.record_subconscious_thought("  raw inner thought  ")

            entries = await agent.markdown_memory.read_recent_dailies(days=1)
            self.assertEqual(result.kind, "subconscious_thought")
            self.assertEqual(result.event_type, "subconscious_thought")
            self.assertEqual(await agent.message_storage.get_latest_message_cursor(), 0)
            self.assertEqual(len(entries), 1)
            self.assertIn("raw inner thought", entries[0][1])


class SubconsciousLoopTests(unittest.TestCase):
    class _ModelClientStub:
        def __init__(self, event_batches):
            self.event_batches = list(event_batches)
            self.calls = []

        async def model_turn_events(self, **kwargs):
            self.calls.append(kwargs)
            if self.event_batches:
                events = self.event_batches.pop(0)
            else:
                events = []
            for event in events:
                yield event

    @staticmethod
    def _json_event(payload: dict) -> ModelStreamEvent:
        return ModelStreamEvent(type="text", delta=json.dumps(payload))

    def _set_model_events(self, agent: MagicMock, event_batches) -> None:
        agent.model_client = self._ModelClientStub(event_batches)

    def _set_model_json(self, agent: MagicMock, payload: dict) -> None:
        self._set_model_events(agent, [[self._json_event(payload)]])

    def _make_agent_mock(self) -> MagicMock:
        agent = MagicMock()
        agent.system_prompt = "You are a helpful assistant."
        agent.supports_vision = True
        agent.max_history = AgentConfig.DEFAULT_MAX_HISTORY
        agent.max_iter = AgentConfig.DEFAULT_MAX_ITER
        agent.max_concurrent_tools = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
        agent._assistant_sender_id = "agent"
        memory_handler = MagicMock()
        memory_handler.get_subconscious_context.return_value = "Recent memory content."
        memory_handler.append_durable_diary = AsyncMock(return_value=True)
        memory_handler.get_relationship_context = AsyncMock(return_value="")
        memory_handler.relationship_store.list_keys = AsyncMock(return_value=[])
        agent.memory_handler = memory_handler
        message_handler = MessageHandler(MagicMock(), system_prompt=agent.system_prompt)
        message_handler.get_recent_messages = AsyncMock(return_value=[])
        message_handler.store_context_event = AsyncMock()
        agent.message_handler = message_handler
        agent.enqueue_initiative = AsyncMock(return_value=True)
        agent.recipient_directory = MagicMock()
        agent.recipient_directory.list_routes.return_value = []
        self._set_model_json(agent, {"diary_entry": None, "impulse": None})
        tool_manager = MagicMock()
        tool_manager._tools = {"web_search": MagicMock()}
        tool_manager.cached_tool_specs = [{"type": "function", "function": {"name": "web_search"}}]
        agent.tool_manager = tool_manager
        agent.tool_executor = MagicMock()
        agent.tool_executor.handle_tool_calls = AsyncMock(return_value=None)
        agent._workspace_context = MagicMock(return_value=AgentConfig.build_workspace_context("/tmp/workspace"))
        agent._skills_catalog_context = MagicMock(return_value="Available skill: test")
        return agent

    def test_should_trigger_disabled(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._enabled = False
            for _ in range(100):
                self.assertFalse(loop.should_trigger())

    def test_should_trigger_probability(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            for _ in range(20):
                self.assertTrue(loop.should_trigger())
            loop._probability = 0.0
            for _ in range(20):
                self.assertFalse(loop.should_trigger())

    def test_parse_subconscious_json_plain(self):
        result = SubconsciousLoop._parse_subconscious_json(
            json.dumps({
                "diary_entry": "A durable observation.",
                "impulse": {"recipient_key": "api:web_user", "intent": "check in"},
            })
        )
        self.assertEqual(result["diary_entry"], "A durable observation.")
        self.assertEqual(result["impulse"].recipient_key, "api:web_user")
        self.assertEqual(result["impulse"].intent, "check in")

    def test_parse_subconscious_json_with_code_fence(self):
        result = SubconsciousLoop._parse_subconscious_json(
            "```json\n" + json.dumps({"diary_entry": None, "impulse": None}) + "\n```"
        )
        self.assertIsNone(result["diary_entry"])
        self.assertIsNone(result["impulse"])

    def test_invalid_json_is_silence(self):
        result = SubconsciousLoop._parse_subconscious_json("Just a random string")
        self.assertIsNone(result["diary_entry"])
        self.assertIsNone(result["impulse"])

    def test_non_dict_json_is_silence(self):
        result = SubconsciousLoop._parse_subconscious_json('["not", "a dict"]')
        self.assertIsNone(result["diary_entry"])
        self.assertIsNone(result["impulse"])

    def test_impulse_rejects_final_outgoing_copy(self):
        result = SubconsciousLoop._parse_subconscious_json(
            json.dumps({
                "diary_entry": None,
                "impulse": {
                    "recipient_key": "feishu:ou_1",
                    "intent": "Hello there.",
                    "external_content": "Hello there.",
                },
            })
        )
        self.assertIsNone(result["impulse"])

    def test_impulse_requires_channel_key(self):
        result = SubconsciousLoop._parse_impulse({"recipient_key": "Alice", "intent": "say hi"})
        self.assertIsNone(result)

    def test_recent_messages_injected_into_prompt(self):
        agent = self._make_agent_mock()
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[
            Message(role=RoleType.USER, sender_id="alice", content="你好，今天心情怎么样？", timestamp=1716000000.0),
            Message(role=RoleType.ASSISTANT, sender_id=None, content="挺好的！", timestamp=1716000001.0),
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            recent_experience = next(msg for msg in messages if msg.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME)
            self.assertIn("<recent_experience>", recent_experience["content"])
            self.assertIn("[speaker=alice]", recent_experience["content"])
            self.assertIn("今天心情怎么样", recent_experience["content"])
            current_task = next(msg for msg in messages if msg.get("name") == AgentConfig.CURRENT_TASK_NAME)
            self.assertIn('mode="subconscious_json"', current_task["content"])
            self.assertIn("diary_entry", current_task["content"])
            self.assertIn("impulse", current_task["content"])
            self.assertIn("recipient_key", current_task["content"])
            self.assertNotIn("external_content", current_task["content"])
            self.assertNotIn("worthy=true", current_task["content"])

    def test_subconscious_omits_tools_and_skills(self):
        agent = self._make_agent_mock()
        agent.system_prompt = "I am a test identity."
        agent.message_handler.system_prompt = agent.system_prompt
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())
            instructions = agent.model_client.calls[0]["instructions"]
            names = {i.get("name") for i in instructions}
            self.assertIn(AgentConfig.CORE_INTERACTION_RULES_NAME, names)
            self.assertIn(AgentConfig.IDENTITY_CONTEXT_NAME, names)
            self.assertNotIn(AgentConfig.TOOL_POLICY_NAME, names)
            self.assertEqual(agent.model_client.calls[0]["tool_specs"], [])

    def test_tool_call_is_not_executed(self):
        agent = self._make_agent_mock()
        tool_call = ChatToolCall(call_id="call_1", name="web_search", arguments='{"query":"x"}')
        self._set_model_events(agent, [[ModelStreamEvent(type="tool_calls", tool_calls=[tool_call])]])
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())
            agent.tool_executor.handle_tool_calls.assert_not_awaited()
            agent.memory_handler.append_durable_diary.assert_not_awaited()
            agent.enqueue_initiative.assert_not_awaited()

    def test_blank_result_is_successful_noop(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())
            agent.memory_handler.append_durable_diary.assert_not_awaited()
            agent.enqueue_initiative.assert_not_awaited()
            self.assertEqual(loop._stale_streak, 1)

    def test_invalid_json_does_not_write_diary(self):
        agent = self._make_agent_mock()
        self._set_model_events(agent, [[ModelStreamEvent(type="text", delta="not json")]])
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())
            agent.memory_handler.append_durable_diary.assert_not_awaited()
            agent.enqueue_initiative.assert_not_awaited()

    def test_diary_only_clears_habituation(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {"diary_entry": "A new durable note.", "impulse": None})
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            loop._stale_streak = 3
            asyncio.run(loop.maybe_think())
            agent.memory_handler.append_durable_diary.assert_awaited_once()
            agent.enqueue_initiative.assert_not_awaited()
            self.assertEqual(loop._stale_streak, 0)

    def test_impulse_only_clears_habituation(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "diary_entry": None,
            "impulse": {"recipient_key": "api:web_user", "intent": "say hello"},
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            loop._stale_streak = 2
            asyncio.run(loop.maybe_think())
            agent.memory_handler.append_durable_diary.assert_not_awaited()
            agent.enqueue_initiative.assert_awaited_once()
            impulse = agent.enqueue_initiative.await_args.args[0]
            self.assertIsInstance(impulse, Impulse)
            self.assertEqual(impulse.recipient_key, "api:web_user")
            self.assertEqual(loop._stale_streak, 0)

    def test_diary_failure_does_not_block_impulse(self):
        agent = self._make_agent_mock()
        agent.memory_handler.append_durable_diary = AsyncMock(return_value=False)
        self._set_model_json(agent, {
            "diary_entry": "duplicate note",
            "impulse": {"recipient_key": "feishu:ou_1", "intent": "follow up"},
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())
            agent.enqueue_initiative.assert_awaited_once()
            self.assertEqual(loop._stale_streak, 0)

    def test_impulse_failure_does_not_block_diary(self):
        agent = self._make_agent_mock()
        agent.enqueue_initiative = AsyncMock(return_value=False)
        self._set_model_json(agent, {
            "diary_entry": "keep this",
            "impulse": {"recipient_key": "feishu:ou_1", "intent": "follow up"},
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())
            agent.memory_handler.append_durable_diary.assert_awaited_once()
            self.assertEqual(loop._stale_streak, 0)

    def test_collect_relationship_context_reads_store_cards(self):
        agent = self._make_agent_mock()
        memory_handler = MagicMock()
        memory_handler.relationship_store.list_keys = AsyncMock(return_value=["feishu:alice"])
        memory_handler.get_relationship_context = AsyncMock(return_value="## Alice\nAn older open thread.")
        agent.memory_handler = memory_handler
        agent.recipient_directory = MagicMock()
        agent.recipient_directory.list_routes.return_value = []
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            context = asyncio.run(loop._collect_relationship_context())
        self.assertIn("older open thread", context)
        kwargs = memory_handler.get_relationship_context.await_args.kwargs
        self.assertEqual(kwargs["speaker_keys"], ["feishu:alice"])
        self.assertTrue(kwargs["include_routing_id"])


if __name__ == "__main__":
    unittest.main()
