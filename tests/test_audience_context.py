"""Audience / present_keys relationship context (Phase 5–6)."""

import unittest

from xagent.core.config import AgentConfig
from xagent.integrations.feishu.history import (
    FeishuMessageRecord,
    build_feishu_room_snapshot,
    collect_feishu_present_keys,
)


class FeishuPresentKeysTests(unittest.TestCase):
    def test_collect_present_keys_dedupes_and_caps(self):
        records = [
            FeishuMessageRecord("1", "ou_a", "A", "one", 1),
            FeishuMessageRecord("2", "ou_b", "B", "two", 2),
            FeishuMessageRecord("3", "ou_a", "A", "again", 3),
            FeishuMessageRecord("4", "ou_c", "C", "three", 4),
        ]
        keys = collect_feishu_present_keys(records, max_cards=2)
        self.assertEqual(keys, ["feishu:ou_a", "feishu:ou_b"])

    def test_room_snapshot_fills_present_keys_from_history(self):
        records = [
            FeishuMessageRecord("1", "ou_x", "X", "hi", 1),
            FeishuMessageRecord("2", "ou_y", "Y", "yo", 2),
        ]
        snapshot = build_feishu_room_snapshot("chat1", records)
        self.assertIn("feishu:ou_x", snapshot.present_keys)
        self.assertIn("feishu:ou_y", snapshot.present_keys)


if __name__ == "__main__":
    unittest.main()
