"""Unit tests for the subconscious inspiration system."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from xagent.core.runtime.inspiration import (
    INSPIRATION_SOURCE,
    INSPIRATION_EVENT_TYPE,
    INTERNAL_MARKER,
    ContactEntry,
    InspirationLoop,
    load_contacts,
    save_contacts,
    upsert_contact,
    resolve_contacts_path,
    resolve_inspiration_tasks_dir,
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

    def test_resolve_inspiration_tasks_dir(self):
        workspace = Path("/tmp/test_workspace")
        result = resolve_inspiration_tasks_dir(workspace)
        self.assertTrue(result.name == "inspiration_tasks")
        self.assertEqual(result.parent, workspace)


class InspirationLoopTests(unittest.TestCase):
    """Tests for the InspirationLoop class."""

    def _make_agent_mock(self) -> MagicMock:
        agent = MagicMock()
        memory_handler = MagicMock()
        memory_handler.get_recent_context.return_value = "Recent memory content."
        agent.memory_handler = memory_handler
        model_client = AsyncMock()
        from xagent.core.config import ReplyType
        model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({"worthy": False, "content": "Just a thought.", "reasoning": "Not helpful."}),
        )
        agent.model_client = model_client
        agent.record_internal_thought = AsyncMock()
        return agent

    def test_should_trigger_disabled(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = InspirationLoop(agent, workspace=Path(tmpdir))
            loop._enabled = False
            for _ in range(100):
                self.assertFalse(loop.should_trigger())

    def test_should_trigger_probability(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = InspirationLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0
            for _ in range(20):
                self.assertTrue(loop.should_trigger())

            loop._probability = 0.0
            for _ in range(20):
                self.assertFalse(loop.should_trigger())

    def test_parse_inspiration_json_plain(self):
        result = InspirationLoop._parse_inspiration_json(
            '{"worthy": true, "content": "Hello!", "reasoning": "Seems useful."}'
        )
        self.assertTrue(result["worthy"])
        self.assertEqual(result["content"], "Hello!")

    def test_parse_inspiration_json_with_code_fence(self):
        result = InspirationLoop._parse_inspiration_json(
            '```json\n{"worthy": false, "content": "Nah.", "reasoning": "Nothing."}\n```'
        )
        self.assertFalse(result["worthy"])
        self.assertEqual(result["content"], "Nah.")

    def test_parse_inspiration_json_fallback(self):
        result = InspirationLoop._parse_inspiration_json("Just a random string")
        self.assertFalse(result.get("worthy"))

    def test_is_appropriate_time(self):
        morning = datetime(2026, 6, 22, 7, 0, 0)
        self.assertFalse(InspirationLoop._is_appropriate_time(morning))

        daytime = datetime(2026, 6, 22, 12, 0, 0)
        self.assertTrue(InspirationLoop._is_appropriate_time(daytime))

        evening = datetime(2026, 6, 22, 23, 0, 0)
        self.assertFalse(InspirationLoop._is_appropriate_time(evening))

        early_morning = datetime(2026, 6, 22, 8, 0, 0)
        self.assertTrue(InspirationLoop._is_appropriate_time(early_morning))

    def test_next_appropriate_time(self):
        # Late night → next day 9 AM
        late = datetime(2026, 6, 22, 23, 0, 0)
        result = InspirationLoop._next_appropriate_time(late)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 0)
        self.assertEqual(result.day, 23)

        # Early morning → today 9 AM
        early = datetime(2026, 6, 22, 3, 0, 0)
        result = InspirationLoop._next_appropriate_time(early)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.day, 22)

    def test_record_interaction(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = InspirationLoop(agent, workspace=Path(tmpdir))
            loop.record_interaction(
                channel="feishu",
                user_id="ou_123",
                target={"chat_id": "oc_xxx", "sender_name": "张三"},
            )
            contacts = load_contacts(loop.contacts_file)
            self.assertEqual(len(contacts), 1)
            self.assertEqual(contacts[0].channel, "feishu")

    def test_maybe_inspire_not_triggered(self):
        agent = self._make_agent_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = InspirationLoop(agent, workspace=Path(tmpdir))
            loop._probability = 0.0
            asyncio.run(loop.maybe_inspire())
            agent.model_client.call.assert_not_called()

    def test_maybe_inspire_unworthy_writes_internal_thought(self):
        from xagent.core.config import ReplyType

        agent = self._make_agent_mock()
        agent.model_client.call.return_value = (
            ReplyType.SIMPLE_REPLY,
            json.dumps({"worthy": False, "content": "Hmm interesting...", "reasoning": "Not worth sharing."}),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = InspirationLoop(agent, workspace=Path(tmpdir))
            loop._probability = 1.0

            asyncio.run(loop.maybe_inspire())

            agent.record_internal_thought.assert_called_once()
            call_args = agent.record_internal_thought.call_args
            self.assertEqual(call_args[0][0], "Hmm interesting...")
            self.assertIn("Not worth sharing", call_args[1].get("reasoning", ""))


class InspirationTaskIsolationTests(unittest.TestCase):
    """Verify inspiration tasks are isolated from user-created tasks."""

    def test_inspiration_tasks_separate_directory(self):
        with tempfile.TemporaryDirectory() as workspace:
            tasks_dir = Path(workspace) / "tasks"
            inspiration_dir = Path(workspace) / "inspiration_tasks"
            tasks_dir.mkdir(parents=True)
            inspiration_dir.mkdir(parents=True)

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

            # Enqueue an inspiration task in the separate directory
            insp_task = enqueue_scheduled_task(
                task_type="message",
                content="Inspiration thought",
                run_at=datetime(2026, 6, 22, 14, 0, 0),
                tasks_dir=inspiration_dir,
                channel="feishu",
                target={"chat_id": "oc_xxx"},
                user_id="ou_123",
                source={"source": "inspiration"},
            )

            # User task list should NOT include inspiration tasks
            user_records = list_active_task_records(tasks_dir)
            self.assertEqual(len(user_records), 1)
            self.assertEqual(user_records[0].content, "User reminder")

            # Inspiration task list should NOT include user tasks
            insp_records = list_active_task_records(inspiration_dir)
            self.assertEqual(len(insp_records), 1)
            self.assertEqual(insp_records[0].content, "Inspiration thought")

    def test_async_scheduler_filters_by_channel_across_dirs(self):
        """Both task and inspiration schedulers filter by channel independently."""
        async def run_test():
            with tempfile.TemporaryDirectory() as workspace:
                tasks_dir = Path(workspace) / "tasks"
                insp_dir = Path(workspace) / "inspiration_tasks"

                # User task for web channel
                enqueue_scheduled_task(
                    task_type="message", content="User web task",
                    run_at=datetime(2026, 1, 1), tasks_dir=tasks_dir,
                    channel="web", target={"user_id": "u1"}, user_id="u1",
                )
                # Inspiration task for feishu channel
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
        self.assertEqual(INSPIRATION_EVENT_TYPE, "internal_monologue")
        self.assertEqual(INTERNAL_MARKER, "internal")

    def test_context_event_metadata_for_internal_thought(self):
        """Verify internal thoughts carry the right metadata."""
        from xagent.schemas import Message
        msg = Message.create_context_event(
            content="A private thought",
            source=INSPIRATION_SOURCE,
            event_type=INSPIRATION_EVENT_TYPE,
            metadata={"reasoning": "Test reasoning"},
        )
        self.assertEqual(msg.type.value, "context_event")
        self.assertEqual(msg.metadata["event_type"], "internal_monologue")
        self.assertEqual(msg.metadata["source"], "inspiration")
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
            source=INSPIRATION_SOURCE,
            event_type=INSPIRATION_EVENT_TYPE,
        )
        self.assertTrue(MessageHandler._is_internal_monologue(internal))

    def test_internal_thought_header_format(self):
        """Verify the header format for internal thoughts."""
        from xagent.schemas import Message
        from xagent.core.handlers.message import MessageHandler

        msg = Message.create_context_event(
            content="Thinking...",
            source=INSPIRATION_SOURCE,
            event_type=INSPIRATION_EVENT_TYPE,
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
            source=INSPIRATION_SOURCE,
            event_type=INSPIRATION_EVENT_TYPE,
        )
        lines = MessageHandler._format_experience_entry("observation", msg, "A deep thought")
        self.assertEqual(len(lines), 2)
        self.assertIn("[speaker=ME]", lines[0])
        self.assertIn("[internal]", lines[0])


if __name__ == "__main__":
    unittest.main()
