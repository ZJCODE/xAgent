"""Tests for the persistent dedup ledger."""
import os
import tempfile
import unittest
from pathlib import Path

from xagent.integrations.feishu.dedup import PersistentDedup


class PersistentDedupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, **kwargs) -> PersistentDedup:
        return PersistentDedup(namespace="test_app", state_dir=self.state_dir, **kwargs)

    def test_claim_then_duplicate(self):
        d = self._make()
        self.assertEqual(d.try_begin("om_1"), "claimed")
        # Same id while still inflight -> "inflight"
        self.assertEqual(d.try_begin("om_1"), "inflight")
        d.finalize("om_1")
        # After finalize -> "duplicate"
        self.assertEqual(d.try_begin("om_1"), "duplicate")

    def test_missing_id_is_invalid(self):
        d = self._make()
        self.assertEqual(d.try_begin(None), "invalid")
        self.assertEqual(d.try_begin(""), "invalid")

    def test_persistence_across_instances(self):
        first = self._make()
        self.assertEqual(first.try_begin("om_persist"), "claimed")
        first.finalize("om_persist")
        # New instance, same namespace + state dir should see it as duplicate.
        second = self._make()
        self.assertEqual(second.try_begin("om_persist"), "duplicate")

    def test_release_lets_message_be_claimed_again(self):
        d = self._make()
        self.assertEqual(d.try_begin("om_release"), "claimed")
        d.release("om_release")
        self.assertEqual(d.try_begin("om_release"), "claimed")

    def test_file_path_uses_safe_namespace(self):
        d = PersistentDedup(namespace="app/with:weird*chars", state_dir=self.state_dir)
        # No dangerous path chars should appear in the resolved file.
        rel = d.file_path.relative_to(self.state_dir).as_posix()
        self.assertNotIn("/", rel.removeprefix("feishu/dedup/"))
        self.assertNotIn(":", rel)
        self.assertNotIn("*", rel)


class DefaultStateDirEnvTests(unittest.TestCase):
    def test_env_override(self):
        from xagent.integrations.feishu.dedup import default_state_dir

        with tempfile.TemporaryDirectory() as tmp:
            prev = os.environ.get("XAGENT_STATE_DIR")
            os.environ["XAGENT_STATE_DIR"] = tmp
            try:
                self.assertEqual(default_state_dir(), Path(tmp))
            finally:
                if prev is None:
                    os.environ.pop("XAGENT_STATE_DIR", None)
                else:
                    os.environ["XAGENT_STATE_DIR"] = prev


if __name__ == "__main__":
    unittest.main()
