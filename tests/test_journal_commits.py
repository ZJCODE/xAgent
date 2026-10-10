"""Recovery receipts preserve diary bytes across interruption and conflicts."""

import copy
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from xagent.core.journal_commits import JournalCommitStore, atomic_write_bytes


class JournalCommitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = JournalCommitStore(self.root)
        self.day = date(2026, 10, 10)
        self.target = self.root / "daily/2026/2026-10/2026-10-10.md"

    def tearDown(self):
        self.directory.cleanup()

    def prepare(self, old=b"", prepared="\n## 2026-10-10 10:00\n\n我记住了一件事。\n"):
        if old:
            atomic_write_bytes(self.target, old)
        return self.store.prepare(
            target_date=self.day, old_content=old, prepared_content=prepared,
            cursor_before=8, start_exclusive=4, end_inclusive=12,
        )

    def test_prepared_append_preserves_original_bytes_and_applies_once(self):
        old = "已有日记。\n".encode("utf-8")
        receipt = self.prepare(old=old)
        self.assertTrue(self.store.apply(receipt))
        written = self.target.read_bytes()
        self.assertTrue(written.startswith(old))
        self.assertTrue(self.store.apply(self.store.load()))
        self.assertEqual(self.target.read_bytes(), written)

    def test_crash_before_atomic_replace_keeps_old_page_and_prepared_receipt(self):
        old = b"original diary\n"
        receipt = self.prepare(old=old)
        with patch("xagent.core.journal_commits.os.replace", side_effect=OSError("interrupted rename")):
            with self.assertRaises(OSError):
                self.store.apply(receipt)
        self.assertEqual(self.target.read_bytes(), old)
        self.assertEqual(self.store.load()["status"], "prepared")
        self.assertTrue(self.store.apply(self.store.load()))
        self.assertEqual(self.target.read_bytes().count(old), 1)

    def test_crash_after_replace_recognizes_append_on_recovery(self):
        receipt = self.prepare(old=b"original diary\n")
        with patch("xagent.core.journal_commits.sync_directory", side_effect=OSError("interrupted fsync")):
            with self.assertRaises(OSError):
                self.store.apply(receipt)
        written = self.target.read_bytes()
        self.assertTrue(self.store.apply(self.store.load()))
        self.assertEqual(self.target.read_bytes(), written)

    def test_manual_prefix_edit_is_quarantined(self):
        receipt = self.prepare(old=b"original diary\n")
        self.target.write_bytes(b"edited original diary\n")
        self.assertFalse(self.store.apply(receipt))
        self.assertEqual(self.target.read_bytes(), b"edited original diary\n")
        self.assertEqual(self.store.load()["status"], "needs_review")

    def test_manual_edit_during_tempfile_flush_is_preserved(self):
        receipt = self.prepare(old=b"original diary\n")
        actual_fsync = os.fsync
        edited = False

        def edit_during_flush(descriptor):
            nonlocal edited
            if not edited:
                self.target.write_bytes(b"manual edit during commit\n")
                edited = True
            actual_fsync(descriptor)

        with patch("xagent.core.journal_commits.os.fsync", side_effect=edit_during_flush):
            self.assertFalse(self.store.apply(receipt))
        self.assertEqual(self.target.read_bytes(), b"manual edit during commit\n")
        self.assertEqual(self.store.load()["status"], "needs_review")

    def test_corrupt_or_redirected_preparation_is_rejected(self):
        receipt = self.prepare()
        for field, bad_value in (
            ("target_path", "../outside.md"), ("target_date", "not-a-date"),
            ("prepared_content", "modified"), ("prepared_sha256", "0" * 64),
            ("prepared_length", 1), ("end_inclusive", 3),
        ):
            with self.subTest(field=field):
                corrupted = copy.deepcopy(receipt)
                corrupted[field] = bad_value
                with self.assertRaises(ValueError):
                    self.store.apply(corrupted)
        self.assertFalse(self.target.exists())

    def test_unresolved_receipt_cannot_be_replaced_by_next_window(self):
        receipt = self.prepare()
        with self.assertRaises(ValueError):
            self.prepare(prepared="a different window")
        self.assertEqual(self.store.load()["commit_id"], receipt["commit_id"])

    def test_empty_slice_has_receipt_but_no_daily_file(self):
        receipt = self.prepare(prepared="")
        self.assertTrue(self.store.apply(receipt))
        self.assertFalse(self.target.exists())
        self.store.complete(receipt)
        self.assertEqual(self.store.load()["status"], "completed")


if __name__ == "__main__":
    unittest.main()
