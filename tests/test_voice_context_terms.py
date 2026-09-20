import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xagent.interfaces.voice.context_terms import (
    contact_terms,
    known_people_terms,
    refreshed_context_terms,
)


class FakeRelationshipStore:
    def __init__(self, cards):
        self._cards = cards

    async def list_keys(self):
        return [card.key for card in self._cards]

    async def read_cards(self, keys):
        return [card for card in self._cards if card.key in set(keys)]


def _card(key, user_id, display_name):
    return SimpleNamespace(key=key, user_id=user_id, display_name=display_name)


class ContextTermTests(unittest.TestCase):
    def test_contacts_supply_display_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "contacts.json").write_text(
                json.dumps(
                    {
                        "contacts": [
                            {
                                "channel": "feishu",
                                "user_id": "ou_123",
                                "target": {"sender_name": "Mei"},
                                "last_seen": "2026-01-01T00:00:00",
                            },
                            {
                                "channel": "voice",
                                "user_id": "local_voice",
                                "target": {"user_id": "local_voice"},
                                "last_seen": "2026-01-01T00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            agent = SimpleNamespace(workspace=workspace)

            self.assertEqual(contact_terms(agent), ["Mei"])

    def test_relationship_cards_supply_names(self):
        agent = SimpleNamespace(
            relationship_store=FakeRelationshipStore(
                [
                    _card("voice:alice", "alice", "Alice"),
                    _card("voice:bob", "bob", ""),
                ]
            )
        )

        self.assertEqual(asyncio.run(known_people_terms(agent)), ["Alice"])

    def test_refreshed_terms_lead_with_the_agents_own_name(self):
        agent = SimpleNamespace(
            name="Telos",
            relationship_store=FakeRelationshipStore([_card("voice:alice", "alice", "Alice")]),
        )

        terms = asyncio.run(refreshed_context_terms(agent))

        self.assertEqual(terms[0], "Telos")
        self.assertIn("Alice", terms)

    def test_missing_stores_are_not_fatal(self):
        self.assertEqual(asyncio.run(refreshed_context_terms(SimpleNamespace())), [])


if __name__ == "__main__":
    unittest.main()
