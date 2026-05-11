import unittest
import logging
import asyncio
from types import SimpleNamespace

try:
    from lark_oapi import LogLevel
except ImportError:  # pragma: no cover - optional dependency
    LogLevel = None

from xagent.integrations.feishu.adapter import FeishuAdapter
from xagent.integrations.feishu.config import FeishuAdapterConfig


class _FakeAgent:
    output_type = None

    def __init__(self, observe_reply=None):
        self.chat_calls = []
        self.observe_calls = []
        self.observe_reply = observe_reply

    async def chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        return "agent reply"

    async def observe(self, **kwargs):
        self.observe_calls.append(kwargs)
        if self.observe_reply is not None:
            return self.observe_reply
        return SimpleNamespace(replied=False, reply=None)


class _FakeChannel:
    def __init__(self, bot_open_id="ou_bot"):
        self.bot_identity = SimpleNamespace(open_id=bot_open_id)
        self.sent = []

    async def send(self, chat_id, message, opts=None):
        self.sent.append((chat_id, message, opts))
        return SimpleNamespace(success=True, message_id="om_reply", error=None, raw=None)


class FeishuAdapterTests(unittest.TestCase):
    @unittest.skipIf(LogLevel is None, "lark-oapi is not installed")
    def test_normalize_log_level_accepts_yaml_friendly_strings(self):
        self.assertEqual(FeishuAdapter._normalize_log_level("info", LogLevel), LogLevel.INFO)
        self.assertEqual(FeishuAdapter._normalize_log_level("warn", LogLevel), LogLevel.WARNING)
        self.assertEqual(FeishuAdapter._normalize_log_level("ERROR", LogLevel), LogLevel.ERROR)

    @unittest.skipIf(LogLevel is None, "lark-oapi is not installed")
    def test_normalize_log_level_rejects_unknown_value(self):
        with self.assertRaises(RuntimeError):
            FeishuAdapter._normalize_log_level("chatty", LogLevel)

    def test_unknown_top_level_keys_are_ignored_but_advanced_block_forwards(self):
        cfg = FeishuAdapterConfig.from_dict(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "log_level": "info",
                "custom_sdk_kwarg": "ignored",  # legacy/unknown top-level key
                "advanced": {"policy": "marker"},
            }
        )

        self.assertEqual(cfg.advanced, {"policy": "marker"})

    def test_log_redaction_hides_ws_credentials(self):
        record = logging.LogRecord(
            name="Lark",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="connected to wss://example/ws?access_key=abc&ticket=def&service_id=1",
            args=(),
            exc_info=None,
        )

        self.assertTrue(FeishuAdapter._install_log_redaction_filter() is None)
        lark_logger = logging.getLogger("Lark")
        for item in lark_logger.filters:
            item.filter(record)

        message = record.getMessage()
        self.assertIn("access_key=[redacted]", message)
        self.assertIn("ticket=[redacted]", message)
        self.assertNotIn("access_key=abc", message)
        self.assertNotIn("ticket=def", message)

    def test_direct_chat_reply_does_not_quote_source_message_or_use_private(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = _FakeChannel()
        msg = SimpleNamespace(
            chat_type="p2p",
            chat_id="oc_dm",
            message_id="om_user",
            sender_id="ou_user",
            content_text="hello",
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.chat_calls), 1)
        self.assertNotIn("private", agent.chat_calls[0])
        self.assertEqual(adapter._channel.sent[0][2], None)

    def test_group_mention_detects_mentions_matching_bot_identity(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_group_msg",
            sender_id="ou_user",
            content_text="@Mono ping",
            mentioned_bot=False,
            mentions=[SimpleNamespace(open_id="ou_bot")],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.chat_calls), 1)
        self.assertEqual(agent.chat_calls[0]["user_id"], "ou_user")
        self.assertEqual(adapter._channel.sent[0][2]["reply_to"], "om_group_msg")
        self.assertNotIn("reply_in_thread", adapter._channel.sent[0][2])

    def test_topic_mention_is_handled_like_group_message(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="topic",
            chat_id="oc_group",
            message_id="om_topic_msg",
            sender_id="ou_user",
            content_text="@Mono ping",
            mentioned_bot=False,
            mentions=[SimpleNamespace(open_id="ou_bot")],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.chat_calls), 1)

    def test_observe_group_routes_unmentioned_messages_to_observe_without_private(self):
        agent = _FakeAgent(observe_reply=SimpleNamespace(replied=True, reply="observe reply"))
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_observed",
            sender_id="ou_user",
            content_text="ambient group message",
            mentioned_bot=False,
            mentions=[],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.observe_calls), 1)
        self.assertNotIn("private", agent.observe_calls[0])
        self.assertEqual(agent.observe_calls[0]["metadata"]["addressed_to_agent"], False)
        self.assertEqual(adapter._channel.sent[0], ("oc_group", {"markdown": "observe reply"}, None))

    def test_unmentioned_topic_message_is_also_observed(self):
        agent = _FakeAgent(observe_reply=SimpleNamespace(replied=False, reply=None))
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="topic",
            chat_id="oc_topic",
            message_id="om_topic",
            sender_id="ou_user",
            content_text="hello everyone",
            mentioned_bot=False,
            mentions=[],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.chat_calls), 0)
        self.assertEqual(len(agent.observe_calls), 1)
        # Agent declined to speak -> nothing sent.
        self.assertEqual(adapter._channel.sent, [])

    def test_send_opts_never_uses_thread_reply(self):
        adapter = FeishuAdapter(
            agent=_FakeAgent(),
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        self.assertIsNone(adapter._send_opts(message_id="om_x", is_group=False))
        opts = adapter._send_opts(message_id="om_x", is_group=True)
        self.assertEqual(opts, {"reply_to": "om_x"})
        self.assertNotIn("reply_in_thread", opts)

    def test_legacy_config_keys_are_silently_ignored(self):
        cfg = FeishuAdapterConfig.from_dict(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "group_require_mention": True,
                "observe_group": True,
                "reply_in_group_thread": True,
                "private": True,
            }
        )
        self.assertEqual(cfg.advanced, {})
        self.assertFalse(hasattr(cfg, "group_require_mention"))
        self.assertFalse(hasattr(cfg, "observe_group"))

    def test_message_text_falls_back_to_content_text_for_sdk_batches(self):
        msg = SimpleNamespace(
            content_text="",
            content=SimpleNamespace(text="merged text"),
        )

        self.assertEqual(FeishuAdapter._message_text(msg), "merged text")


if __name__ == "__main__":
    unittest.main()
