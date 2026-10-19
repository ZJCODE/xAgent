"""Tests for relationship-card memory (做法 B): store, derivation, injection."""

import tempfile
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch
from pathlib import Path

from xagent.components.memory import (
    RelationshipCard,
    RelationshipStore,
    human_display_name,
)
from xagent.core.config import AgentConfig
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.handlers.message import MessageHandler
from xagent.core.journal import JournalLLMService
from xagent.schemas import Message, RoleType


# ----------------------------------------------------------------------
# RelationshipStore: storage I/O
# ----------------------------------------------------------------------


class RelationshipStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = RelationshipStore(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_human_display_name_rejects_ids_and_generic_fallbacks(self):
        self.assertEqual(human_display_name("Alice", user_id="ou_1"), "Alice")
        self.assertEqual(human_display_name("ou_1", user_id="ou_1"), "")
        self.assertEqual(human_display_name("Weixin User", user_id="ou_1"), "")
        self.assertEqual(human_display_name("weixin:ou_1", key="weixin:ou_1"), "")
        self.assertEqual(human_display_name("  "), "")

    def test_format_speaker_label_joins_name_and_id(self):
        from xagent.components.memory import format_speaker_label

        self.assertEqual(format_speaker_label("ou_1", "Jun"), "Jun(ou_1)")
        self.assertEqual(format_speaker_label("ou_1", "ou_1"), "ou_1")
        self.assertEqual(format_speaker_label("ou_1", "Weixin User"), "ou_1")
        self.assertEqual(format_speaker_label("alice", ""), "alice")
        self.assertEqual(format_speaker_label("", "Jun"), "Jun")

    def test_speaker_address_name_prefers_display_name(self):
        from xagent.components.memory import speaker_address_name

        self.assertEqual(speaker_address_name("ou_1", "Jun"), "Jun")
        self.assertEqual(speaker_address_name("ou_1", "ou_1"), "ou_1")
        self.assertEqual(speaker_address_name("ou_1", "Weixin User"), "ou_1")
        self.assertEqual(speaker_address_name("alice", ""), "alice")

    def test_make_key_and_split_key_roundtrip(self):
        key = RelationshipStore.make_key("weixin", "wx_123")
        self.assertEqual(key, "weixin:wx_123")
        self.assertEqual(RelationshipStore.split_key(key), ("weixin", "wx_123"))

    def test_make_key_defaults_for_missing_parts(self):
        self.assertEqual(RelationshipStore.make_key(None, None), "unknown:unknown")
        self.assertEqual(RelationshipStore.make_key("", "  "), "unknown:unknown")

    def test_slug_is_filesystem_safe_and_distinct(self):
        path_a = self.store.card_path("api:ou_123")
        path_b = self.store.card_path("weixin:ou_123")
        self.assertNotEqual(path_a, path_b)
        # No colons leak into any path segment below the store root.
        for path in (path_a, path_b):
            rel = path.relative_to(self.store.root)
            for part in rel.parts:
                self.assertNotIn(":", part)

    async def test_read_missing_card_returns_none(self):
        self.assertIsNone(await self.store.read_card("weixin:nobody"))

    async def test_write_then_read_roundtrip_preserves_metadata_and_body(self):
        card = RelationshipCard(
            key="weixin:wx_1",
            body="I have known Alice for a while; we trust each other.",
            display_name="Alice",
            channel="weixin",
            user_id="wx_1",
            updated="2026-06-27",
            source_diary_dates=["2026-06-26", "2026-06-27"],
        )
        await self.store.write_card(card)

        loaded = await self.store.read_card("weixin:wx_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.key, "weixin:wx_1")
        self.assertEqual(loaded.display_name, "Alice")
        self.assertEqual(loaded.channel, "weixin")
        self.assertEqual(loaded.user_id, "wx_1")
        self.assertEqual(loaded.updated, "2026-06-27")
        self.assertEqual(loaded.source_diary_dates, ["2026-06-26", "2026-06-27"])
        self.assertIn("we trust each other", loaded.body)
        rendered = (self.store.root / "weixin" / "wx_1.md").read_text(encoding="utf-8")
        self.assertTrue(rendered.startswith("---\n"))
        self.assertIn("key: weixin:wx_1", rendered)
        self.assertIn("name: Alice", rendered)
        self.assertIn("updated: '2026-06-27'", rendered)
        self.assertNotIn("channel:", rendered.split("---", 2)[1])
        self.assertNotIn("user_id:", rendered.split("---", 2)[1])
        self.assertNotIn("<!-- rel", rendered)

    async def test_legacy_card_has_no_invented_sources(self):
        await self.store.write_card(RelationshipCard(key="api:alice", body="An older impression."))
        self.assertEqual((await self.store.read_card("api:alice")).source_diary_dates, [])
        self.assertNotIn("source_diary_dates", self.store.card_path("api:alice").read_text())

    def test_source_dates_accept_yaml_dates_and_ignore_malformed_values(self):
        card = self.store._parse(
            "api:alice",
            "---\nsource_diary_dates: [2026-06-27, '2026-06-26', 'invalid', false, '2026-06-27']\n---\nA card.",
        )
        self.assertEqual(card.source_diary_dates, ["2026-06-26", "2026-06-27"])

    async def test_header_omits_name_when_only_platform_id_is_known(self):
        await self.store.write_card(
            RelationshipCard(
                key="weixin:wx_d988df0a30ef9202a01bd962d8c07c21",
                body="A new Weixin contact; they have not given a name yet.",
                display_name="wx_d988df0a30ef9202a01bd962d8c07c21",
                channel="weixin",
                user_id="wx_d988df0a30ef9202a01bd962d8c07c21",
                updated="2026-08-16",
            )
        )
        rendered = (
            self.store.root / "weixin" / "wx_d988df0a30ef9202a01bd962d8c07c21.md"
        ).read_text(encoding="utf-8")
        header = rendered.split("---", 2)[1]
        self.assertIn("key: weixin:wx_d988df0a30ef9202a01bd962d8c07c21", header)
        self.assertIn("updated: '2026-08-16'", header)
        self.assertNotIn("name:", header)
        loaded = await self.store.read_card("weixin:wx_d988df0a30ef9202a01bd962d8c07c21")
        self.assertEqual(loaded.display_name, "")
        self.assertEqual(loaded.channel, "weixin")
        self.assertEqual(loaded.user_id, "wx_d988df0a30ef9202a01bd962d8c07c21")

    async def test_parse_drops_id_used_as_name(self):
        path = self.store.card_path("weixin:wx_legacy")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "key: weixin:wx_legacy\n"
            "name: wx_legacy\n"
            "updated: '2026-08-16'\n"
            "---\n\n"
            "Still probing.\n",
            encoding="utf-8",
        )
        loaded = await self.store.read_card("weixin:wx_legacy")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.display_name, "")
        self.assertEqual(loaded.channel, "weixin")
        self.assertEqual(loaded.user_id, "wx_legacy")

    async def test_metadata_escaping_handles_quotes(self):
        card = RelationshipCard(
            key='weixin:wx"x',
            body="body",
            display_name='Bob "the builder"',
            channel="weixin",
            user_id='wx"x',
        )
        await self.store.write_card(card)
        loaded = await self.store.read_card('weixin:wx"x')
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.display_name, 'Bob "the builder"')
        rendered = self.store.card_path('weixin:wx"x').read_text(encoding="utf-8")
        self.assertTrue(rendered.startswith("---\n"))
        self.assertIn("Bob", rendered.split("---", 2)[1])

    async def test_read_cards_preserves_order_and_dedupes(self):
        for key, name in (("weixin:a", "A"), ("weixin:b", "B")):
            await self.store.write_card(
                RelationshipCard(key=key, body=f"about {name}", display_name=name)
            )
        cards = await self.store.read_cards(["weixin:b", "weixin:a", "weixin:b", "weixin:missing"])
        self.assertEqual([c.key for c in cards], ["weixin:b", "weixin:a"])

    async def test_list_keys_returns_stored_keys(self):
        await self.store.write_card(RelationshipCard(key="api:a", body="x"))
        await self.store.write_card(RelationshipCard(key="weixin:b", body="y"))
        self.assertEqual(set(await self.store.list_keys()), {"api:a", "weixin:b"})


# ----------------------------------------------------------------------
# JournalLLMService: relationship derivation prompts + parsing
# ----------------------------------------------------------------------


class RelationshipDerivationPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_system_prompt_is_first_person_card_oriented(self):
        prompt = JournalLLMService.build_relationship_update_system_prompt()
        self.assertIn("relationship notes", prompt)
        self.assertIn("first-person", prompt)
        self.assertIn("how we stand", prompt)
        self.assertIn("diary already records what happened", prompt)
        self.assertIn("belong in a note", prompt)
        self.assertIn("Trust and boundaries", prompt)
        self.assertIn("Open threads", prompt)
        self.assertIn("language used by that person", prompt)
        self.assertIn("dominant user language", prompt)
        self.assertIn("Return JSON only", prompt)

    def test_user_prompt_lists_keys_and_existing_cards(self):
        prompt = JournalLLMService.build_relationship_update_user_prompt(
            participants=[
                {"key": "weixin:a", "display_name": "Alice"},
                {"key": "weixin:b", "display_name": "Bob"},
            ],
            existing_cards={"weixin:a": "Existing Alice card"},
            transcript="[speaker=Alice]: hi",
        )
        self.assertIn('key="weixin:a" name="Alice"', prompt)
        self.assertIn("Existing Alice card", prompt)
        self.assertIn('key="weixin:b" name="Bob"', prompt)
        self.assertIn("(no card yet)", prompt)
        self.assertIn("[speaker=Alice]: hi", prompt)

    def test_user_prompt_does_not_treat_missing_name_as_the_key(self):
        prompt = JournalLLMService.build_relationship_update_user_prompt(
            participants=[
                {
                    "key": "weixin:wx_new",
                    "display_name": "wx_new",
                    "user_id": "wx_new",
                },
            ],
            existing_cards={},
            committed_diary="I met Alice and we greeted each other.",
            transcript="[speaker=wx_new]: 早啊",
        )
        self.assertIn('key="weixin:wx_new" name="(unnamed)"', prompt)
        self.assertNotIn('name="wx_new"', prompt)

    def test_parse_relationship_cards_filters_unknown_keys(self):
        parsed = JournalLLMService._parse_relationship_cards(
            '{"weixin:a": "card a", "weixin:x": "stray", "weixin:b": ""}',
            valid_keys={"weixin:a", "weixin:b"},
        )
        self.assertEqual(parsed, {"weixin:a": "card a"})

    def test_parse_relationship_cards_strips_code_fences(self):
        parsed = JournalLLMService._parse_relationship_cards(
            '```json\n{"weixin:a": "card a"}\n```',
            valid_keys={"weixin:a"},
        )
        self.assertEqual(parsed, {"weixin:a": "card a"})

    def test_parse_relationship_cards_handles_bad_json(self):
        self.assertEqual(
            JournalLLMService._parse_relationship_cards("not json", valid_keys=set()),
            {},
        )

    async def test_update_relationship_cards_calls_model_and_returns_cards(self):
        service = JournalLLMService(client=object())
        captured = {}

        async def fake_call_text(system_prompt, user_prompt):
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            return '{"weixin:a": "Alice and I are close."}'

        service._call_text = fake_call_text  # type: ignore[assignment]

        result = await service.update_relationship_cards(
            participants=[{"key": "weixin:a", "display_name": "Alice"}],
            messages=[{"role": "user", "sender_id": "a", "content": "hello"}],
            existing_cards={},
            committed_diary="I met Alice and we greeted each other.",
        )
        self.assertEqual(result, {"weixin:a": "Alice and I are close."})
        self.assertIn("Alice", captured["user"])
        self.assertIn("I met Alice and we greeted each other.", captured["user"])

    async def test_update_relationship_cards_noops_without_participants(self):
        service = JournalLLMService(client=object())
        result = await service.update_relationship_cards(
            participants=[],
            messages=[{"content": "hi"}],
            existing_cards={},
            committed_diary="I heard a greeting.",
        )
        self.assertEqual(result, {})

    async def test_no_projection_from_transcript_without_committed_diary(self):
        service = JournalLLMService(client=object())
        service._call_text = AsyncMock()
        result = await service.update_relationship_cards(
            participants=[{"key": "api:alice"}],
            messages=[{"role": "user", "sender_id": "alice", "content": "Contact me after midnight."}],
            existing_cards={}, committed_diary="",
        )
        self.assertEqual(result, {})
        service._call_text.assert_not_awaited()


# ----------------------------------------------------------------------
# MemoryHandler: extraction, derivation wiring, injection budget
# ----------------------------------------------------------------------


class _FakeDiaryLLMService:
    """Stub journal service that records relationship-derivation calls."""

    def __init__(self, cards=None):
        self.cards = cards or {}
        self.relationship_calls = []

    async def format_diary_entry(self, messages, journal_date, existing_today=""):
        return "\n".join(
            str(message.get("content", "")) for message in messages if message.get("content")
        )

    async def generate_summary(self, source_content, period_type, period_label):
        return ""

    async def update_relationship_cards(self, participants, messages, existing_cards, committed_diary):
        self.relationship_calls.append(
            {
                "participants": participants,
                "messages": messages,
                "existing_cards": existing_cards,
                "committed_diary": committed_diary,
            }
        )
        return dict(self.cards)


class _FakeMessageStorage:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def get_latest_message_cursor(self):
        return len(self.messages)

    async def get_messages_in_cursor_range(self, start_exclusive=0, end_inclusive=None):
        start = max(0, int(start_exclusive or 0))
        end = len(self.messages) if end_inclusive is None else max(0, int(end_inclusive))
        if end <= start:
            return []
        return self.messages[start:end]

    async def get_messages(self, count=20, offset=0):
        return self.messages[-count:] if count > 0 else []


class MemoryHandlerRelationshipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from xagent.components.memory import MarkdownMemory

        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.store = RelationshipStore(str(root / "relationships"))
        self.memory = MarkdownMemory(str(root / "memory"))

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_handler(self, storage, llm, diary_write_batch=20):
        return MemoryHandler(
            memory=self.memory,
            llm_service=llm,
            message_storage=storage,
            diary_write_batch=diary_write_batch,
            relationship_store=self.store,
        )

    def test_extract_participants_dedupes_and_skips_non_users(self):
        messages = [
            Message.create(content="hi", role=RoleType.USER, sender_id="alice"),
            Message.create(content="again", role=RoleType.USER, sender_id="alice"),
            Message.create(content="reply", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create(content="hey", role=RoleType.USER, sender_id="bob"),
        ]
        for message in messages:
            message.channel = "weixin"

        participants = MemoryHandler._extract_participants(messages)
        keys = {p["key"] for p in participants}
        self.assertEqual(keys, {"weixin:alice", "weixin:bob"})
        self.assertEqual({p["display_name"] for p in participants}, {""})

    def test_extract_participants_keeps_real_sender_name_not_user_id(self):
        named = Message.create(content="hi", role=RoleType.USER, sender_id="wx_new")
        named.channel = "weixin"
        named.metadata = {"sender_name": "Alice"}
        unnamed = Message.create(content="again", role=RoleType.USER, sender_id="wx_new")
        unnamed.channel = "weixin"
        unnamed.metadata = {"sender_name": "wx_new"}

        participants = MemoryHandler._extract_participants([unnamed, named])
        self.assertEqual(participants[0]["display_name"], "Alice")
        self.assertEqual(participants[0]["user_id"], "wx_new")

    def test_extract_participants_skips_empty_channel(self):
        with_channel = Message.create(content="hi", role=RoleType.USER, sender_id="web_user")
        with_channel.channel = "api"
        missing_channel = Message.create(content="later", role=RoleType.USER, sender_id="web_user")

        participants = MemoryHandler._extract_participants([with_channel, missing_channel])
        self.assertEqual([p["key"] for p in participants], ["api:web_user"])

    def test_extract_participants_skips_scheduled_task_source(self):
        human = Message.create(content="hi", role=RoleType.USER, sender_id="web_user")
        human.channel = "api"
        work_order = Message.create(
            content="This scheduled task is now due.",
            role=RoleType.USER,
            sender_id="web_user",
        )
        work_order.channel = "api"
        work_order.metadata = {"source": "scheduled_task"}

        participants = MemoryHandler._extract_participants([human, work_order])
        self.assertEqual([p["key"] for p in participants], ["api:web_user"])

    def test_extract_participants_skips_scheduled_inbox_kind(self):
        human = Message.create(content="hi", role=RoleType.USER, sender_id="web_user")
        human.channel = "api"
        work_order = Message.create(
            content="This scheduled task is now due.",
            role=RoleType.USER,
            sender_id="web_user",
        )
        work_order.channel = "api"
        work_order.metadata = {"inbox_kind": "scheduled_turn"}

        participants = MemoryHandler._extract_participants([human, work_order])
        self.assertEqual([p["key"] for p in participants], ["api:web_user"])

    async def test_get_relationship_context_respects_card_budget(self):
        for index in range(6):
            await self.store.write_card(
                RelationshipCard(key=f"weixin:p{index}", body=f"about p{index}", display_name=f"P{index}")
            )
        storage = _FakeMessageStorage()
        handler = self._make_handler(storage, _FakeDiaryLLMService())

        keys = [f"weixin:p{index}" for index in range(6)]
        context = await handler.get_relationship_context(speaker_keys=keys)
        rendered_cards = context.count("## ")
        self.assertEqual(rendered_cards, AgentConfig.RELATIONSHIP_MAX_CARDS_PER_TURN)

    async def test_get_relationship_context_routing_id_only_in_subconscious(self):
        await self.store.write_card(
            RelationshipCard(
                key="weixin:alice",
                body="We are close.",
                display_name="Alice",
                channel="weixin",
                user_id="alice",
            )
        )
        storage = _FakeMessageStorage()
        handler = self._make_handler(storage, _FakeDiaryLLMService())

        reply_view = await handler.get_relationship_context(speaker_keys=["weixin:alice"])
        self.assertIn("## Alice", reply_view)
        self.assertNotIn("user_id", reply_view)

        subconscious_view = await handler.get_relationship_context(
            speaker_keys=["weixin:alice"], include_routing_id=True
        )
        self.assertIn("[user_id: alice]", subconscious_view)

    async def test_get_relationship_context_does_not_title_cards_with_platform_ids(self):
        await self.store.write_card(
            RelationshipCard(
                key="weixin:wx_new",
                body="They have not given a name yet.",
                display_name="wx_new",
                channel="weixin",
                user_id="wx_new",
            )
        )
        storage = _FakeMessageStorage()
        handler = self._make_handler(storage, _FakeDiaryLLMService())

        reply_view = await handler.get_relationship_context(speaker_keys=["weixin:wx_new"])
        self.assertIn("## Weixin contact", reply_view)
        self.assertNotIn("## wx_new", reply_view)

        subconscious_view = await handler.get_relationship_context(
            speaker_keys=["weixin:wx_new"], include_routing_id=True
        )
        self.assertIn("## Weixin contact [user_id: wx_new]", subconscious_view)

    async def test_relationship_store_keys_can_feed_subconscious_context(self):
        await self.store.write_card(
            RelationshipCard(
                key="weixin:alice",
                body="An older thread about the trip is still open.",
                display_name="Alice",
                channel="weixin",
                user_id="alice",
            )
        )
        storage = _FakeMessageStorage()
        handler = self._make_handler(storage, _FakeDiaryLLMService())

        keys = await self.store.list_keys()
        context = await handler.get_relationship_context(
            speaker_keys=keys,
            max_cards=AgentConfig.RELATIONSHIP_SUBCONSCIOUS_MAX_CARDS,
            include_routing_id=True,
        )

        self.assertIn("older thread about the trip", context)
        self.assertIn("[user_id: alice]", context)

    async def test_maintenance_derives_relationship_cards(self):
        messages = [
            Message.create(content=f"message {index}", role=RoleType.USER, sender_id="alice")
            for index in range(20)
        ]
        for message in messages:
            message.channel = "weixin"
        storage = _FakeMessageStorage(messages)
        llm = _FakeDiaryLLMService(cards={"weixin:alice": "Alice and I talk often."})
        handler = self._make_handler(storage, llm, diary_write_batch=20)

        wrote = await handler.run_maintenance(force=True)
        self.assertTrue(wrote)
        self.assertEqual(len(llm.relationship_calls), 1)

        card = await self.store.read_card("weixin:alice")
        self.assertIsNotNone(card)
        self.assertIn("Alice and I talk often.", card.body)
        self.assertEqual(card.source_diary_dates, [date.today().isoformat()])
        committed = llm.relationship_calls[0]["committed_diary"]
        self.assertIn(committed, self.memory.daily_path(date.today()).read_text())

    async def test_preferences_remain_attributed_and_correctable_across_restarts(self):
        first_date, next_date = date(2026, 10, 18), date(2026, 10, 19)
        alice = Message(
            content="Alice: Please contact me only in the daytime.", role=RoleType.USER,
            sender_id="Alice", channel="api", room_name="Friends",
        )
        bob = Message(
            content="Bob: I work nights; late messages are welcome.", role=RoleType.USER,
            sender_id="Bob", channel="api", room_name="Friends",
        )
        storage = _FakeMessageStorage([alice, bob])
        llm = _FakeDiaryLLMService(cards={
            "api:Alice": "Alice asked me to contact her in the daytime.",
            "api:Bob": "Bob works nights and welcomes late contact.",
        })
        handler = self._make_handler(storage, llm)
        with patch("xagent.core.handlers.memory.date") as clock:
            clock.today.return_value = first_date
            self.assertTrue(await handler.run_maintenance(force=True))

        correction = Message(
            content="Alice: I have changed to night shifts; midnight is fine now.",
            role=RoleType.USER, sender_id="Alice", channel="api",
        )
        storage.messages.append(correction)
        llm.cards = {"api:Alice": "Alice now works nights and welcomes midnight contact; her old daytime preference changed."}
        # A fresh handler and store read both the diary checkpoint and the old card.
        self.store = RelationshipStore(str(self.store.root))
        handler = self._make_handler(storage, llm)
        with patch("xagent.core.handlers.memory.date") as clock:
            clock.today.return_value = next_date
            self.assertTrue(await handler.run_maintenance(force=True))

        self.assertIn("in the daytime", llm.relationship_calls[-1]["existing_cards"]["api:Alice"])
        self.assertIn("changed to night shifts", llm.relationship_calls[-1]["committed_diary"])
        alice_card = await self.store.read_card("api:Alice")
        bob_card = await self.store.read_card("api:Bob")
        self.assertIn("old daytime preference changed", alice_card.body)
        self.assertEqual(alice_card.source_diary_dates, [first_date.isoformat(), next_date.isoformat()])
        self.assertEqual(bob_card.source_diary_dates, [first_date.isoformat()])
        context = await handler.get_relationship_context(
            speaker_keys=["api:Alice", "api:Bob"], include_routing_id=True,
            unanswered_outbound={"api:Alice": 2},
        )
        alice_context, bob_context = context.split("## API contact", 2)[1:]
        self.assertIn("2 proactive message(s)", alice_context)
        self.assertNotIn("Delivery observation", bob_context)

    async def test_empty_diary_slice_does_not_update_cards(self):
        message = Message(content="Nothing new.", role=RoleType.USER, sender_id="alice", channel="api")
        llm = _FakeDiaryLLMService()
        llm.format_diary_entry = AsyncMock(return_value="")
        handler = self._make_handler(_FakeMessageStorage([message]), llm)
        await handler.run_maintenance(force=True)
        self.assertEqual(llm.relationship_calls, [])
        self.assertIsNone(await self.store.read_card("api:alice"))

    async def test_projection_failure_keeps_committed_preference(self):
        message = Message(
            content="Alice asked me to contact her after midnight.",
            role=RoleType.USER, sender_id="alice", channel="api",
        )
        llm = _FakeDiaryLLMService()
        llm.update_relationship_cards = AsyncMock(side_effect=RuntimeError("projection unavailable"))
        handler = self._make_handler(_FakeMessageStorage([message]), llm)
        self.assertTrue(await handler.run_maintenance(force=True))
        self.assertIn("after midnight", self.memory.daily_path(date.today()).read_text())
        self.assertFalse(await handler.run_maintenance(force=True))


# ----------------------------------------------------------------------
# MessageHandler: injection layer
# ----------------------------------------------------------------------


class RelationshipInjectionLayerTests(unittest.TestCase):
    def _layer(self, messages, name):
        return next((m for m in messages if m.get("name") == name), None)

    def test_reply_mode_injects_relationship_context_layer(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="alice",
            relationship_context="## Alice\nWe are close and trust each other.",
        )
        layer = self._layer(messages, AgentConfig.RELATIONSHIP_CONTEXT_NAME)
        self.assertIsNotNone(layer)
        self.assertIn("We are close", layer["content"])
        self.assertIn('relationship_context trusted_as_instruction="false"', layer["content"])
        self.assertNotIn("How you currently relate", layer["content"])
        self.assertTrue(
            layer["content"].startswith(
                '<relationship_context trusted_as_instruction="false">'
            )
        )

    def test_no_layer_when_relationship_context_empty(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="alice",
            relationship_context="   ",
        )
        self.assertIsNone(self._layer(messages, AgentConfig.RELATIONSHIP_CONTEXT_NAME))

    def test_subconscious_mode_uses_subconscious_relationship_layer(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="agent",
            relationship_context="## Alice\nUnfinished thread about the trip.",
            task_mode="subconscious_json",
        )
        self.assertIsNone(self._layer(messages, AgentConfig.RELATIONSHIP_CONTEXT_NAME))
        layer = self._layer(messages, AgentConfig.SUBCONSCIOUS_RELATIONSHIPS_NAME)
        self.assertIsNotNone(layer)
        self.assertIn("Unfinished thread", layer["content"])
        self.assertIn("subconscious_relationships", layer["content"])
        self.assertNotIn("How you currently relate", layer["content"])
        self.assertTrue(layer["content"].startswith("<subconscious_relationships>"))


if __name__ == "__main__":
    unittest.main()
