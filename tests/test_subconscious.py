"""Unit tests for the subconscious thought system."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from xagent.core.config import AgentConfig
from xagent.core.runtime.subconscious import (
    SUBCONSCIOUS_SOURCE,
    SUBCONSCIOUS_EVENT_TYPE,
    INTERNAL_MARKER,
    ContactEntry,
    SubconsciousLoop,
    load_contacts,
    save_contacts,
    upsert_contact,
    resolve_contacts_path,
    resolve_subconscious_tasks_dir,
)
from xagent.core.runtime.tasks import enqueue_scheduled_task, list_active_task_records


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

    def test_resolve_subconscious_tasks_dir(self):
        workspace = Path("/tmp/test_workspace")
        result = resolve_subconscious_tasks_dir(workspace)
        self.assertTrue(result.name == "subconscious_tasks")
        self.assertEqual(result.parent, workspace)


class SubconsciousLoopTests(unittest.TestCase):
    """Tests for the SubconsciousLoop class."""

    def _make_agent_mock(self) -> MagicMock:
        agent = MagicMock()
        agent.system_prompt = "You are a helpful assistant."
        memory_handler = MagicMock()
        memory_handler.get_recent_context.return_value = "Recent memory content."
        agent.memory_handler = memory_handler
        message_handler = MagicMock()
        message_handler.get_recent_messages = AsyncMock(return_value=[])
        agent.message_handler = message_handler
        model_client = AsyncMock()
        from xagent.core.config import ReplyType
        model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({
                "internal_content": "Just a thought.",
                "worthy": False,
                "reasoning": "Not helpful.",
                "recipient_hint": None,
                "external_content": None,
            }),
        )
        agent.model_client = model_client
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
                "reasoning": "Seems useful.",
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
                "reasoning": "Nothing.",
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
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
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
        from xagent.core.config import ReplyType
        from xagent.schemas import Message, RoleType

        agent = self._make_agent_mock()
        # Set up recent messages
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[
            Message(role=RoleType.USER, sender_id="alice", content="你好，今天心情怎么样？", timestamp=1716000000.0),
            Message(role=RoleType.ASSISTANT, sender_id=None, content="挺好的！", timestamp=1716000001.0),
        ])
        agent.model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({
                "internal_content": "Hmm.",
                "worthy": False,
                "reasoning": "Nothing to say.",
                "recipient_hint": None,
                "external_content": None,
            }),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            # Verify the prompt passed to the LLM includes recent experience
            call_args = agent.model_client.call.call_args
            user_message = call_args.kwargs["messages"][0]["content"]
            self.assertIn("**Recent experience:**", user_message)
            self.assertIn("[speaker=alice]", user_message)
            self.assertIn("今天心情怎么样", user_message)
            self.assertIn("[speaker=ME]", user_message)
            self.assertIn("挺好的", user_message)

    def test_recent_messages_empty_omits_section(self):
        """When there are no recent messages, the section is omitted."""
        agent = self._make_agent_mock()
        agent.message_handler.get_recent_messages = AsyncMock(return_value=[])

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            call_args = agent.model_client.call.call_args
            user_message = call_args.kwargs["messages"][0]["content"]
            self.assertNotIn("**Recent experience:**", user_message)
            self.assertIn("**Recent memories:**", user_message)

    def test_core_rules_and_identity_in_instructions(self):
        """Verify composable BASE_AGENT_* rules (without Response) and identity are passed."""
        agent = self._make_agent_mock()
        agent.system_prompt = "I am a test identity."

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            asyncio.run(loop.maybe_think())

            call_args = agent.model_client.call.call_args
            instructions = call_args.kwargs["instructions"]

            # Should have at least 3 instructions: subconscious prompt, core rules, identity
            self.assertGreaterEqual(len(instructions), 3)

            contents = [i["content"] for i in instructions]
            # Should contain the relevant building blocks (without Response)
            self.assertTrue(any("Context and Attribution" in c for c in contents))
            # Should NOT contain the Response section
            self.assertFalse(any("Deliver user-visible images" in c for c in contents))
            self.assertFalse(any("Reply to the latest speaker" in c for c in contents))

            # Check identity context is present
            identities = [i for i in instructions if i.get("name") == "identity_context"]
            self.assertEqual(len(identities), 1)
            self.assertIn("I am a test identity.", identities[0]["content"])

    def test_maybe_think_not_triggered(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 0.0
            asyncio.run(loop.maybe_think())
            agent.model_client.call.assert_not_called()

    def test_maybe_think_unworthy_writes_internal_thought(self):
        from xagent.core.config import ReplyType

        agent = self._make_agent_mock()
        agent.model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({
                "internal_content": "Hmm interesting...",
                "worthy": False,
                "reasoning": "Not worth sharing.",
                "recipient_hint": None,
                "external_content": None,
            }),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            asyncio.run(loop.maybe_think())

            agent.record_internal_thought.assert_called_once()
            call_args = agent.record_internal_thought.call_args
            self.assertEqual(call_args[0][0], "Hmm interesting...")
            self.assertIn("Not worth sharing", call_args[1].get("reasoning", ""))


    def test_nighttime_worthy_writes_internal_thought(self):
        """During quiet hours, even worthy thoughts are written as internal thoughts."""
        from xagent.core.config import ReplyType

        agent = self._make_agent_mock()
        agent.model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({
                "internal_content": "The timing matters, but this can wait.",
                "worthy": True,
                "reasoning": "This is profound, but it's 3 AM.",
                "recipient_hint": "张三",
                "external_content": "A 3 AM revelation!",
            }),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            # Add a contact so routing has a recipient
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=False):
                asyncio.run(loop.maybe_think())

            # Should have written as internal thought, NOT enqueued a task
            agent.record_internal_thought.assert_called_once()
            call_args = agent.record_internal_thought.call_args
            self.assertEqual(call_args[0][0], "The timing matters, but this can wait.")

    def test_daytime_worthy_enqueues_task(self):
        """During appropriate hours, worthy thoughts are enqueued for delivery."""
        from xagent.core.config import ReplyType

        agent = self._make_agent_mock()
        agent.model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({
                "internal_content": "This insight might help 张三 move the thread forward.",
                "worthy": True,
                "reasoning": "User should see this.",
                "recipient_hint": "张三",
                "external_content": "A daytime insight!",
            }),
        )
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

            # Should have enqueued a task, NOT written as internal thought
            agent.record_internal_thought.assert_not_called()
            # Verify the subconscious task was created
            records = list_active_task_records(loop.subconscious_tasks_dir)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].content, "A daytime insight!")
            self.assertEqual(records[0].delivery_channel, "feishu")

    def test_worthy_without_recipient_writes_internal_thought(self):
        """A worthy thought with no route records the internal thought only."""
        from xagent.core.config import ReplyType

        agent = self._make_agent_mock()
        agent.model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({
                "internal_content": "This is for someone, but I do not know who yet.",
                "worthy": True,
                "reasoning": "Useful but unroutable.",
                "recipient_hint": "张三",
                "external_content": "A routable insight.",
            }),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = SubconsciousLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            with patch.object(SubconsciousLoop, '_is_appropriate_time', return_value=True):
                asyncio.run(loop.maybe_think())

            agent.record_internal_thought.assert_called_once()
            call_args = agent.record_internal_thought.call_args
            self.assertEqual(call_args[0][0], "This is for someone, but I do not know who yet.")
            self.assertEqual(list_active_task_records(loop.subconscious_tasks_dir), [])

    def test_worthy_without_external_content_writes_internal_thought(self):
        """A worthy decision without outward wording does not enqueue an empty task."""
        from xagent.core.config import ReplyType

        agent = self._make_agent_mock()
        agent.model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({
                "internal_content": "There is a signal here, but it is not speakable yet.",
                "worthy": True,
                "reasoning": "No outward text was produced.",
                "recipient_hint": "张三",
                "external_content": None,
            }),
        )
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
            self.assertEqual(list_active_task_records(loop.subconscious_tasks_dir), [])


class SubconsciousTaskIsolationTests(unittest.TestCase):
    """Verify subconscious tasks are isolated from user-created tasks."""

    def test_subconscious_tasks_separate_directory(self):
        with tempfile.TemporaryDirectory() as workspace:
            tasks_dir = Path(workspace) / "tasks"
            subconscious_dir = Path(workspace) / "subconscious_tasks"
            tasks_dir.mkdir(parents=True)
            subconscious_dir.mkdir(parents=True)

            # Enqueue a user task
            user_task = enqueue_scheduled_task(
                task_type="message",
                content="User reminder",
                run_at=datetime(2026, 6, 22, 14, 0, 0),
                tasks_dir=tasks_dir,
                channel="web",
                target={"user_id": "web_user"},
                user_id="web_user",
            )

            # Enqueue an subconscious task in the separate directory
            insp_task = enqueue_scheduled_task(
                task_type="message",
                content="Subconscious thought",
                run_at=datetime(2026, 6, 22, 14, 0, 0),
                tasks_dir=subconscious_dir,
                channel="feishu",
                target={"chat_id": "oc_xxx"},
                user_id="ou_123",
                source={"source": "subconscious"},
            )

            # User task list should NOT include subconscious tasks
            user_records = list_active_task_records(tasks_dir)
            self.assertEqual(len(user_records), 1)
            self.assertEqual(user_records[0].content, "User reminder")

            # Subconscious task list should NOT include user tasks
            sub_records = list_active_task_records(subconscious_dir)
            self.assertEqual(len(sub_records), 1)
            self.assertEqual(sub_records[0].content, "Subconscious thought")

    def test_async_scheduler_filters_by_channel_across_dirs(self):
        """Both task and subconscious schedulers filter by channel independently."""
        async def run_test():
            with tempfile.TemporaryDirectory() as workspace:
                tasks_dir = Path(workspace) / "tasks"
                insp_dir = Path(workspace) / "subconscious_tasks"

                # User task for web channel
                enqueue_scheduled_task(
                    task_type="message", content="User web task",
                    run_at=datetime(2026, 1, 1), tasks_dir=tasks_dir,
                    channel="web", target={"user_id": "u1"}, user_id="u1",
                )
                # Subconscious task for feishu channel
                enqueue_scheduled_task(
                    task_type="message", content="Insp feishu task",
                    run_at=datetime(2026, 1, 1), tasks_dir=insp_dir,
                    channel="feishu", target={"chat_id": "oc_x"}, user_id="ou_x",
                )

                from xagent.core.runtime.tasks import AsyncTaskScheduler, list_active_task_records

                # Verify tasks are in the right directories
                self.assertEqual(len(list_active_task_records(tasks_dir)), 1)
                self.assertEqual(len(list_active_task_records(insp_dir)), 1)

        asyncio.run(run_test())


class InternalThoughtFormatTests(unittest.TestCase):
    """Verify the [internal] marker format for internal monologue."""

    def test_internal_monologue_event_type(self):
        self.assertEqual(SUBCONSCIOUS_EVENT_TYPE, "internal_monologue")
        self.assertEqual(INTERNAL_MARKER, "internal")

    def test_context_event_metadata_for_internal_thought(self):
        """Verify internal thoughts carry the right metadata."""
        from xagent.schemas import Message
        msg = Message.create_context_event(
            content="A private thought",
            source=SUBCONSCIOUS_SOURCE,
            event_type=SUBCONSCIOUS_EVENT_TYPE,
            metadata={"reasoning": "Test reasoning"},
        )
        self.assertEqual(msg.type.value, "context_event")
        self.assertEqual(msg.metadata["event_type"], "internal_monologue")
        self.assertEqual(msg.metadata["source"], "subconscious")
        self.assertEqual(msg.metadata["reasoning"], "Test reasoning")

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

        # Internal monologue
        internal = Message.create_context_event(
            content="I just realized something...",
            source=SUBCONSCIOUS_SOURCE,
            event_type=SUBCONSCIOUS_EVENT_TYPE,
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
        self.assertIn("[speaker=ME]", header)
        self.assertIn("[timestamp=", header)
        self.assertIn("[internal]", header)

    def test_experience_entry_formats_internal_as_me(self):
        """Verify _format_experience_entry uses the internal thought header."""
        from xagent.schemas import Message
        from xagent.core.handlers.message import MessageHandler

        msg = Message.create_context_event(
            content="A deep thought",
            source=SUBCONSCIOUS_SOURCE,
            event_type=SUBCONSCIOUS_EVENT_TYPE,
        )
        lines = MessageHandler._format_experience_entry("observation", msg, "A deep thought")
        self.assertEqual(len(lines), 2)
        self.assertIn("[speaker=ME]", lines[0])
        self.assertIn("[internal]", lines[0])


if __name__ == "__main__":
    unittest.main()
