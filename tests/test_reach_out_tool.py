"""Tests for the reach_out conscious outbound producer."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from xagent.core.runtime import ContactEntry, ScheduledDeliveryContext, scheduled_delivery_context, upsert_contact
from xagent.core.runtime.outbound import list_pending_outbound, resolve_contacts_path
from xagent.tools.reach_out_tool import create_reach_out_tool


class ReachOutToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_reach_out_enqueues_for_resolved_contact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            contacts = resolve_contacts_path(workspace)
            upsert_contact(
                contacts,
                channel="feishu",
                user_id="ou_b",
                target={"chat_id": "oc_b", "sender_name": "Bob"},
            )
            tool = create_reach_out_tool(workspace=str(workspace))
            context = ScheduledDeliveryContext(
                channel="feishu",
                user_id="ou_a",
                target={"chat_id": "oc_a", "sender_name": "Alice"},
            )
            with scheduled_delivery_context(context):
                result = await tool(person_ref="Bob", content="Dinner is ready", motive="relay")

            self.assertTrue(result["ok"])
            self.assertEqual(result["recipient"]["user_id"], "ou_b")
            self.assertEqual(result["requester_user_id"], "ou_a")
            pending = list_pending_outbound(workspace)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].recipient.user_id, "ou_b")
            self.assertEqual(pending[0].content, "Dinner is ready")
            self.assertEqual(pending[0].source, "conscious")
            # Delivery target must come from the resolved contact, not requester context.
            self.assertEqual(pending[0].recipient.target.get("chat_id"), "oc_b")

    async def test_reach_out_reports_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            contacts = resolve_contacts_path(workspace)
            upsert_contact(contacts, "feishu", "ou_1", {"sender_name": "Sam"})
            upsert_contact(contacts, "weixin", "wx_1", {"sender_name": "Sam"})
            tool = create_reach_out_tool(workspace=str(workspace))
            result = await tool(person_ref="Sam", content="hi")
            self.assertFalse(result["ok"])
            self.assertIn("ambiguous", result["error"])
            self.assertEqual(len(result["candidates"]), 2)
            self.assertEqual(list_pending_outbound(workspace), [])


if __name__ == "__main__":
    unittest.main()
