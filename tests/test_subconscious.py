"""Unit tests for the subconscious thought system."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.core.handlers.model import ChatToolCall, ModelStreamEvent
from xagent.schemas import RoleType
from xagent.core.runtime.subconscious import (
    SUBCONSCIOUS_SOURCE,
    SUBCONSCIOUS_EVENT_TYPE,
    ContactEntry,
    SubconsciousLoop,
    load_contacts,
    save_contacts,
    upsert_contact,
    resolve_contacts_path,
)


class ContactManagementTests(unittest.TestCase):
    """Tests for the persistent contacts registry."""

    def test_load_contacts_empty_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contacts_file = Path(tmpdir) / "contacts.json"
            contacts = load_contacts(contacts_file)
            self.assertEqual(contacts, [])

    def test_save_and_load_contacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contacts_file = Path(tmpdir) / "contacts.json"
            contacts = [
                ContactEntry(
                    channel="feishu",
                    user_id="ou_123",
                    target={"chat_id": "oc_xxx", "sender_name": "张三"},
                    last_seen="2026-06-22 15:30:00",
                    interaction_count=5,
                ),
            ]
            save_contacts(contacts_file, contacts)
            loaded = load_contacts(contacts_file)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].channel, "feishu")
            self.assertEqual(loaded[0].user_id, "ou_123")
            self.assertEqual(loaded[0].target["chat_id"], "oc_xxx")
            self.assertEqual(loaded[0].interaction_count, 5)

    def test_upsert_contact_new(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contacts_file = Path(tmpdir) / "contacts.json"
            upsert_contact(
                contacts_file,
                channel="weixin",
                user_id="wx_456",
                target={"user_id": "wx_456"},
            )
            loaded = load_contacts(contacts_file)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].channel, "weixin")
            self.assertEqual(loaded[0].interaction_count, 1)

    def test_upsert_contact_existing_updates_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contacts_file = Path(tmpdir) / "contacts.json"
            upsert_contact(
                contacts_file,
                channel="api",
                user_id="user_1",
                target={"user_id": "user_1"},
            )
            upsert_contact(
                contacts_file,
                channel="api",
                user_id="user_1",
                target={"user_id": "user_1", "extra": "value"},
            )
            loaded = load_contacts(contacts_file)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].interaction_count, 2)
            self.assertEqual(loaded[0].target["extra"], "value")

    def test_upsert_contact_different_channels_independent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contacts_file = Path(tmpdir) / "contacts.json"
            upsert_contact(contacts_file, "feishu", "user_1", {"chat_id": "oc_1"})
            upsert_contact(contacts_file, "weixin", "user_1", {"user_id": "user_1"})
            loaded = load_contacts(contacts_file)
            self.assertEqual(len(loaded), 2)

    def test_resolve_contacts_path(self):
        workspace = Path("/tmp/test_workspace")
        result = resolve_contacts_path(workspace)
        self.assertEqual(result, workspace / "contacts.json")

class SubconsciousLoopTests(unittest.TestCase):
    """Tests for the SubconsciousLoop class."""

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
        agent.subconscious_pure_thought = AgentConfig.SUBCONSCIOUS_PURE_THOUGHT
        agent._assistant_sender_id = "agent"
        memory_handler = MagicMock()
        memory_handler.get_recent_context.return_value = "Recent memory content."
        agent.memory_handler = memory_handler
        message_handler = MessageHandler(MagicMock(), system_prompt=agent.system_prompt)
        message_handler.get_recent_messages = AsyncMock(return_value=[])
        agent.message_handler = message_handler
        self._set_model_json(agent, {
            "internal_content": "Just a thought.",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })
        tool_manager = MagicMock()
        tool_manager._tools = {
            "web_search": MagicMock(),
            "attach_artifact": MagicMock(),
        }
        tool_manager.cached_tool_specs = [{"type": "function", "function": {"name": "web_search"}}]
        agent.tool_manager = tool_manager
        agent.tool_executor = MagicMock()
        agent.tool_executor.handle_tool_calls = AsyncMock(return_value=None)
        agent._workspace_context = MagicMock(return_value=AgentConfig.build_workspace_context("/tmp/workspace"))
        agent._skills_catalog_context = MagicMock(return_value="Available skill: test")
        agent.record_internal_thought = AsyncMock()
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
                "internal_content": "Thinking about saying hello.",
                "worthy": True,
                "recipient_hint": "Alice",
                "external_content": "Hello!",
            })
        )
        self.assertTrue(result["worthy"])
        self.assertEqual(result["internal_content"], "Thinking about saying hello.")
        self.assertEqual(result["external_content"], "Hello!")

    def test_parse_subconscious_json_with_code_fence(self):
        result = SubconsciousLoop._parse_subconscious_json(
            "```json\n"
            + json.dumps({
                "internal_content": "Nah.",
                "worthy": False,
                "recipient_hint": None,
                "external_content": None,
            })
            + "\n```"
        )
        self.assertFalse(result["worthy"])
        self.assertEqual(result["internal_content"], "Nah.")
        self.assertIsNone(result["external_content"])

    def test_parse_subconscious_json_fallback(self):
        result = SubconsciousLoop._parse_subconscious_json("Just a random string")
        self.assertFalse(result.get("worthy"))
        self.assertEqual(result["internal_content"], "Just a random string")
        self.assertIsNone(result["external_content"])

    def test_parse_subconscious_json_non_dict_fallback(self):
        result = SubconsciousLoop._parse_subconscious_json('["not", "a dict"]')
        self.assertFalse(result.get("worthy"))
        self.assertEqual(result["internal_content"], "['not', 'a dict']")
        self.assertIsNone(result["external_content"])

    def test_is_appropriate_time_default_config(self):
        """Default quiet hours 22–8: 8 AM to <10 PM is appropriate."""
        with patch.object(AgentConfig, 'SUBCONSCIOUS_QUIET_HOURS_START', 22), \
             patch.object(AgentConfig, 'SUBCONSCIOUS_QUIET_HOURS_END', 8):
            self.assertFalse(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 7, 0)))
            self.assertTrue(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 8, 0)))
            self.assertTrue(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 12, 0)))
            self.assertTrue(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 21, 59)))
            self.assertFalse(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 22, 0)))
            self.assertFalse(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 23, 0)))

    def test_is_appropriate_time_simple_range(self):
        """Simple range: quiet 0–6, only midnight to 6 AM is blocked."""
        with patch.object(AgentConfig, 'SUBCONSCIOUS_QUIET_HOURS_START', 0), \
             patch.object(AgentConfig, 'SUBCONSCIOUS_QUIET_HOURS_END', 6):
            self.assertFalse(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 3, 0)))
            self.assertTrue(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 6, 0)))
            self.assertTrue(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 12, 0)))
            self.assertTrue(SubconsciousLoop._is_appropriate_time(datetime(2026, 6, 22, 23, 0)))

    def test_record_interaction(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"feishu"})
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            contacts = load_contacts(loop.contacts_file)
            self.assertEqual(len(contacts), 1)
            self.assertEqual(contacts[0].channel, "feishu")

    def test_recent_messages_injected_into_prompt(self):
        """Verify recent conversation messages are included in the LLM prompt."""
        from xagent.schemas import Message, RoleType

        agent = self._make_agent_mock()
        # Set up recent messages
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[
            Message(role=RoleType.USER, sender_id="alice", content="你好，今天心情怎么样？", timestamp=1716000000.0),
            Message(role=RoleType.ASSISTANT, sender_id=None, content="挺好的！", timestamp=1716000001.0),
        ])
        self._set_model_json(agent, {
            "internal_content": "Hmm.",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            recent_experience = next(msg for msg in messages if msg.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME)
            self.assertIn("<recent_experience>", recent_experience["content"])
            self.assertIn("[speaker=alice]", recent_experience["content"])
            self.assertIn("今天心情怎么样", recent_experience["content"])
            self.assertIn("[speaker=ME]", recent_experience["content"])
            self.assertIn("挺好的", recent_experience["content"])
            current_task = next(msg for msg in messages if msg.get("name") == AgentConfig.CURRENT_TASK_NAME)
            self.assertIn('mode="subconscious_json"', current_task["content"])
            self.assertIn("Return JSON only", current_task["content"])
            self.assertNotIn("Known delivery contacts", current_task["content"])

    def test_recent_messages_empty_uses_named_recent_experience_layer(self):
        """When there are no recent messages, the named layer remains with empty context."""
        agent = self._make_agent_mock()
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            recent_experience = next(msg for msg in messages if msg.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME)
            self.assertIn("[No recent experience]", recent_experience["content"])
            recent_memory = next(msg for msg in messages if msg.get("name") == AgentConfig.RECENT_MEMORY_NAME)
            self.assertIn("Recent memory content", recent_memory["content"])

    def test_contacts_summary_filters_undeliverable_channels(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"api"})
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop.record_interaction(
                channel="api",
                user_id="api_user",
                target={"user_id": "api_user", "sender_name": "Alice"},
            )

            summary = loop._collect_contacts_summary()

            self.assertIn("Alice", summary)
            self.assertIn("channel: api", summary)
            self.assertNotIn("张三", summary)
            self.assertNotIn("channel: feishu", summary)

    def test_contacts_summary_without_declared_channels_exposes_no_contacts(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )

            summary = loop._collect_contacts_summary()

            self.assertEqual(summary, "(no contacts available for this runtime)")

    def test_default_pure_thought_omits_tools_and_skills(self):
        """Default subconscious turns keep identity but omit tool/skill capability layers."""
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
            self.assertNotIn(AgentConfig.WORKSPACE_CONTEXT_NAME, names)
            self.assertNotIn(AgentConfig.SKILLS_CATALOG_NAME, names)

            contents = [i["content"] for i in instructions]
            self.assertTrue(any("Context and Attribution" in c for c in contents))
            self.assertFalse(any("All available tools are defined" in c for c in contents))
            self.assertEqual(agent.model_client.calls[0]["tool_specs"], [])
            agent._workspace_context.assert_not_called()
            agent._skills_catalog_context.assert_not_called()

            identities = [i for i in instructions if i.get("name") == "identity_context"]
            self.assertEqual(len(identities), 1)
            self.assertIn("I am a test identity.", identities[0]["content"])

    def test_non_pure_thought_includes_tool_and_skill_layers(self):
        """Disabling pure thought preserves the previous tool/skill-capable behavior."""
        agent = self._make_agent_mock()
        agent.subconscious_pure_thought = False
        agent.system_prompt = "I am a test identity."
        agent.message_handler.system_prompt = agent.system_prompt

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            instructions = agent.model_client.calls[0]["instructions"]

            names = {i.get("name") for i in instructions}
            self.assertIn(AgentConfig.CORE_INTERACTION_RULES_NAME, names)
            self.assertIn(AgentConfig.TOOL_POLICY_NAME, names)
            self.assertIn(AgentConfig.IDENTITY_CONTEXT_NAME, names)
            self.assertIn(AgentConfig.WORKSPACE_CONTEXT_NAME, names)
            self.assertIn(AgentConfig.SKILLS_CATALOG_NAME, names)
            self.assertEqual(
                agent.model_client.calls[0]["tool_specs"],
                [{"type": "function", "function": {"name": "web_search"}}],
            )
            agent._workspace_context.assert_called_once()
            agent._skills_catalog_context.assert_called_once()

    def test_tool_call_loop_continues_until_json(self):
        """Subconscious turns can use tools before returning final JSON."""
        agent = self._make_agent_mock()
        agent.subconscious_pure_thought = False
        tool_call = ChatToolCall(call_id="call_1", name="web_search", arguments='{"query":"x"}')
        self._set_model_events(agent, [
            [ModelStreamEvent(type="tool_calls", tool_calls=[tool_call])],
            [self._json_event({
                "internal_content": "Tool result changed the thought.",
                "worthy": False,
                "recipient_hint": None,
                "external_content": None,
            })],
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.tool_executor.handle_tool_calls.assert_awaited_once()
            self.assertEqual(len(agent.model_client.calls), 2)
            agent.record_internal_thought.assert_called_once()
            self.assertEqual(agent.record_internal_thought.call_args[0][0], "Tool result changed the thought.")

    def test_pure_thought_tool_call_without_text_is_not_executed(self):
        """Pure thought mode rejects tool-only model turns without executing tools."""
        agent = self._make_agent_mock()
        tool_call = ChatToolCall(call_id="call_1", name="web_search", arguments='{"query":"x"}')
        self._set_model_events(agent, [
            [ModelStreamEvent(type="tool_calls", tool_calls=[tool_call])],
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.tool_executor.handle_tool_calls.assert_not_awaited()
            self.assertEqual(len(agent.model_client.calls), 1)
            agent.record_internal_thought.assert_not_called()

    def test_pure_thought_ignores_tool_call_when_text_is_present(self):
        """Pure thought mode parses returned JSON text and ignores stray tool calls."""
        agent = self._make_agent_mock()
        tool_call = ChatToolCall(call_id="call_1", name="web_search", arguments='{"query":"x"}')
        self._set_model_events(agent, [
            [
                ModelStreamEvent(type="tool_calls", tool_calls=[tool_call]),
                self._json_event({
                    "internal_content": "Text still wins.",
                    "worthy": False,
                    "recipient_hint": None,
                    "external_content": None,
                }),
            ],
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.tool_executor.handle_tool_calls.assert_not_awaited()
            agent.record_internal_thought.assert_called_once()
            self.assertEqual(agent.record_internal_thought.call_args[0][0], "Text still wins.")

    def test_non_json_after_tool_loop_falls_back_to_internal_thought(self):
        """Invalid final JSON does not trigger delivery after tool use."""
        agent = self._make_agent_mock()
        agent.subconscious_pure_thought = False
        tool_call = ChatToolCall(call_id="call_1", name="web_search", arguments='{"query":"x"}')
        self._set_model_events(agent, [
            [ModelStreamEvent(type="tool_calls", tool_calls=[tool_call])],
            [ModelStreamEvent(type="text", delta="not json")],
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            agent.tool_executor.handle_tool_calls.assert_awaited_once()
            agent.record_internal_thought.assert_called_once()
            self.assertEqual(agent.record_internal_thought.call_args[0][0], "not json")

    def test_maybe_think_not_triggered(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 0.0
            asyncio.run(loop.maybe_think())
            self.assertEqual(agent.model_client.calls, [])

    def test_maybe_think_unworthy_writes_internal_thought(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "Hmm interesting...",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.record_internal_thought.assert_called_once()
            call_args = agent.record_internal_thought.call_args
            self.assertEqual(call_args[0][0], "Hmm interesting...")


    def test_nighttime_worthy_writes_internal_thought(self):
        """During quiet hours, even worthy thoughts are written as internal thoughts."""
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "The timing matters, but this can wait.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A 3 AM revelation!",
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"feishu"})
            # Add a contact so routing has a recipient
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=False):
                asyncio.run(loop.maybe_think())

            # Should have written as internal thought, NOT delivered outward.
            agent.record_internal_thought.assert_called_once()
            call_args = agent.record_internal_thought.call_args
            self.assertEqual(call_args[0][0], "The timing matters, but this can wait.")

    def test_daytime_worthy_delivers_to_sink(self):
        """During appropriate hours, worthy thoughts are delivered directly."""
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This insight might help 张三 move the thread forward.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A daytime insight!",
        })
        delivery_sink = AsyncMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"feishu"},
            )
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            agent.record_internal_thought.assert_not_called()
            delivery_sink.assert_awaited_once()
            delivery = delivery_sink.await_args.args[0]
            self.assertEqual(delivery.content, "A daytime insight!")
            self.assertEqual(delivery.internal_content, "This insight might help 张三 move the thread forward.")
            self.assertEqual(delivery.recipient.channel, "feishu")
            self.assertEqual(delivery.recipient.user_id, "ou_123")

    def test_undeliverable_channel_worthy_writes_internal_thought(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This should not pretend to reach Feishu.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A Feishu-only note.",
        })
        delivery_sink = AsyncMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"api"},
            )
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            delivery_sink.assert_not_awaited()
            agent.record_internal_thought.assert_called_once()
            self.assertEqual(agent.record_internal_thought.call_args[0][0], "This should not pretend to reach Feishu.")

    def test_hint_to_undeliverable_contact_does_not_fallback_to_other_contact(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This was meant for 张三 only.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "Do not send this to Alice.",
        })
        delivery_sink = AsyncMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"api"},
            )
            save_contacts(loop.contacts_file, [
                ContactEntry(
                    channel="feishu",
                    user_id="ou_123",
                    target={"chat_id": "oc_xxx", "sender_name": "张三"},
                    last_seen="2026-06-25 09:00:00",
                ),
                ContactEntry(
                    channel="api",
                    user_id="api_user",
                    target={"user_id": "api_user", "sender_name": "Alice"},
                    last_seen="2026-06-25 10:00:00",
                ),
            ])
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            delivery_sink.assert_not_awaited()
            agent.record_internal_thought.assert_called_once()
            self.assertEqual(agent.record_internal_thought.call_args[0][0], "This was meant for 张三 only.")

    def test_empty_hint_defaults_to_most_recent_deliverable_contact(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This can go to the current reachable contact.",
            "worthy": True,
            "recipient_hint": None,
            "external_content": "A reachable note.",
        })
        delivery_sink = AsyncMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"api"},
            )
            save_contacts(loop.contacts_file, [
                ContactEntry(
                    channel="api",
                    user_id="old_api_user",
                    target={"user_id": "old_api_user", "sender_name": "Old"},
                    last_seen="2026-06-25 08:00:00",
                ),
                ContactEntry(
                    channel="feishu",
                    user_id="ou_123",
                    target={"chat_id": "oc_xxx", "sender_name": "张三"},
                    last_seen="2026-06-25 11:00:00",
                ),
                ContactEntry(
                    channel="api",
                    user_id="new_api_user",
                    target={"user_id": "new_api_user", "sender_name": "New"},
                    last_seen="2026-06-25 10:00:00",
                ),
            ])
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            delivery_sink.assert_awaited_once()
            delivery = delivery_sink.await_args.args[0]
            self.assertEqual(delivery.recipient.channel, "api")
            self.assertEqual(delivery.recipient.user_id, "new_api_user")
            agent.record_internal_thought.assert_not_called()

    def test_delivery_sink_failure_writes_internal_thought(self):
        """If direct delivery fails, the thought is retained as internal monologue."""
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This should not be lost.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A fragile outward message.",
        })
        delivery_sink = AsyncMock(side_effect=RuntimeError("send failed"))
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"feishu"},
            )
            loop._delivery_retry_delay_seconds = 0
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            self.assertEqual(delivery_sink.await_count, 3)
            agent.record_internal_thought.assert_called_once()
            self.assertEqual(agent.record_internal_thought.call_args[0][0], "This should not be lost.")

    def test_delivery_sink_transient_failure_retries_without_internal_thought(self):
        """A transient direct delivery failure is retried before falling back."""
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This should still reach the user.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A retried outward message.",
        })
        delivery_sink = AsyncMock(side_effect=[RuntimeError("rate limited"), None])
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"feishu"},
            )
            loop._delivery_retry_delay_seconds = 0
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            self.assertEqual(delivery_sink.await_count, 2)
            agent.record_internal_thought.assert_not_called()

    def test_worthy_without_recipient_writes_internal_thought(self):
        """A worthy thought with no route records the internal thought only."""
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This is for someone, but I do not know who yet.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A routable insight.",
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            agent.record_internal_thought.assert_called_once()
            call_args = agent.record_internal_thought.call_args
            self.assertEqual(call_args[0][0], "This is for someone, but I do not know who yet.")

    def test_worthy_without_external_content_writes_internal_thought(self):
        """A worthy decision without outward wording does not deliver an empty message."""
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "There is a signal here, but it is not speakable yet.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": None,
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            agent.record_internal_thought.assert_called_once()
            call_args = agent.record_internal_thought.call_args
            self.assertEqual(call_args[0][0], "There is a signal here, but it is not speakable yet.")


class InternalThoughtFormatTests(unittest.TestCase):
    """Verify the [internal_monologue] marker format for internal monologue."""

    def test_internal_monologue_event_type(self):
        self.assertEqual(SUBCONSCIOUS_EVENT_TYPE, "internal_monologue")

    def test_context_event_metadata_for_internal_thought(self):
        """Verify internal thoughts carry the right metadata."""
        from xagent.schemas import Message
        msg = Message.create_context_event(
            content="A private thought",
            source=SUBCONSCIOUS_SOURCE,
            event_type=SUBCONSCIOUS_EVENT_TYPE,
        )
        self.assertEqual(msg.type.value, "context_event")
        self.assertEqual(msg.metadata["event_type"], "internal_monologue")
        self.assertEqual(msg.metadata["source"], "subconscious")

    def test_internal_monologue_detection(self):
        """Verify _is_internal_monologue correctly identifies such messages."""
        from xagent.schemas import Message, MessageType
        from xagent.core.handlers.message import MessageHandler

        # Regular context event (ambient observation)
        regular = Message.create_context_event(
            content="Someone entered the room",
            source="environment",
            event_type="observation",
        )
        self.assertFalse(MessageHandler._is_internal_monologue(regular))

        # Internal monologue (ASSISTANT role identifies it as the agent's own thought)
        internal = Message.create_context_event(
            content="I just realized something...",
            source=SUBCONSCIOUS_SOURCE,
            event_type=SUBCONSCIOUS_EVENT_TYPE,
            role=RoleType.ASSISTANT,
        )
        self.assertTrue(MessageHandler._is_internal_monologue(internal))

    def test_internal_thought_header_format(self):
        """Verify the header format for internal thoughts."""
        from xagent.schemas import Message
        from xagent.core.handlers.message import MessageHandler

        msg = Message.create_context_event(
            content="Thinking...",
            source=SUBCONSCIOUS_SOURCE,
            event_type=SUBCONSCIOUS_EVENT_TYPE,
        )
        header = MessageHandler._format_internal_thought_header(msg)
        self.assertTrue(header.startswith("[internal_monologue][timestamp="))
        self.assertNotIn("[speaker=ME]", header)

    def test_experience_entry_uses_internal_monologue_header(self):
        """Verify _format_experience_entry uses the internal thought header."""
        from xagent.schemas import Message
        from xagent.core.handlers.message import MessageHandler

        msg = Message.create_context_event(
            content="A deep thought",
            source=SUBCONSCIOUS_SOURCE,
            event_type=SUBCONSCIOUS_EVENT_TYPE,
            role=RoleType.ASSISTANT,
        )
        lines = MessageHandler._format_experience_entry("observation", msg, "A deep thought")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("[internal_monologue][timestamp="))
        self.assertNotIn("[speaker=ME]", lines[0])


if __name__ == "__main__":
    unittest.main()
