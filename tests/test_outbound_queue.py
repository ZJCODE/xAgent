"""Tests for the outbound intent queue and recipient resolution."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from xagent.core.runtime import ContactEntry
from xagent.core.runtime.outbound import (
    OUTBOUND_SOURCE_CONSCIOUS,
    OUTBOUND_SOURCE_SUBCONSCIOUS,
    OUTBOUND_STATUS_DELIVERED,
    OUTBOUND_STATUS_FAILED,
    claim_pending_outbound,
    drain_outbound_once,
    enqueue_outbound,
    list_pending_outbound,
    mark_delivered,
    mark_failed,
    resolve_recipient,
)


class OutboundQueueTests(unittest.IsolatedAsyncioTestCase):
    def test_enqueue_and_list_pending_by_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            feishu = ContactEntry(
                channel="feishu",
                user_id="ou_a",
                target={"chat_id": "oc_1", "sender_name": "Alice"},
                last_seen="2026-08-04 10:00:00",
            )
            weixin = ContactEntry(
                channel="weixin",
                user_id="wx_b",
                target={"user_id": "wx_b"},
                last_seen="2026-08-04 10:01:00",
            )
            enqueue_outbound(
                workspace,
                content="hello alice",
                recipient=feishu,
                source=OUTBOUND_SOURCE_CONSCIOUS,
                motive="self",
            )
            enqueue_outbound(
                workspace,
                content="hello bob",
                recipient=weixin,
                source=OUTBOUND_SOURCE_SUBCONSCIOUS,
            )
            all_pending = list_pending_outbound(workspace)
            self.assertEqual(len(all_pending), 2)
            feishu_only = list_pending_outbound(workspace, channel="feishu")
            self.assertEqual(len(feishu_only), 1)
            self.assertEqual(feishu_only[0].content, "hello alice")

    def test_claim_is_channel_scoped_and_removes_from_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            recipient = ContactEntry(
                channel="api",
                user_id="web_user",
                target={"user_id": "web_user"},
                last_seen="2026-08-04 10:00:00",
            )
            enqueue_outbound(
                workspace,
                content="ping",
                recipient=recipient,
                source=OUTBOUND_SOURCE_CONSCIOUS,
            )
            claimed = claim_pending_outbound(workspace, channel="api")
            self.assertEqual(len(claimed), 1)
            self.assertEqual(list_pending_outbound(workspace), [])
            self.assertTrue(claimed[0].path is not None)
            self.assertIn(".claiming-", claimed[0].path.name)

    def test_mark_delivered_and_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            recipient = ContactEntry(
                channel="api",
                user_id="web_user",
                target={"user_id": "web_user"},
                last_seen="2026-08-04 10:00:00",
            )
            intent = enqueue_outbound(
                workspace,
                content="ok",
                recipient=recipient,
                source=OUTBOUND_SOURCE_CONSCIOUS,
            )
            claimed = claim_pending_outbound(workspace, channel="api")[0]
            delivered = mark_delivered(claimed)
            self.assertEqual(delivered.status, OUTBOUND_STATUS_DELIVERED)
            self.assertTrue(delivered.path is not None)
            self.assertIn("/delivered/", str(delivered.path).replace("\\", "/"))

            intent2 = enqueue_outbound(
                workspace,
                content="fail",
                recipient=recipient,
                source=OUTBOUND_SOURCE_CONSCIOUS,
            )
            claimed2 = claim_pending_outbound(workspace, channel="api")[0]
            failed = mark_failed(claimed2, error="offline")
            self.assertEqual(failed.status, OUTBOUND_STATUS_FAILED)
            self.assertEqual(failed.error, "offline")

    async def test_drain_outbound_once_delivers_and_archives(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            recipient = ContactEntry(
                channel="api",
                user_id="web_user",
                target={"user_id": "web_user"},
                last_seen="2026-08-04 10:00:00",
            )
            enqueue_outbound(
                workspace,
                content="hello",
                recipient=recipient,
                source=OUTBOUND_SOURCE_CONSCIOUS,
            )
            seen = []

            async def deliver(intent):
                seen.append(intent.content)

            count = await drain_outbound_once(
                workspace,
                channels=["api"],
                deliver=deliver,
            )
            self.assertEqual(count, 1)
            self.assertEqual(seen, ["hello"])
            self.assertEqual(list_pending_outbound(workspace), [])

    async def test_drain_marks_failed_on_transport_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            recipient = ContactEntry(
                channel="api",
                user_id="web_user",
                target={"user_id": "web_user"},
                last_seen="2026-08-04 10:00:00",
            )
            enqueue_outbound(
                workspace,
                content="hello",
                recipient=recipient,
                source=OUTBOUND_SOURCE_CONSCIOUS,
            )

            async def deliver(_intent):
                raise RuntimeError("transport down")

            count = await drain_outbound_once(
                workspace,
                channels=["api"],
                deliver=deliver,
            )
            self.assertEqual(count, 0)
            self.assertEqual(list_pending_outbound(workspace), [])


class RecipientResolutionTests(unittest.TestCase):
    def test_exact_and_ambiguous_resolution(self):
        contacts = [
            ContactEntry("feishu", "ou_a", {"sender_name": "Alice"}, "2026-08-04 10:00:00"),
            ContactEntry("feishu", "ou_b", {"sender_name": "Bob"}, "2026-08-04 10:01:00"),
            ContactEntry("weixin", "wx_a", {"sender_name": "Alice"}, "2026-08-04 10:02:00"),
        ]
        exact = resolve_recipient("Bob", contacts)
        self.assertTrue(exact.ok)
        self.assertEqual(exact.match.user_id, "ou_b")

        ambiguous = resolve_recipient("Alice", contacts)
        self.assertFalse(ambiguous.ok)
        self.assertEqual(len(ambiguous.candidates), 2)

        scoped = resolve_recipient("Alice", contacts, channel="weixin")
        self.assertTrue(scoped.ok)
        self.assertEqual(scoped.match.channel, "weixin")


if __name__ == "__main__":
    unittest.main()
