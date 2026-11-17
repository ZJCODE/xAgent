"""Unit tests for the subconscious thought system."""

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from xagent.core.config import AgentConfig
from xagent.core.agent import Agent
from xagent.core.handlers.message import MessageHandler
from xagent.core.handlers.model import ChatToolCall, ModelStreamEvent
from xagent.schemas import Message, RoleType
from xagent.components.memory import RelationshipCard
from xagent.core.runtime.subconscious import (
    ContactEntry,
    SubconsciousLoop,
    load_contacts,
    save_contacts,
    upsert_contact,
    resolve_contacts_path,
    persist_subconscious_delivery,
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
                    channel="weixin",
                    user_id="wx_123",
                    target={"user_id": "wx_123", "sender_name": "张三"},
                    last_seen="2026-06-22 15:30:00",
                    interaction_count=5,
                ),
            ]
            save_contacts(contacts_file, contacts)
            loaded = load_contacts(contacts_file)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].channel, "weixin")
            self.assertEqual(loaded[0].user_id, "wx_123")
            self.assertEqual(loaded[0].target["user_id"], "wx_123")
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
            upsert_contact(contacts_file, "api", "user_1", {"user_id": "user_1"})
            upsert_contact(contacts_file, "weixin", "user_1", {"user_id": "user_1"})
            loaded = load_contacts(contacts_file)
            self.assertEqual(len(loaded), 2)

    def test_resolve_contacts_path(self):
        workspace = Path("/tmp/test_workspace")
        result = resolve_contacts_path(workspace)
        self.assertEqual(result, workspace / "contacts.json")


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

    async def test_reflection_can_decline_repeated_notes_using_existing_diary_context(self):
        """Exercise context and write routing; model decisions are scripted, not a semantic benchmark."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(client=object(), workspace=tmpdir)
            pending = "The morning tasks have not been merged yet."
            await agent.record_subconscious_thought(pending)
            before = await agent.markdown_memory.read_recent_dailies(days=1)
            calls = []

            class Client:
                async def model_turn_events(inner, **kwargs):
                    calls.append(kwargs)
                    yield ModelStreamEvent(type="text", delta='{"note": null, "message": null}')

            agent.model_client = Client()
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), interval_seconds=60)
            for _ in range(8):
                loop._next_think_at = 0
                await loop.maybe_think()
            self.assertEqual(await agent.markdown_memory.read_recent_dailies(days=1), before)
            self.assertEqual(len(calls), 8)  # No second judge call.
            for call in calls:
                memory = next(m for m in call["messages"] if m.get("name") == AgentConfig.RECENT_MEMORY_NAME)
                self.assertIn(pending, memory["content"])
                task = next(m for m in call["messages"] if m.get("name") == AgentConfig.CURRENT_TASK_NAME)
                self.assertIn("Compare the meaning", task["content"])
                self.assertIn("elapsed time", task["content"])
                self.assertIn("return note null", task["content"])
                self.assertEqual(call["tool_specs"], [])
            self.assertFalse((Path(tmpdir) / ".runtime" / "reflection.json").exists())

    async def test_reflection_keeps_a_new_insight_on_an_existing_topic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(client=object(), workspace=tmpdir)
            pending = "The morning tasks have not been merged yet."
            insight = "Collect air data separately to preserve freshness, then combine the delivery."
            await agent.record_subconscious_thought(pending)
            calls = []

            class Client:
                async def model_turn_events(inner, **kwargs):
                    calls.append(kwargs)
                    yield ModelStreamEvent(type="text", delta=json.dumps({"note": insight, "message": None}))

            agent.model_client = Client()
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            await loop.maybe_think()
            diary = "\n".join(text for _, text in await agent.markdown_memory.read_recent_dailies(days=1))
            self.assertIn(pending, diary)
            self.assertEqual(diary.count(insight), 1)
            self.assertEqual(len(calls), 1)

    async def test_restart_uses_diary_for_comparison_without_a_separate_note_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = Agent(client=object(), workspace=tmpdir)
            note = "Air readings need a separate collection step before the combined morning delivery."
            await original.record_subconscious_thought(note)
            before = await original.markdown_memory.read_recent_dailies(days=1)
            restarted = Agent(client=object(), workspace=tmpdir)
            captured = []

            class Client:
                async def model_turn_events(inner, **kwargs):
                    captured.append(kwargs)
                    yield ModelStreamEvent(type="text", delta='{"note": null, "message": null}')

            restarted.model_client = Client()
            loop = SubconsciousLoop(restarted, workspace=Path(tmpdir))
            loop._next_think_at = 0
            await loop.maybe_think()
            memory = next(m for m in captured[0]["messages"] if m.get("name") == AgentConfig.RECENT_MEMORY_NAME)
            self.assertIn(note, memory["content"])
            self.assertEqual(await restarted.markdown_memory.read_recent_dailies(days=1), before)

    async def test_record_subconscious_thought_skips_json_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(client=object(), workspace=tmpdir)
            blob = (
                '{"internal_content": "secret", "worthy": true, '
                '"recipient_hint": "ou_d988", "external_content": "draft"}'
            )

            await agent.record_subconscious_thought(blob)

            entries = await agent.markdown_memory.read_recent_dailies(days=1)
            self.assertEqual(entries, [])



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
        agent.recent_messages = AgentConfig.DEFAULT_RECENT_MESSAGES
        agent.max_agent_loops = AgentConfig.DEFAULT_MAX_AGENT_LOOPS
        agent.max_concurrent_tools = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
        agent._assistant_sender_id = "agent"
        memory_handler = MagicMock()
        memory_handler.get_subconscious_context.return_value = "Recent memory content."
        memory_handler.get_notebook_context.return_value = ""
        memory_handler.get_relationship_context = AsyncMock(return_value="")
        agent.memory_handler = memory_handler
        message_handler = MessageHandler(MagicMock(), system_prompt=agent.system_prompt)
        message_handler.get_recent_messages = AsyncMock(return_value=[])
        message_handler.store_context_event = AsyncMock()
        agent.message_handler = message_handler
        self._set_model_json(agent, {"note": 'Just a thought.', "message": None})
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
        agent.record_subconscious_thought = AsyncMock()
        return agent


    def test_recent_messages_injected_into_prompt(self):
        """Verify recent conversation messages are included in the LLM prompt."""
        from xagent.schemas import Message, RoleType

        agent = self._make_agent_mock()
        # Set up recent messages
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[
            Message(role=RoleType.USER, sender_id="alice", content="你好，今天心情怎么样？", timestamp=1716000000.0),
            Message(role=RoleType.ASSISTANT, sender_id=None, content="挺好的！", timestamp=1716000001.0),
        ])
        self._set_model_json(agent, {"note": 'Hmm.', "message": None})

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
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
            self.assertIn("Silence is fine", current_task["content"])
            self.assertIn('"note"', current_task["content"])
            self.assertIn('"message"', current_task["content"])
            self.assertIn("exact identifier", current_task["content"])
            self.assertIn("Respect explicit contact preferences", current_task["content"])
            current_time = current_task["content"].split("Current time: ", 1)[1].splitlines()[0]
            self.assertIsNotNone(datetime.fromisoformat(current_time).utcoffset())
            self.assertIn("never redirect someone else", current_task["content"])
            self.assertIn("recipient’s language", current_task["content"])
            self.assertNotIn("replay the same thought", current_task["content"])
            self.assertNotIn("quiet hours", current_task["content"].lower())
            self.assertNotIn("connect older memories", current_task["content"])
            self.assertNotIn("Known delivery contacts", current_task["content"])
            self.assertNotIn("rewrite the same unspoken thought", current_task["content"])


    def test_recent_messages_empty_uses_named_recent_experience_layer(self):
        """When there are no recent messages, the named layer remains with empty context."""
        agent = self._make_agent_mock()
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            recent_experience = next(msg for msg in messages if msg.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME)
            self.assertIn("[No recent experience]", recent_experience["content"])
            recent_memory = next(msg for msg in messages if msg.get("name") == AgentConfig.RECENT_MEMORY_NAME)
            self.assertIn("Recent memory content", recent_memory["content"])


    def test_subconscious_uses_wider_memory_context_when_available(self):
        agent = self._make_agent_mock()
        agent.memory_handler.get_subconscious_context = AsyncMock(
            return_value="Recent daily diary plus longer-range summaries."
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            recent_memory = next(msg for msg in messages if msg.get("name") == AgentConfig.RECENT_MEMORY_NAME)
            self.assertIn("longer-range summaries", recent_memory["content"])
            agent.memory_handler.get_subconscious_context.assert_awaited_once()


    def test_subconscious_injects_the_notebook_recalled_against_recent_diary(self):
        agent = self._make_agent_mock()
        agent.memory_handler.get_subconscious_context = AsyncMock(
            return_value="I spent the morning dialling in the grinder."
        )
        agent.memory_handler.get_notebook_context = AsyncMock(
            return_value="[pinned]\n- (202608190930) Grinder reads two clicks coarse"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            notebook = next(
                msg
                for msg in messages
                if msg.get("name") == AgentConfig.SUBCONSCIOUS_NOTEBOOK_NAME
            )
            self.assertIn("two clicks coarse", notebook["content"])
            self.assertNotIn(
                AgentConfig.NOTEBOOK_CONTEXT_NAME,
                {msg.get("name") for msg in messages},
            )
            kwargs = agent.memory_handler.get_notebook_context.await_args.kwargs
            self.assertIn("dialling in the grinder", kwargs["current_text"])


    def test_subconscious_ignores_a_non_text_notebook_context(self):
        agent = self._make_agent_mock()
        agent.memory_handler.get_notebook_context = MagicMock(return_value=object())

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            asyncio.run(loop.maybe_think())

            names = {msg.get("name") for msg in agent.model_client.calls[0]["messages"]}
            self.assertNotIn(AgentConfig.SUBCONSCIOUS_NOTEBOOK_NAME, names)


    def test_subconscious_omits_tools_and_skills(self):
        """Subconscious turns keep identity but omit tool/skill capability layers."""
        agent = self._make_agent_mock()
        agent.system_prompt = "I am a test identity."
        agent.message_handler.system_prompt = agent.system_prompt

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            asyncio.run(loop.maybe_think())

            instructions = agent.model_client.calls[0]["instructions"]

            names = {i.get("name") for i in instructions}
            self.assertIn(AgentConfig.CORE_INTERACTION_RULES_NAME, names)
            self.assertIn(AgentConfig.CURRENT_MODE_NAME, names)
            self.assertIn(AgentConfig.IDENTITY_CONTEXT_NAME, names)
            self.assertNotIn(AgentConfig.TOOL_POLICY_NAME, names)
            self.assertNotIn(AgentConfig.WORKSPACE_CONTEXT_NAME, names)
            self.assertNotIn(AgentConfig.SKILLS_CATALOG_NAME, names)
            self.assertNotIn(AgentConfig.CAPABILITY_LIMITS_NAME, names)

            contents = [i["content"] for i in instructions]
            self.assertTrue(any("Context and Attribution" in c for c in contents))
            self.assertTrue(any("already know what to call them" in c for c in contents))
            self.assertFalse(any("avoid unsolicited messages" in c for c in contents))
            self.assertFalse(any("worthy=true" in c for c in contents))
            self.assertFalse(any("quiet hours" in c.lower() for c in contents))
            self.assertFalse(any("All available tools are defined" in c for c in contents))
            self.assertEqual(agent.model_client.calls[0]["tool_specs"], [])
            agent._workspace_context.assert_not_called()
            agent._skills_catalog_context.assert_not_called()

            identities = [i for i in instructions if i.get("name") == "identity_context"]
            self.assertEqual(len(identities), 1)
            self.assertIn("I am a test identity.", identities[0]["content"])

            modes = [i for i in instructions if i.get("name") == AgentConfig.CURRENT_MODE_NAME]
            self.assertEqual(len(modes), 1)
            self.assertIn('<current_mode name="private_reflection">', modes[0]["content"])
            self.assertIn("<purpose>", modes[0]["content"])
            core = next(
                i["content"] for i in instructions
                if i.get("name") == AgentConfig.CORE_INTERACTION_RULES_NAME
            )
            self.assertNotIn("<current_mode", core)
            self.assertNotIn("avoid unsolicited messages", core)


    def test_working_context_injected_into_subconscious_prompt(self):
        from xagent.core.working_context import WorkingContextView

        agent = self._make_agent_mock()
        agent._working_context_for_turn = AsyncMock(
            return_value=WorkingContextView(
                summary="Earlier we discussed Redis cluster failover.",
                covers_through_cursor=18,
            )
        )
        self._set_model_json(agent, {"note": 'Still thinking about the failover edge case.', "message": None})

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            recent_experience = next(
                msg for msg in messages if msg.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME
            )
            self.assertIn("[Earlier working context]", recent_experience["content"])
            self.assertIn("Earlier we discussed Redis cluster failover.", recent_experience["content"])


    def test_notebook_recall_query_excludes_long_term_summaries(self):
        from xagent.schemas import Message, RoleType

        agent = self._make_agent_mock()
        agent.memory_handler.get_subconscious_context = AsyncMock(
            return_value=(
                "Longer-range diary summaries:\n"
                "[yearly: 2025]\nYearly macro keywords alpha beta\n\n"
                "[monthly: 2026-04]\nMonthly recap gamma delta\n\n"
                "[weekly: 2026-05-01_2026-05-07]\nWeekly digest epsilon\n\n"
                "Recent daily diary:\n"
                "## 2026-05-18\nToday we debugged the Raft consensus election timeout."
            )
        )
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[
            Message(role=RoleType.USER, sender_id="alice", content="Check the leader lease.", timestamp=1716000000.0),
        ])
        agent.memory_handler.get_notebook_context = AsyncMock(return_value="")
        self._set_model_json(agent, {"note": '', "message": None})

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            asyncio.run(loop.maybe_think())

            agent.memory_handler.get_notebook_context.assert_awaited_once()
            query = agent.memory_handler.get_notebook_context.call_args.kwargs["current_text"]
            self.assertIn("Raft consensus election timeout", query)
            self.assertIn("Check the leader lease", query)
            self.assertNotIn("Yearly macro keywords", query)
            self.assertNotIn("Monthly recap", query)
            self.assertNotIn("Weekly digest", query)


    def test_subconscious_message_shows_recipient_in_recent_experience(self):
        from xagent.schemas import Message, RoleType

        agent = self._make_agent_mock()
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[
            Message(
                role=RoleType.ASSISTANT,
                sender_id="agent",
                recipient_id="wx_123",
                channel="weixin",
                content="Just remembered that book you asked about.",
                timestamp=1716000000.0,
                metadata={
                    "subconscious": {
                        "source": "subconscious",
                        "recipient": {
                            "channel": "weixin",
                            "user_id": "wx_123",
                            "target": {"user_id": "wx_123", "sender_name": "张三"},
                        },
                    }
                },
            ),
        ])
        self._set_model_json(agent, {"note": '', "message": None})

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._next_think_at = 0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            recent_experience = next(
                msg for msg in messages if msg.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME
            )
            self.assertIn("[to=张三(wx_123)]", recent_experience["content"])



class FixedIntervalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from xagent.core.inbox import AgentInbox
        from types import SimpleNamespace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = SimpleNamespace(inbox=AgentInbox(), record_subconscious_thought=AsyncMock())
        self.sink = AsyncMock()
        self.loop = SubconsciousLoop(self.agent, workspace=Path(self.tmp.name),
                                    interval_seconds=60, delivery_sink=self.sink,
                                    deliverable_channels={'api', 'weixin'})
        self.loop.record_interaction('api', 'Alice', {'sender_name': 'Same'})
        self.loop.record_interaction('weixin', 'Alice', {'sender_name': 'Same'})
        self.loop._generate_subconscious_thought = AsyncMock(return_value={
            'note': None, 'message': {'recipient': 'api:Alice', 'text': 'New insight'}})
        self.loop._delivery_retry_delay_seconds = 0

    async def due(self):
        self.loop._next_think_at = 0
        await self.loop.maybe_think()

    async def test_fixed_schedule_and_no_immediate_start(self):
        await self.loop.maybe_think()
        self.loop._generate_subconscious_thought.assert_not_called()
        await self.due()
        await self.loop.maybe_think()
        self.assertEqual(self.sink.await_count, 1)

    async def test_busy_keeps_due_opportunity(self):
        await self.agent.inbox.acquire_turn()
        await self.due()
        self.assertEqual(self.loop._next_think_at, 0)
        self.agent.inbox.release_turn()
        await self.loop.maybe_think()
        self.sink.assert_awaited_once()

    async def test_repeated_silence_and_errors_do_not_change_interval(self):
        for result in ({'note': None, 'message': None}, RuntimeError('provider down')):
            self.loop._generate_subconscious_thought.side_effect = result if isinstance(result, Exception) else None
            self.loop._generate_subconscious_thought.return_value = result
            for _ in range(6):
                before = time.monotonic()
                await self.due()
                self.assertAlmostEqual(self.loop._next_think_at - before, 60, delta=1)

    async def test_exact_recipient_without_relationship_card(self):
        await self.due()
        self.assertEqual(self.sink.call_args.args[0].recipient.channel, 'api')
        self.assertEqual(self.sink.call_args.args[0].recipient.user_id, 'Alice')

    async def test_invalid_recipients_never_fallback(self):
        for recipient in ('Alice', 'Same', 'api:alice', 'api:unknown'):
            self.loop._generate_subconscious_thought.return_value = {
                'note': None, 'message': {'recipient': recipient, 'text': 'Private'}}
            await self.due()
        self.sink.assert_not_called()

    async def test_short_completed_request_invalidates_whole_result(self):
        async def generate(contacts):
            await self.agent.inbox.acquire_turn()
            self.agent.inbox.release_turn()
            return {'note': 'Old context', 'message': {'recipient': 'api:Alice', 'text': 'Old'}}
        self.loop._generate_subconscious_thought.side_effect = generate
        await self.due()
        self.sink.assert_not_called()
        self.agent.record_subconscious_thought.assert_not_called()

    async def test_delivery_failure_does_not_block_note_or_next_cycle(self):
        self.sink.side_effect = RuntimeError('disconnected')
        self.loop._generate_subconscious_thought.return_value['note'] = 'Useful private note'
        await self.due()
        self.assertEqual(self.sink.await_count, 3)
        self.agent.record_subconscious_thought.assert_awaited_once_with('Useful private note')
        self.sink.side_effect = None
        await self.due()
        self.assertEqual(self.sink.await_count, 4)

    async def test_note_failure_does_not_block_delivery(self):
        self.agent.record_subconscious_thought.side_effect = RuntimeError('diary failed')
        self.loop._generate_subconscious_thought.return_value['note'] = 'New note'
        await self.due()
        self.sink.assert_awaited_once()

    async def test_permanent_error_does_not_retry(self):
        self.sink.side_effect = ValueError('bad target')
        await self.due()
        self.sink.assert_awaited_once()

    async def test_uncertain_delivery_is_not_retried(self):
        from xagent.core.runtime.task_receipts import DeliveryUncertainError
        self.sink.side_effect = DeliveryUncertainError("may have partially played")
        await self.due()
        self.sink.assert_awaited_once()

    async def test_interruption_during_retry_stops_resending(self):
        async def sink(delivery):
            ticket = self.agent.inbox.reserve_turn()
            self.agent.inbox.discard(ticket)
            raise RuntimeError('disconnected')
        self.sink.side_effect = sink
        await self.due()
        self.sink.assert_awaited_once()

    async def test_restart_ignores_legacy_state(self):
        state = Path(self.tmp.name) / '.runtime' / 'subconscious_state.json'
        state.parent.mkdir()
        state.write_text('{"stale_streak": 100, "unanswered_outbound": {"api:Alice": 99}}')
        loop = SubconsciousLoop(self.agent, workspace=Path(self.tmp.name), interval_seconds=60)
        self.assertGreater(loop._next_think_at, time.monotonic())
        self.assertFalse(hasattr(loop, '_stale_streak'))
        self.assertTrue(state.exists())

    async def test_disabled_and_concurrent_cycles(self):
        self.loop._interval = 0
        await self.due()
        self.loop._generate_subconscious_thought.assert_not_called()
        self.loop._interval = 60
        async def generate(contacts):
            await asyncio.sleep(0)
            return {'note': None, 'message': None}
        self.loop._generate_subconscious_thought.side_effect = generate
        self.loop._next_think_at = 0
        await asyncio.gather(self.loop.maybe_think(), self.loop.maybe_think())
        self.loop._generate_subconscious_thought.assert_awaited_once()

    def test_json_protocol_rejects_malformed_results(self):
        for text in ('raw thought', '{"note":', '[]', '{"note": 2, "message": null}',
                     '{"note": null, "message": {"recipient": "Alice"}}',
                     '{"internal_content": "old", "worthy": true}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                SubconsciousLoop._parse_subconscious_json(text)
        result = SubconsciousLoop._parse_subconscious_json('```json\n{"note": null, "message": null}\n```')
        self.assertEqual(result, {'note': None, 'message': None})

    async def test_real_context_and_history_survive_restart(self):
        agent = Agent(client=object(), workspace=self.tmp.name)
        calls = []
        class Client:
            async def model_turn_events(inner, **kwargs):
                calls.append(kwargs)
                yield ModelStreamEvent(type='text', delta=json.dumps({
                    'note': None, 'message': {'recipient': 'api:Alice', 'text': 'A useful update'}}))
        agent.model_client = Client()
        async def sink(delivery):
            await persist_subconscious_delivery(agent, delivery, channel='api', recipient_id='Alice')
        for _ in range(2):
            loop = SubconsciousLoop(agent, workspace=Path(self.tmp.name), interval_seconds=60,
                                    delivery_sink=sink, deliverable_channels={'api'})
            loop._next_think_at = 0
            await loop.maybe_think()
        history = await agent.message_storage.get_messages()
        self.assertEqual([m.content for m in history], ['A useful update', 'A useful update'])
        self.assertIn('api:Alice', json.dumps(calls[0]['messages']))
        self.assertEqual(calls[0]['tool_specs'], [])
