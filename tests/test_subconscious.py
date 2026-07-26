from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from xagent.core.config import AgentConfig
from xagent.core.runtime import AgentEvent
from xagent.core.runtime.subconscious import ContactEntry, SubconsciousLoop


class SubconsciousLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_is_disabled_by_default(self):
        loop = SubconsciousLoop(
            object(),
            event_sink=AsyncMock(),
            probability=AgentConfig.SUBCONSCIOUS_ACTIVITY,
        )
        self.assertFalse(loop.should_trigger())

    def test_parser_accepts_fenced_json_and_fails_closed(self):
        parsed = SubconsciousLoop._parse_subconscious_json(
            '```json\n{"internal_content":"x","worthy":false}\n```'
        )
        self.assertEqual(parsed["internal_content"], "x")
        self.assertFalse(parsed["worthy"])

        fallback = SubconsciousLoop._parse_subconscious_json("not json")
        self.assertFalse(fallback["worthy"])
        self.assertEqual(fallback["internal_content"], "not json")

    def test_recipient_requires_an_explicit_match_when_hint_is_present(self):
        contacts = [
            ContactEntry(
                channel="feishu",
                user_id="ou_alice",
                target={"sender_name": "Alice"},
                last_seen="2026-07-25T12:00:00",
            )
        ]
        self.assertIs(
            SubconsciousLoop._pick_recipient(contacts, "ou_alice"),
            contacts[0],
        )
        self.assertIsNone(SubconsciousLoop._pick_recipient(contacts, "Bob"))

    async def test_trigger_is_persisted_before_any_model_work(self):
        event_sink = AsyncMock()
        loop = SubconsciousLoop(object(), event_sink=event_sink, probability=1.0)
        loop._generate_subconscious_thought = AsyncMock()

        await loop.maybe_submit()

        event_sink.assert_awaited_once()
        event = event_sink.await_args.args[0]
        self.assertEqual(event.kind, "subconscious")
        loop._generate_subconscious_thought.assert_not_awaited()

    async def test_persisted_event_uses_only_sqlite_supplied_contacts(self):
        agent = type(
            "AgentStub",
            (),
            {"record_subconscious_thought": AsyncMock()},
        )()
        contact = ContactEntry(
            channel="feishu",
            user_id="ou_alice",
            target={"chat_id": "oc_chat", "sender_name": "Alice"},
            last_seen="2026-07-25T12:00:00",
        )
        sink = AsyncMock()
        before_side_effect = AsyncMock()
        loop = SubconsciousLoop(
            agent,
            event_sink=AsyncMock(),
            probability=1.0,
            contacts_provider=lambda: [contact],
            delivery_sink=sink,
            before_side_effect=before_side_effect,
            deliverable_channels={"feishu"},
        )
        loop._generate_subconscious_thought = AsyncMock(
            return_value={
                "internal_content": "private",
                "worthy": True,
                "recipient_hint": "ou_alice",
                "external_content": "hello",
            }
        )

        with patch.object(SubconsciousLoop, "_is_appropriate_time", return_value=True):
            result = await loop.process_event(
                AgentEvent.create(
                    event_id="subconscious-event",
                    kind="subconscious",
                    source="runtime",
                    speaker_id="agent",
                    content="heartbeat",
                )
            )

        agent.record_subconscious_thought.assert_awaited_once_with("private")
        sink.assert_awaited_once()
        self.assertGreaterEqual(before_side_effect.await_count, 1)
        self.assertTrue(
            all(call.args == ("subconscious-event",) for call in before_side_effect.await_args_list)
        )
        self.assertEqual(sink.await_args.args[0].event_id, "subconscious-event")
        self.assertEqual(sink.await_args.args[0].recipient, contact)
        self.assertTrue(result["delivery_created"])
