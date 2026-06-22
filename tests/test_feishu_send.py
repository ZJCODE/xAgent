"""Tests for Feishu send helper."""
import asyncio
import logging
import unittest
from types import SimpleNamespace

from xagent.channels.feishu.send import send_message


def _ok(message_id="om_sent"):
    return SimpleNamespace(success=True, message_id=message_id, error=None, raw=None)


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


class SendMessageTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test")

    def test_group_reply_sends_reply_to_and_uuid(self):
        channel = _FakeChannel([_ok()])
        result = asyncio.run(
            send_message(
                channel,
                chat_id="oc_group",
                payload={"markdown": "hi"},
                reply_to="om_user",
                uuid="om_user",
                logger=self.logger,
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(channel.calls[0][2], {"reply_to": "om_user", "uuid": "om_user"})

    def test_p2p_send_has_uuid_without_reply_to(self):
        channel = _FakeChannel([_ok()])
        asyncio.run(
            send_message(
                channel,
                chat_id="oc_dm",
                payload={"markdown": "hi"},
                reply_to=None,
                uuid="om_user",
                logger=self.logger,
            )
        )

        self.assertEqual(channel.calls[0][2], {"uuid": "om_user"})

    def test_missing_options_sends_none(self):
        channel = _FakeChannel([_ok()])
        asyncio.run(
            send_message(
                channel,
                chat_id="oc_dm",
                payload={"markdown": "hi"},
                reply_to=None,
                uuid=None,
                logger=self.logger,
            )
        )

        self.assertIsNone(channel.calls[0][2])

    def test_failure_is_returned_without_retry(self):
        channel = _FakeChannel([_generic_failure()])
        result = asyncio.run(
            send_message(
                channel,
                chat_id="oc_group",
                payload={"markdown": "hi"},
                reply_to="om_user",
                uuid="om_user",
                logger=self.logger,
            )
        )

        self.assertFalse(result.success)
        self.assertEqual(len(channel.calls), 1)


if __name__ == "__main__":
    unittest.main()
