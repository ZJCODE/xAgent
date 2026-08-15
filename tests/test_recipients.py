"""Tests for the SQLite recipient directory."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from xagent.core.recipients import (
    RecipientDirectory,
    make_recipient_key,
    sanitize_target,
)


class RecipientDirectoryTests(unittest.TestCase):
    def _directory(self, tmpdir: str) -> RecipientDirectory:
        return RecipientDirectory(Path(tmpdir) / "messages" / "messages.sqlite3")

    def test_upsert_and_resolve_direct_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = self._directory(tmpdir)
            route = directory.upsert(
                channel="feishu",
                user_id="ou_123",
                display_name="张三",
                target={"chat_id": "oc_p2p", "sender_id": "ou_123", "context_token": "secret"},
                aliases=["张三"],
            )
            self.assertIsNotNone(route)
            self.assertEqual(route.recipient_key, "feishu:ou_123")
            self.assertNotIn("context_token", route.target)
            found = directory.resolve("feishu:ou_123")
            self.assertEqual(found.user_id, "ou_123")
            by_alias = directory.resolve("张三", channel="feishu")
            self.assertEqual(by_alias.recipient_key, "feishu:ou_123")
            self.assertFalse((Path(tmpdir) / "contacts.json.lock").exists())

    def test_group_targets_are_not_stored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = self._directory(tmpdir)
            self.assertIsNone(
                directory.upsert(
                    channel="feishu",
                    user_id="ou_123",
                    target={"chat_id": "oc_group", "is_group": True},
                )
            )
            self.assertEqual(directory.list_routes(), [])

    def test_cross_channel_keys_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = self._directory(tmpdir)
            directory.upsert(channel="feishu", user_id="same", target={"chat_id": "oc_1"})
            directory.upsert(channel="weixin", user_id="same", target={"user_id": "same"})
            keys = {route.recipient_key for route in directory.list_routes()}
            self.assertEqual(keys, {"feishu:same", "weixin:same"})

    def test_sanitize_target_drops_tokens(self):
        cleaned = sanitize_target({
            "user_id": "wx_1",
            "context_token": "tok",
            "account_id": "acct",
            "chat_id": "oc_1",
        })
        self.assertEqual(cleaned, {"user_id": "wx_1", "chat_id": "oc_1"})

    def test_concurrent_upsert(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = self._directory(tmpdir)
            errors = []

            def write(index: int) -> None:
                try:
                    directory.upsert(
                        channel="api",
                        user_id=f"user_{index % 3}",
                        display_name=f"User {index}",
                        target={"user_id": f"user_{index % 3}"},
                    )
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(i,)) for i in range(24)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(directory.list_routes()), 3)

    def test_legacy_import_is_idempotent_and_skips_groups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contacts = Path(tmpdir) / "contacts.json"
            contacts.write_text(
                json.dumps({
                    "contacts": [
                        {
                            "channel": "feishu",
                            "user_id": "张三",
                            "target": {
                                "chat_id": "oc_p2p",
                                "sender_id": "ou_abc",
                                "sender_name": "张三",
                            },
                            "last_seen": "2026-06-25 09:00:00",
                        },
                        {
                            "channel": "feishu",
                            "user_id": "群里的人",
                            "target": {"chat_id": "oc_group", "is_group": True, "sender_id": "ou_skip"},
                        },
                        {
                            "channel": "weixin",
                            "user_id": "wx_1",
                            "target": {"user_id": "wx_1", "context_token": "tok"},
                        },
                    ]
                }),
                encoding="utf-8",
            )
            directory = self._directory(tmpdir)
            first = directory.import_legacy_contacts(contacts)
            second = directory.import_legacy_contacts(contacts)
            self.assertGreaterEqual(first, 2)
            self.assertGreaterEqual(second, 2)
            self.assertTrue(directory.legacy_import_recorded())
            feishu = directory.resolve("feishu:ou_abc")
            self.assertIsNotNone(feishu)
            self.assertEqual(feishu.display_name, "张三")
            self.assertIn("张三", feishu.aliases)
            self.assertIsNone(directory.resolve("feishu:ou_skip"))
            weixin = directory.resolve("weixin:wx_1")
            self.assertNotIn("context_token", weixin.target)
            self.assertTrue(contacts.exists())
            self.assertFalse(Path(str(contacts) + ".lock").exists())

    def test_make_recipient_key(self):
        self.assertEqual(make_recipient_key("feishu", "ou_1"), "feishu:ou_1")


if __name__ == "__main__":
    unittest.main()
