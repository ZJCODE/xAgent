"""Tests for relationship-card memory (做法 B): store, derivation, injection."""

import tempfile
import unittest
from pathlib import Path

from xagent.components.memory import RelationshipCard, RelationshipStore
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

    def test_make_key_and_split_key_roundtrip(self):
        key = RelationshipStore.make_key("feishu", "ou_123")
        self.assertEqual(key, "feishu:ou_123")
        self.assertEqual(RelationshipStore.split_key(key), ("feishu", "ou_123"))

    def test_make_key_defaults_for_missing_parts(self):
        self.assertEqual(RelationshipStore.make_key(None, None), "unknown:unknown")
        self.assertEqual(RelationshipStore.make_key("", "  "), "unknown:unknown")

    def test_slug_is_filesystem_safe_and_distinct(self):
        path_a = self.store.card_path("feishu:ou_123")
        path_b = self.store.card_path("weixin:ou_123")
        self.assertNotEqual(path_a, path_b)
        # No colons leak into any path segment below the store root.
        for path in (path_a, path_b):
            rel = path.relative_to(self.store.root)
            for part in rel.parts:
                self.assertNotIn(":", part)

    async def test_read_missing_card_returns_none(self):
        self.assertIsNone(await self.store.read_card("feishu:nobody"))

    async def test_write_then_read_roundtrip_preserves_metadata_and_body(self):
        card = RelationshipCard(
            key="feishu:ou_1",
            body="I have known Alice for a while; we trust each other.",
            display_name="Alice",
            channel="feishu",
            user_id="ou_1",
            updated="2026-06-27",
        )
        await self.store.write_card(card)

        loaded = await self.store.read_card("feishu:ou_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.key, "feishu:ou_1")
        self.assertEqual(loaded.display_name, "Alice")
        self.assertEqual(loaded.channel, "feishu")
        self.assertEqual(loaded.user_id, "ou_1")
        self.assertEqual(loaded.updated, "2026-06-27")
        self.assertIn("we trust each other", loaded.body)

    async def test_metadata_escaping_handles_quotes(self):
        card = RelationshipCard(
            key='feishu:ou"x',
            body="body",
            display_name='Bob "the builder"',
            channel="feishu",
            user_id='ou"x',
        )
        await self.store.write_card(card)
        loaded = await self.store.read_card('feishu:ou"x')
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.display_name, 'Bob "the builder"')

    async def test_read_cards_preserves_order_and_dedupes(self):
        for key, name in (("feishu:a", "A"), ("feishu:b", "B")):
            await self.store.write_card(
                RelationshipCard(key=key, body=f"about {name}", display_name=name)
            )
        cards = await self.store.read_cards(["feishu:b", "feishu:a", "feishu:b", "feishu:missing"])
        self.assertEqual([c.key for c in cards], ["feishu:b", "feishu:a"])

    async def test_list_keys_returns_stored_keys(self):
        await self.store.write_card(RelationshipCard(key="feishu:a", body="x"))
        await self.store.write_card(RelationshipCard(key="weixin:b", body="y"))
        self.assertEqual(set(await self.store.list_keys()), {"feishu:a", "weixin:b"})


# ----------------------------------------------------------------------
# JournalLLMService: relationship derivation prompts + parsing
# ----------------------------------------------------------------------


class RelationshipDerivationPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_system_prompt_is_first_person_card_oriented(self):
        prompt = JournalLLMService.build_relationship_update_system_prompt()
        self.assertIn("relationship notes", prompt)
        self.assertIn("first-person", prompt)
        self.assertIn("Trust and boundaries", prompt)
        self.assertIn("Open threads", prompt)
        self.assertIn("Return JSON only", prompt)

    def test_user_prompt_lists_keys_and_existing_cards(self):
        prompt = JournalLLMService.build_relationship_update_user_prompt(
            participants=[
                {"key": "feishu:a", "display_name": "Alice"},
                {"key": "feishu:b", "display_name": "Bob"},
            ],
            existing_cards={"feishu:a": "Existing Alice card"},
            transcript="[speaker=Alice]: hi",
        )
        self.assertIn('key="feishu:a" name="Alice"', prompt)
        self.assertIn("Existing Alice card", prompt)
        self.assertIn('key="feishu:b" name="Bob"', prompt)
        self.assertIn("(no card yet)", prompt)
        self.assertIn("[speaker=Alice]: hi", prompt)

    def test_parse_relationship_cards_filters_unknown_keys(self):
        parsed = JournalLLMService._parse_relationship_cards(
            '{"feishu:a": "card a", "feishu:x": "stray", "feishu:b": ""}',
            valid_keys={"feishu:a", "feishu:b"},
        )
        self.assertEqual(parsed, {"feishu:a": "card a"})

    def test_parse_relationship_cards_strips_code_fences(self):
        parsed = JournalLLMService._parse_relationship_cards(
            '```json\n{"feishu:a": "card a"}\n```',
            valid_keys={"feishu:a"},
        )
        self.assertEqual(parsed, {"feishu:a": "card a"})

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
            return '{"feishu:a": "Alice and I are close."}'

        service._call_text = fake_call_text  # type: ignore[assignment]

        result = await service.update_relationship_cards(
            participants=[{"key": "feishu:a", "display_name": "Alice"}],
            messages=[{"role": "user", "sender_id": "a", "content": "hello"}],
            existing_cards={},
        )
        self.assertEqual(result, {"feishu:a": "Alice and I are close."})
        self.assertIn("Alice", captured["user"])

    async def test_update_relationship_cards_noops_without_participants(self):
        service = JournalLLMService(client=object())
        result = await service.update_relationship_cards(
            participants=[],
            messages=[{"content": "hi"}],
            existing_cards={},
        )
        self.assertEqual(result, {})


# ----------------------------------------------------------------------
# MemoryHandler: extraction, derivation wiring, injection budget
# ----------------------------------------------------------------------


class _FakeDiaryLLMService:
    """Stub journal service that records relationship-derivation calls."""

    def __init__(self, cards=None):
        self.cards = cards or {}
        self.relationship_calls = []

    async def format_diary_entry(self, messages, journal_date):
        return "\n".join(
            str(message.get("content", "")) for message in messages if message.get("content")
        )

    async def generate_summary(self, source_content, period_type, period_label):
        return ""

    async def update_relationship_cards(self, participants, messages, existing_cards):
        self.relationship_calls.append(
            {
                "participants": participants,
                "messages": messages,
                "existing_cards": existing_cards,
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

    def _make_handler(self, storage, llm, max_history=20):
        return MemoryHandler(
            memory=self.memory,
            llm_service=llm,
            message_storage=storage,
            max_history=max_history,
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
            message.channel = "feishu"

        participants = MemoryHandler._extract_participants(messages)
        keys = {p["key"] for p in participants}
        self.assertEqual(keys, {"feishu:alice", "feishu:bob"})

    async def test_get_relationship_context_respects_card_budget(self):
        for index in range(6):
            await self.store.write_card(
                RelationshipCard(key=f"feishu:p{index}", body=f"about p{index}", display_name=f"P{index}")
            )
        storage = _FakeMessageStorage()
        handler = self._make_handler(storage, _FakeDiaryLLMService())

        keys = [f"feishu:p{index}" for index in range(6)]
        context = await handler.get_relationship_context(speaker_keys=keys)
        rendered_cards = context.count("## ")
        self.assertEqual(rendered_cards, AgentConfig.RELATIONSHIP_MAX_CARDS_PER_TURN)

    async def test_get_relationship_context_routing_id_only_in_subconscious(self):
        await self.store.write_card(
            RelationshipCard(
                key="feishu:alice",
                body="We are close.",
                display_name="Alice",
                channel="feishu",
                user_id="alice",
            )
        )
        storage = _FakeMessageStorage()
        handler = self._make_handler(storage, _FakeDiaryLLMService())

        reply_view = await handler.get_relationship_context(speaker_keys=["feishu:alice"])
        self.assertIn("## Alice", reply_view)
        self.assertNotIn("user_id", reply_view)

        subconscious_view = await handler.get_relationship_context(
            speaker_keys=["feishu:alice"], include_routing_id=True
        )
        self.assertIn("[user_id: alice]", subconscious_view)

    async def test_relationship_store_keys_can_feed_subconscious_context(self):
        await self.store.write_card(
            RelationshipCard(
                key="feishu:alice",
                body="An older thread about the trip is still open.",
                display_name="Alice",
                channel="feishu",
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
            message.channel = "feishu"
        storage = _FakeMessageStorage(messages)
        llm = _FakeDiaryLLMService(cards={"feishu:alice": "Alice and I talk often."})
        handler = self._make_handler(storage, llm, max_history=20)

        wrote = await handler.run_maintenance(force=True)
        self.assertTrue(wrote)
        self.assertEqual(len(llm.relationship_calls), 1)

        card = await self.store.read_card("feishu:alice")
        self.assertIsNotNone(card)
        self.assertIn("Alice and I talk often.", card.body)


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


if __name__ == "__main__":
    unittest.main()
