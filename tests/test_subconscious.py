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
    async def test_persisted_followups_remain_visible_after_restart_without_merging_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(client=object(), workspace=tmpdir)
            await agent.message_storage.add_messages(Message(
                role=RoleType.USER, sender_id="Alice", channel="api", content="Let me know what you find.",
            ))
            await agent.relationship_store.write_card(RelationshipCard(
                key="api:Alice", display_name="Alice", body="Alice welcomes updates on our project.",
            ))
            agent.model_client = SubconsciousLoopTests._ModelClientStub([
                [SubconsciousLoopTests._json_event({
                    "internal_content": f"I have update {index} for Alice.", "worthy": True,
                    "recipient_hint": "Alice", "external_content": f"Here is update {index}.",
                })] for index in (1, 2)
            ])

            async def sink(delivery):
                await persist_subconscious_delivery(agent, delivery, channel="api", recipient_id="Alice")

            loop = SubconsciousLoop(
                agent, workspace=Path(tmpdir), probability=1, delivery_sink=sink, deliverable_channels={"api"},
            )
            loop.record_interaction("api", "Alice", {"user_id": "Alice"})
            await loop.maybe_think()
            loop = SubconsciousLoop(
                agent, workspace=Path(tmpdir), probability=1, delivery_sink=sink, deliverable_channels={"api"},
            )
            self.assertEqual(loop._unanswered_outbound, {"api:Alice": 1})
            await loop.maybe_think()
            self.assertEqual(loop._unanswered_outbound, {"api:Alice": 2})
            relationship_context = next(
                m["content"] for m in agent.model_client.calls[-1]["messages"]
                if m.get("name") == AgentConfig.SUBCONSCIOUS_RELATIONSHIPS_NAME
            )
            self.assertIn("1 proactive message(s)", relationship_context)
            history = await agent.message_storage.get_messages()
            self.assertEqual([m.content for m in history if m.role == RoleType.ASSISTANT], ["Here is update 1.", "Here is update 2."])
            loop.record_interaction("api", "alice", {"user_id": "alice"})
            self.assertEqual(loop._unanswered_outbound, {"api:Alice": 2})

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
        agent.record_subconscious_thought = AsyncMock()
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
        self.assertEqual(result["internal_content"], "")
        self.assertIsNone(result["external_content"])

    def test_parse_subconscious_json_accepts_curly_quotes(self):
        result = SubconsciousLoop._parse_subconscious_json(
            '{“internal_content”: “hello”, “worthy”: false, “recipient_hint”: null, “external_content”: null}'
        )
        self.assertFalse(result["worthy"])
        self.assertEqual(result["internal_content"], "hello")
        self.assertIsNone(result["external_content"])

    def test_parse_subconscious_json_does_not_keep_broken_payload(self):
        blob = (
            '{“internal_content”:“他问到点子上了,那两堵"墙"怎么来的。”,'
            '“worthy”:true,“recipient_hint”:“ou_d988”,“external_content”:"问到关键处了\n'
            "画一个单位圆"
        )
        result = SubconsciousLoop._parse_subconscious_json(blob)
        self.assertFalse(result["worthy"])
        self.assertEqual(result["internal_content"], "")
        self.assertIsNone(result["external_content"])
        self.assertNotIn("ou_d988", result["internal_content"])
        self.assertNotIn("external_content", result["internal_content"])

    def test_parse_subconscious_json_embedded_in_prose(self):
        result = SubconsciousLoop._parse_subconscious_json(
            "A private note first.\n"
            + json.dumps({
                "internal_content": "This should still count.",
                "worthy": True,
                "recipient_hint": "web_user",
                "external_content": "Hello from the inner voice.",
            })
            + "\nThanks."
        )
        self.assertTrue(result["worthy"])
        self.assertEqual(result["internal_content"], "This should still count.")
        self.assertEqual(result["external_content"], "Hello from the inner voice.")
        self.assertEqual(result["recipient_hint"], "web_user")

    def test_parse_subconscious_json_normalizes_string_worthy_and_aliases(self):
        result = SubconsciousLoop._parse_subconscious_json(
            json.dumps({
                "thought": "Alias thought",
                "worthy": "true",
                "recipient": "Alice",
                "outward_content": "Alias message",
            })
        )
        self.assertTrue(result["worthy"])
        self.assertEqual(result["internal_content"], "Alias thought")
        self.assertEqual(result["recipient_hint"], "Alice")
        self.assertEqual(result["external_content"], "Alias message")

        false_result = SubconsciousLoop._parse_subconscious_json(
            json.dumps({
                "internal_content": "Keep this inside.",
                "worthy": "false",
                "recipient_hint": None,
                "external_content": None,
            })
        )
        self.assertFalse(false_result["worthy"])

    def test_held_back_diary_note_matches_thought_language(self):
        self.assertEqual(
            SubconsciousLoop._held_back_diary_note("This should reach Telos."),
            "This should reach Telos.\nI didn't send this.",
        )
        self.assertEqual(
            SubconsciousLoop._held_back_diary_note("这个念头还在心里转。"),
            "这个念头还在心里转。\n我没有发出去。",
        )
        self.assertEqual(
            SubconsciousLoop._held_back_diary_note("Already held.\nI didn't send this."),
            "Already held.\nI didn't send this.",
        )

    def test_pick_recipient_requires_an_explicit_hint(self):
        contacts = [
            ContactEntry(
                channel="api",
                user_id="web_user",
                target={"user_id": "web_user"},
                last_seen="2026-08-15 23:13:00",
            ),
        ]
        self.assertIsNone(SubconsciousLoop._pick_recipient(contacts, None))
        self.assertIsNone(SubconsciousLoop._pick_recipient(contacts, ""))
        self.assertEqual(
            SubconsciousLoop._pick_recipient(contacts, "web_user").user_id,
            "web_user",
        )

    def test_pick_recipient_does_not_let_short_hint_match_longer_name(self):
        contacts = [
            ContactEntry(
                channel="weixin",
                user_id="wx_liming",
                target={"sender_name": "李明"},
                last_seen="2026-08-16 00:00:00",
            ),
        ]
        self.assertIsNone(SubconsciousLoop._pick_recipient(contacts, "李"))
        self.assertEqual(
            SubconsciousLoop._pick_recipient(contacts, "李明").user_id,
            "wx_liming",
        )
        self.assertEqual(
            SubconsciousLoop._pick_recipient(contacts, "李明 (weixin)").user_id,
            "wx_liming",
        )

    def test_record_interaction(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"weixin"})
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            contacts = load_contacts(loop.contacts_file)
            self.assertEqual(len(contacts), 1)
            self.assertEqual(contacts[0].channel, "weixin")

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
            self.assertIn("empty internal_content", current_task["content"])
            self.assertIn("worthy=true", current_task["content"])
            self.assertIn("recipient_hint", current_task["content"])
            self.assertIn("exact user_id", current_task["content"])
            self.assertIn("diary is only yours", current_task["content"])
            self.assertIn("did not send it", current_task["content"])
            self.assertIn("their expressed preferences", current_task["content"])
            self.assertIn("not sending restrictions", current_task["content"])
            self.assertNotIn("At night", current_task["content"])
            current_time = current_task["content"].split("Current time: ", 1)[1].splitlines()[0]
            self.assertIsNotNone(datetime.fromisoformat(current_time).utcoffset())
            self.assertIn("would speak now", current_task["content"])
            self.assertIn("will be sent", current_task["content"])
            self.assertIn("their thread to someone else", current_task["content"])
            self.assertIn("people on this channel only", current_task["content"])
            self.assertIn("recent diary already holds this observation", current_task["content"])
            self.assertIn("return empty internal_content", current_task["content"])
            self.assertIn("nothing in life has moved", current_task["content"])
            self.assertIn(
                "Write internal_content and external_content in the recent conversation language",
                current_task["content"],
            )
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
            loop._probability = 1.0
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
            loop._probability = 1.0
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
            loop._probability = 1.0
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
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            names = {msg.get("name") for msg in agent.model_client.calls[0]["messages"]}
            self.assertNotIn(AgentConfig.SUBCONSCIOUS_NOTEBOOK_NAME, names)

    def test_collect_relationship_context_reads_store_cards_without_contacts(self):
        agent = self._make_agent_mock()
        memory_handler = MagicMock()
        memory_handler.relationship_store.list_keys = AsyncMock(return_value=["weixin:alice"])
        memory_handler.get_relationship_context = AsyncMock(return_value="## Alice\nAn older open thread.")
        agent.memory_handler = memory_handler

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"weixin"})
            context = asyncio.run(loop._collect_relationship_context())

        self.assertIn("older open thread", context)
        memory_handler.get_relationship_context.assert_awaited_once()
        kwargs = memory_handler.get_relationship_context.await_args.kwargs
        self.assertEqual(kwargs["speaker_keys"], ["weixin:alice"])
        self.assertTrue(kwargs["include_routing_id"])

    def test_collect_relationship_context_omits_other_channel_cards(self):
        """This runtime only sees people it can send to; Weixin ids stay off the API loop."""
        agent = self._make_agent_mock()
        memory_handler = MagicMock()
        memory_handler.relationship_store.list_keys = AsyncMock(
            return_value=["weixin:alice", "api:web_user"]
        )
        memory_handler.get_relationship_context = AsyncMock(
            return_value="## Web user [user_id: web_user]\nReachable here."
        )
        agent.memory_handler = memory_handler

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"api"})
            context = asyncio.run(loop._collect_relationship_context())

        self.assertIn("Reachable here", context)
        kwargs = memory_handler.get_relationship_context.await_args.kwargs
        self.assertEqual(kwargs["speaker_keys"], ["api:web_user"])

    def test_collect_relationship_context_omits_just_seen_other_channel_contact(self):
        agent = self._make_agent_mock()
        memory_handler = MagicMock()
        memory_handler.relationship_store.list_keys = AsyncMock(return_value=[])
        memory_handler.get_relationship_context = AsyncMock(return_value="## Web user\nJust spoke.")
        agent.memory_handler = memory_handler

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"api"})
            loop.record_interaction(
                channel="weixin",
                user_id="wx_telos",
                target={"user_id": "wx_telos", "sender_name": "Telos"},
            )
            loop.record_interaction(
                channel="api",
                user_id="web_user",
                target={"user_id": "web_user"},
            )
            context = asyncio.run(loop._collect_relationship_context())

        kwargs = memory_handler.get_relationship_context.await_args.kwargs
        self.assertEqual(kwargs["speaker_keys"], ["api:web_user"])
        self.assertIn("Just spoke", context)

    def test_collect_relationship_context_empty_without_deliverable_channels(self):
        agent = self._make_agent_mock()
        memory_handler = MagicMock()
        memory_handler.relationship_store.list_keys = AsyncMock(
            return_value=["weixin:alice", "api:web_user"]
        )
        memory_handler.get_relationship_context = AsyncMock(return_value="should not be used")
        agent.memory_handler = memory_handler

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            context = asyncio.run(loop._collect_relationship_context())

        self.assertEqual(context, "")
        memory_handler.get_relationship_context.assert_not_awaited()

    def test_deliverable_filter_keeps_declared_channels_only(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"api"})
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop.record_interaction(
                channel="api",
                user_id="api_user",
                target={"user_id": "api_user", "sender_name": "Alice"},
            )

            deliverable = loop._filter_deliverable_contacts(load_contacts(loop.contacts_file))

            channels = {contact.channel for contact in deliverable}
            self.assertEqual(channels, {"api"})
            user_ids = {contact.user_id for contact in deliverable}
            self.assertIn("api_user", user_ids)
            self.assertNotIn("wx_123", user_ids)

    def test_deliverable_filter_without_declared_channels_exposes_no_contacts(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )

            deliverable = loop._filter_deliverable_contacts(load_contacts(loop.contacts_file))

            self.assertEqual(deliverable, [])

    def test_subconscious_omits_tools_and_skills(self):
        """Subconscious turns keep identity but omit tool/skill capability layers."""
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

    def test_tool_call_without_text_is_not_executed(self):
        """Subconscious turns reject tool-only model turns without executing tools."""
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
            agent.record_subconscious_thought.assert_not_called()
            agent.message_handler.store_context_event.assert_not_called()

    def test_tool_call_with_text_is_not_executed(self):
        """Subconscious turns parse returned JSON text and ignore stray tool calls."""
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
            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(agent.record_subconscious_thought.call_args[0][0], "Text still wins.")

    def test_non_json_with_tool_call_falls_back_to_subconscious_thought(self):
        """Invalid final JSON text with a stray tool call is recorded without executing tools."""
        agent = self._make_agent_mock()
        tool_call = ChatToolCall(call_id="call_1", name="web_search", arguments='{"query":"x"}')
        self._set_model_events(agent, [
            [
                ModelStreamEvent(type="tool_calls", tool_calls=[tool_call]),
                ModelStreamEvent(type="text", delta="not json"),
            ],
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.tool_executor.handle_tool_calls.assert_not_awaited()
            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(agent.record_subconscious_thought.call_args[0][0], "not json")

    def test_broken_json_payload_is_not_written_to_diary(self):
        agent = self._make_agent_mock()
        blob = (
            '{“internal_content”:“他问到点子上了。”,“worthy”:true,'
            '“recipient_hint”:“ou_d988”,“external_content”:"问到关键处了\n画一个单位圆'
        )
        self._set_model_events(agent, [[ModelStreamEvent(type="text", delta=blob)]])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_not_called()

    def test_maybe_think_not_triggered(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 0.0
            asyncio.run(loop.maybe_think())
            self.assertEqual(agent.model_client.calls, [])

    def test_maybe_think_unworthy_writes_subconscious_thought(self):
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

            agent.record_subconscious_thought.assert_called_once()
            call_args = agent.record_subconscious_thought.call_args
            self.assertEqual(call_args[0][0], "Hmm interesting...")
            agent.message_handler.store_context_event.assert_not_called()

    def test_blank_subconscious_result_is_successful_noop(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })
        delivery_sink = AsyncMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"weixin"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_not_called()
            delivery_sink.assert_not_awaited()
            agent.message_handler.store_context_event.assert_not_called()


    def test_worthy_delivers_without_clock_gate(self):
        """A worthy thought is delivered; night is a prompt judgment, not a send skip."""
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "The timing matters, but I would speak now.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A 3 AM follow-up.",
        })
        delivery_sink = AsyncMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"weixin"},
            )
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            delivery_sink.assert_awaited_once()
            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(
                agent.record_subconscious_thought.call_args[0][0],
                "The timing matters, but I would speak now.",
            )
            self.assertNotIn("I didn't send this.", agent.record_subconscious_thought.call_args[0][0])
            agent.message_handler.store_context_event.assert_not_called()

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
                deliverable_channels={"weixin"},
            )
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(
                agent.record_subconscious_thought.call_args[0][0],
                "This insight might help 张三 move the thread forward.",
            )
            delivery_sink.assert_awaited_once()
            delivery = delivery_sink.await_args.args[0]
            self.assertEqual(delivery.content, "A daytime insight!")
            self.assertEqual(delivery.internal_content, "This insight might help 张三 move the thread forward.")
            self.assertEqual(delivery.recipient.channel, "weixin")
            self.assertEqual(delivery.recipient.user_id, "wx_123")
            agent.message_handler.store_context_event.assert_not_called()

    def test_undeliverable_channel_worthy_writes_subconscious_thought(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This should not pretend to reach Weixin.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A Weixin-only note.",
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
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            delivery_sink.assert_not_awaited()
            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(
                agent.record_subconscious_thought.call_args[0][0],
                "This should not pretend to reach Weixin.\nI didn't send this.",
            )
            agent.message_handler.store_context_event.assert_not_called()

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
                    channel="weixin",
                    user_id="wx_123",
                    target={"user_id": "wx_123", "sender_name": "张三"},
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

            asyncio.run(loop.maybe_think())

            delivery_sink.assert_not_awaited()
            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(
                agent.record_subconscious_thought.call_args[0][0],
                "This was meant for 张三 only.\n我没有发出去。",
            )

    def test_display_name_hint_matches_api_contact_via_relationship_card(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This is for Alice, even if her routing id is web_user.",
            "worthy": True,
            "recipient_hint": "Alice",
            "external_content": "A note for Alice.",
        })
        card = MagicMock()
        card.key = "api:web_user"
        card.display_name = "Alice"
        agent.memory_handler.relationship_store.read_cards = AsyncMock(return_value=[card])
        delivery_sink = AsyncMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"api"},
            )
            loop.record_interaction(
                channel="api",
                user_id="web_user",
                target={"user_id": "web_user"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            delivery_sink.assert_awaited_once()
            delivery = delivery_sink.await_args.args[0]
            self.assertEqual(delivery.recipient.channel, "api")
            self.assertEqual(delivery.recipient.user_id, "web_user")
            self.assertEqual(delivery.content, "A note for Alice.")

    def test_channel_prefixed_hint_matches_deliverable_contact(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "Address this with the relationship key.",
            "worthy": True,
            "recipient_hint": "api:web_user",
            "external_content": "A keyed note.",
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
                channel="api",
                user_id="web_user",
                target={"user_id": "web_user"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            delivery_sink.assert_awaited_once()
            delivery = delivery_sink.await_args.args[0]
            self.assertEqual(delivery.recipient.user_id, "web_user")

    def test_empty_hint_does_not_send_to_another_contact(self):
        """An unnamed worthy thought must not be dumped on whoever this channel can reach."""
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This was about Telos on Weixin, not the web user.",
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
                    channel="weixin",
                    user_id="wx_123",
                    target={"user_id": "wx_123", "sender_name": "Telos"},
                    last_seen="2026-06-25 11:00:00",
                ),
                ContactEntry(
                    channel="api",
                    user_id="web_user",
                    target={"user_id": "web_user", "sender_name": "New"},
                    last_seen="2026-06-25 10:00:00",
                ),
            ])
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            delivery_sink.assert_not_awaited()
            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(
                agent.record_subconscious_thought.call_args[0][0],
                "This was about Telos on Weixin, not the web user.\nI didn't send this.",
            )

    def test_delivery_sink_failure_keeps_subconscious_thought(self):
        """If direct delivery fails, the already-recorded diary thought is retained."""
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
                deliverable_channels={"weixin"},
            )
            loop._delivery_retry_delay_seconds = 0
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            self.assertEqual(delivery_sink.await_count, 3)
            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(
                agent.record_subconscious_thought.call_args[0][0],
                "This should not be lost.\nI didn't send this.",
            )

    def test_delivery_sink_transient_failure_retries_with_diary_thought(self):
        """A transient direct delivery failure is retried while the thought remains in diary."""
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
                deliverable_channels={"weixin"},
            )
            loop._delivery_retry_delay_seconds = 0
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            self.assertEqual(delivery_sink.await_count, 2)
            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(
                agent.record_subconscious_thought.call_args[0][0],
                "This should still reach the user.",
            )

    def test_worthy_without_recipient_writes_subconscious_thought(self):
        """A worthy thought with no route records the diary thought only."""
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

            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_called_once()
            call_args = agent.record_subconscious_thought.call_args
            self.assertEqual(
                call_args[0][0],
                "This is for someone, but I do not know who yet.\nI didn't send this.",
            )

    def test_worthy_without_external_content_writes_subconscious_thought(self):
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
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_called_once()
            call_args = agent.record_subconscious_thought.call_args
            self.assertEqual(call_args[0][0], "There is a signal here, but it is not speakable yet.")

    def test_effective_probability_habituates_on_stale_streak(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            self.assertEqual(loop._effective_probability(), 1.0)

            loop._stale_streak = 1
            self.assertEqual(loop._effective_probability(), 0.5)

            loop._stale_streak = 4
            self.assertEqual(loop._effective_probability(), 0.0625)

            loop._stale_streak = 8
            self.assertEqual(loop._effective_probability(), 1.0 / 256)

            # No floor: long streaks keep halving toward silence until
            # messages arrive or solitude recovers the streak.
            loop._stale_streak = 20
            self.assertLess(loop._effective_probability(), 1e-6)

    def test_solitude_recovers_habituation_without_new_messages(self):
        agent = self._make_agent_mock()
        agent.message_storage = MagicMock()
        agent.message_storage.get_latest_message_cursor = AsyncMock(return_value=42)
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 0.0  # only exercise recovery, not generation
            loop._last_experience_cursor = 42
            loop._stale_streak = 3
            loop._recovery_seconds = 3600.0
            loop._habituation_anchor_mono = time.monotonic() - 7200.0

            asyncio.run(loop.maybe_think())

            self.assertEqual(loop._stale_streak, 1)
            self.assertEqual(loop._effective_probability(), 0.0)  # activity still 0
            loop._probability = 1.0
            self.assertEqual(loop._effective_probability(), 0.5)

    def test_unworthy_thought_habituates_without_blocking_diary(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "这个陌生人来来回回就是那几句。",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })
        agent.message_storage = MagicMock()
        agent.message_storage.get_latest_message_cursor = AsyncMock(return_value=42)
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            loop._last_experience_cursor = 42

            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(loop._stale_streak, 1)
            self.assertEqual(loop._effective_probability(), 0.5)

    def test_empty_thought_also_habituates(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })
        agent.message_storage = MagicMock()
        agent.message_storage.get_latest_message_cursor = AsyncMock(return_value=7)
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            loop._last_experience_cursor = 7

            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_not_called()
            self.assertEqual(loop._stale_streak, 1)

    def test_new_experience_clears_habituation_before_dice(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "对方又开口了，语气比刚才松一点。",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })
        agent.message_storage = MagicMock()
        agent.message_storage.get_latest_message_cursor = AsyncMock(return_value=99)
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            loop._last_experience_cursor = 10
            loop._stale_streak = 4

            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_called_once()
            self.assertEqual(loop._stale_streak, 1)
            self.assertEqual(loop._last_experience_cursor, 99)

    def test_worthy_delivery_clears_habituation(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "This insight might help 张三 move the thread forward.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "A daytime insight!",
        })
        delivery_sink = AsyncMock()
        agent.message_storage = MagicMock()
        agent.message_storage.get_latest_message_cursor = AsyncMock(return_value=3)
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"weixin"},
            )
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0
            loop._last_experience_cursor = 3
            loop._stale_streak = 3
            self.assertLess(loop._effective_probability(), 1.0)
            loop._stale_streak = 0

            asyncio.run(loop.maybe_think())

            delivery_sink.assert_awaited_once()
            self.assertEqual(loop._stale_streak, 0)
            self.assertEqual(loop._effective_probability(), 1.0)

    def test_working_context_injected_into_subconscious_prompt(self):
        from xagent.core.working_context import WorkingContextView

        agent = self._make_agent_mock()
        agent._working_context_for_turn = AsyncMock(
            return_value=WorkingContextView(
                summary="Earlier we discussed Redis cluster failover.",
                covers_through_cursor=18,
            )
        )
        self._set_model_json(agent, {
            "internal_content": "Still thinking about the failover edge case.",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
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
        self._set_model_json(agent, {
            "internal_content": "",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            agent.memory_handler.get_notebook_context.assert_awaited_once()
            query = agent.memory_handler.get_notebook_context.call_args.kwargs["current_text"]
            self.assertIn("Raft consensus election timeout", query)
            self.assertIn("Check the leader lease", query)
            self.assertNotIn("Yearly macro keywords", query)
            self.assertNotIn("Monthly recap", query)
            self.assertNotIn("Weekly digest", query)

    def test_maybe_think_skips_when_inbox_busy_or_pending(self):
        agent = self._make_agent_mock()
        agent.inbox = MagicMock()
        agent.inbox.busy = True
        agent.inbox.pending_count = 0

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            result = asyncio.run(loop.maybe_think())
            self.assertIsNone(result)
            self.assertEqual(len(agent.model_client.calls), 0)

            agent.inbox.busy = False
            agent.inbox.pending_count = 2
            result = asyncio.run(loop.maybe_think())
            self.assertIsNone(result)
            self.assertEqual(len(agent.model_client.calls), 0)

    def test_recent_interaction_does_not_suppress_reflection(self):
        agent = self._make_agent_mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            mem_dir = Path(tmpdir) / AgentConfig.MEMORY_DIRNAME
            mem_dir.mkdir(parents=True, exist_ok=True)
            (mem_dir / ".last_interaction").write_text(str(time.time() - 10.0), encoding="utf-8")

            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            result = asyncio.run(loop.maybe_think())
            self.assertIsNone(result)
            self.assertEqual(len(agent.model_client.calls), 1)
            self.assertTrue((mem_dir / ".last_interaction").is_file())

    def test_habituation_state_persisted_and_restored_across_restarts(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "Still quiet.",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })
        agent.message_storage = MagicMock()
        agent.message_storage.get_latest_message_cursor = AsyncMock(return_value=55)

        with tempfile.TemporaryDirectory() as tmpdir:
            loop1 = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop1._probability = 1.0
            loop1._last_experience_cursor = 55
            loop1._stale_streak = 0
            asyncio.run(loop1.maybe_think())
            self.assertEqual(loop1._stale_streak, 1)

            loop2 = SubconsciousLoop(agent, workspace=Path(tmpdir))
            self.assertEqual(loop2._stale_streak, 1)
            self.assertEqual(loop2._last_experience_cursor, 55)

    def test_worthy_delivery_advances_cursor_to_prevent_self_excitation(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "Send a quick follow-up to 张三.",
            "worthy": True,
            "recipient_hint": "张三",
            "external_content": "Hey 张三!",
        })
        cursor_box = [3]

        async def get_cursor():
            return cursor_box[0]

        async def fake_delivery(_delivery):
            cursor_box[0] = 4

        agent.message_storage = MagicMock()
        agent.message_storage.get_latest_message_cursor = AsyncMock(side_effect=get_cursor)

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=fake_delivery,
                deliverable_channels={"weixin"},
            )
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0
            loop._last_experience_cursor = 3

            asyncio.run(loop.maybe_think())
            self.assertEqual(loop._last_experience_cursor, 4)

    def test_unanswered_outbound_informs_without_blocking_followup(self):
        agent = self._make_agent_mock()
        self._set_model_json(agent, {
            "internal_content": "Reach out to 张三.",
            "worthy": True,
            "recipient_hint": "wx_123",
            "external_content": "First proactive ping!",
        })
        delivery_sink = AsyncMock()
        agent.message_storage = MagicMock()
        agent.message_storage.get_latest_message_cursor = AsyncMock(return_value=10)

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"weixin"},
            )
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            loop._probability = 1.0
            loop._last_experience_cursor = 10

            # First thought delivers and records 1 unanswered outbound
            asyncio.run(loop.maybe_think())
            self.assertEqual(delivery_sink.await_count, 1)
            self.assertEqual(loop._unanswered_outbound["weixin:wx_123"], 1)
            self.assertEqual(len(loop._filter_deliverable_contacts(load_contacts(loop.contacts_file))), 1)
            self.assertTrue(loop._is_deliverable_relationship_key("weixin:wx_123"))

            # A different, model-selected follow-up is allowed without a reply.
            self._set_model_json(agent, {
                "internal_content": "Reach out to 张三 again.",
                "worthy": True,
                "recipient_hint": "wx_123",
                "external_content": "Second proactive ping!",
            })
            asyncio.run(loop.maybe_think())
            self.assertEqual(delivery_sink.await_count, 2)
            self.assertEqual(loop._unanswered_outbound["weixin:wx_123"], 2)
            self.assertEqual(
                agent.memory_handler.get_relationship_context.call_args.kwargs["unanswered_outbound"],
                {"weixin:wx_123": 1},
            )
            restored = SubconsciousLoop(agent, workspace=Path(tmpdir), deliverable_channels={"weixin"})
            self.assertEqual(restored._unanswered_outbound["weixin:wx_123"], 2)
            self.assertEqual(len(restored._filter_deliverable_contacts(load_contacts(restored.contacts_file))), 1)

            # A new human interaction clears only the delivery observation.
            loop.record_interaction(
                channel="weixin",
                user_id="wx_123",
                target={"user_id": "wx_123", "sender_name": "张三"},
            )
            self.assertNotIn("weixin:wx_123", loop._unanswered_outbound)

    def test_changed_conversation_holds_back_generated_message(self):
        for change in ("cursor", "busy", "pending", "routing"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmpdir:
                agent = self._make_agent_mock()
                agent.inbox.busy = False
                agent.inbox.pending_count = 0
                cursor = [10]
                agent.message_storage.get_latest_message_cursor = AsyncMock(side_effect=lambda: cursor[0])
                sink = AsyncMock()
                loop = SubconsciousLoop(
                    agent, workspace=Path(tmpdir), probability=1,
                    delivery_sink=sink, deliverable_channels={"api"},
                )
                loop.record_interaction("api", "alice", {"user_id": "alice"})

                async def reflect():
                    if change == "cursor":
                        cursor[0] += 1  # A complete new turn can end before reflection returns.
                    elif change == "busy":
                        agent.inbox.busy = True
                    elif change == "pending":
                        agent.inbox.pending_count = 1
                    return {
                        "internal_content": "A useful thought.", "worthy": True,
                        "external_content": "An outdated follow-up.", "recipient_hint": "alice",
                    }

                loop._generate_subconscious_thought = reflect
                if change == "routing":
                    async def lookup(contacts):
                        cursor[0] += 1
                        return contacts
                    loop._alias_contacts_for_routing = lookup
                asyncio.run(loop.maybe_think())
                sink.assert_not_awaited()
                agent.record_subconscious_thought.assert_awaited_once_with(
                    "A useful thought.\nI didn't send this."
                )
                self.assertEqual(loop._unanswered_outbound, {})
                self.assertEqual(loop._last_experience_cursor, 10)

    def test_missing_cursor_uses_busy_check_without_disabling_delivery(self):
        for busy in (False, True):
            with self.subTest(busy=busy), tempfile.TemporaryDirectory() as tmpdir:
                agent = self._make_agent_mock()
                agent.inbox.busy = False
                agent.inbox.pending_count = 0
                sink = AsyncMock()
                loop = SubconsciousLoop(
                    agent, workspace=Path(tmpdir), probability=1,
                    delivery_sink=sink, deliverable_channels={"api"},
                )
                loop._current_experience_cursor = AsyncMock(return_value=None)
                loop.record_interaction("api", "alice", {"user_id": "alice"})

                async def reflect():
                    agent.inbox.busy = busy
                    return {
                        "internal_content": "Still useful.", "worthy": True,
                        "external_content": "A fresh follow-up.", "recipient_hint": "alice",
                    }

                loop._generate_subconscious_thought = reflect
                asyncio.run(loop.maybe_think())
                self.assertEqual(sink.await_count, 0 if busy else 1)

    def test_scheduled_work_does_not_count_as_a_human_reply(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(self._make_agent_mock(), workspace=Path(tmpdir))
            loop._record_unanswered_outbound("api", "Alice")
            loop._sync_unanswered_from_recent_messages([Message(
                role=RoleType.USER, sender_id="Alice", channel="api", content="Run this reminder.",
                metadata={"source": "scheduled_task"},
            )])
            self.assertEqual(loop._unanswered_outbound, {"api:Alice": 1})

    def test_contact_deliverable_callback_filters_unroutable_contacts(self):
        agent = self._make_agent_mock()
        delivery_sink = AsyncMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=delivery_sink,
                deliverable_channels={"weixin"},
                contact_deliverable=lambda c: c.user_id == "wx_routable",
            )
            loop.record_interaction(
                channel="weixin",
                user_id="wx_routable",
                target={"user_id": "wx_routable", "sender_name": "可投递"},
            )
            loop.record_interaction(
                channel="weixin",
                user_id="wx_missing_token",
                target={"user_id": "wx_missing_token", "sender_name": "缺令牌"},
            )
            deliverable = loop._filter_deliverable_contacts(load_contacts(loop.contacts_file))
            self.assertEqual([c.user_id for c in deliverable], ["wx_routable"])

    def test_duplicate_subconscious_thought_is_not_written_twice(self):
        agent = self._make_agent_mock()
        self._set_model_events(agent, [
            [self._json_event({
                "internal_content": "The rain has not stopped all afternoon.",
                "worthy": False,
                "recipient_hint": None,
                "external_content": None,
            })],
            [self._json_event({
                "internal_content": "The   rain has not stopped all afternoon.",
                "worthy": False,
                "recipient_hint": None,
                "external_content": None,
            })],
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())
            asyncio.run(loop.maybe_think())

            agent.record_subconscious_thought.assert_called_once_with(
                "The rain has not stopped all afternoon."
            )

    def test_deliver_with_retries_fails_fast_on_value_error(self):
        agent = self._make_agent_mock()
        calls = 0

        async def bad_sink(_delivery):
            nonlocal calls
            calls += 1
            raise ValueError("no cached context_token")

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(
                agent,
                workspace=Path(tmpdir),
                delivery_sink=bad_sink,
                deliverable_channels={"weixin"},
            )
            loop.record_interaction("weixin", "wx_123", {"user_id": "wx_123"})
            self._set_model_json(agent, {
                "internal_content": "Try sending.",
                "worthy": True,
                "recipient_hint": "wx_123",
                "external_content": "Ping!",
            })
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())
            self.assertEqual(calls, 1)

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
        self._set_model_json(agent, {
            "internal_content": "",
            "worthy": False,
            "recipient_hint": None,
            "external_content": None,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            messages = agent.model_client.calls[0]["messages"]
            recent_experience = next(
                msg for msg in messages if msg.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME
            )
            self.assertIn("[to=张三(wx_123)]", recent_experience["content"])


if __name__ == "__main__":
    unittest.main()

