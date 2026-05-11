"""Tests for `send_with_fallback`."""
import asyncio
import logging
import unittest
from types import SimpleNamespace

from xagent.integrations.feishu.send import send_with_fallback


def _ok(message_id="om_sent"):
    return SimpleNamespace(success=True, message_id=message_id, error=None, raw=None)


def _revoked():
    return SimpleNamespace(
        success=False,
        message_id=None,
        error=SimpleNamespace(code=SimpleNamespace(value="target_revoked"), raw_code=230002, hint="not found"),
        raw=None,
    )


def _generic_failure():
    return SimpleNamespace(
        success=False,
        message_id=None,
        error=SimpleNamespace(code=SimpleNamespace(value="unknown"), raw_code=500, hint="server error"),
        raw=None,
    )


class _FakeChannel:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def send(self, chat_id, payload, opts):
        self.calls.append((chat_id, payload, opts))
        return self._results.pop(0)


class SendWithFallbackTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test")

    def test_success_is_returned_unchanged(self):
        channel = _FakeChannel([_ok()])
        result = asyncio.run(
            send_with_fallback(
                channel,
                chat_id="oc_dm",
                payload={"markdown": "hi"},
                reply_to="om_user",
                is_p2p=True,
                logger=self.logger,
            )
        )
        self.assertTrue(result.success)
        # opts carries reply_to
        self.assertEqual(channel.calls[0][2], {"reply_to": "om_user"})

    def test_target_revoked_in_p2p_retries_without_reply_to(self):
        channel = _FakeChannel([_revoked(), _ok(message_id="om_fresh")])
        result = asyncio.run(
            send_with_fallback(
                channel,
                chat_id="oc_dm",
                payload={"markdown": "hi"},
                reply_to="om_revoked",
                is_p2p=True,
                logger=self.logger,
            )
        )
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "om_fresh")
        self.assertEqual(len(channel.calls), 2)
        # second call has no opts
        self.assertIsNone(channel.calls[1][2])

    def test_target_revoked_in_group_does_NOT_retry(self):
        channel = _FakeChannel([_revoked()])
        result = asyncio.run(
            send_with_fallback(
                channel,
                chat_id="oc_group",
                payload={"markdown": "hi"},
                reply_to="om_revoked",
                is_p2p=False,
                logger=self.logger,
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(len(channel.calls), 1)

    def test_generic_failure_no_retry(self):
        channel = _FakeChannel([_generic_failure()])
        result = asyncio.run(
            send_with_fallback(
                channel,
                chat_id="oc_dm",
                payload={"markdown": "hi"},
                reply_to="om_user",
                is_p2p=True,
                logger=self.logger,
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(len(channel.calls), 1)

    def test_no_reply_to_sends_without_opts(self):
        channel = _FakeChannel([_ok()])
        asyncio.run(
            send_with_fallback(
                channel,
                chat_id="oc_dm",
                payload={"markdown": "hi"},
                reply_to=None,
                is_p2p=True,
                logger=self.logger,
            )
        )
        self.assertIsNone(channel.calls[0][2])


if __name__ == "__main__":
    unittest.main()
