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

    def test_group_mention_routes_to_chat_when_bot_identity_unresolved(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = _FakeChannel(bot_open_id=None)
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_group_msg",
            sender_id="ou_user",
            content_text="@Mono ping",
            mentioned_bot=False,
            mentions=[SimpleNamespace(open_id="ou_unknown_bot", name="Mono")],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.chat_calls), 1)
        self.assertEqual(adapter._channel.sent[0][2], {"reply_to": "om_group_msg"})

    def test_group_mention_with_empty_text_still_replies(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_empty_at",
            sender_id="ou_user",
            content_text="",
            mentioned_bot=True,
            mentions=[],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.chat_calls), 1)
        self.assertEqual(
            agent.chat_calls[0]["user_message"],
            "The user mentioned you without adding any text.",
        )
        self.assertEqual(adapter._channel.sent[0][2], {"reply_to": "om_empty_at"})

    def test_group_mention_matches_raw_nested_open_id_dict(self):
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
            mentions=[{"id": {"open_id": "ou_bot"}, "name": "Mono"}],
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

    def test_reconnect_handlers_accept_sdk_no_arg_callbacks(self):
        adapter = FeishuAdapter(
            agent=_FakeAgent(),
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )

        asyncio.run(adapter._on_reconnecting())
        asyncio.run(adapter._on_reconnected())

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


class FeishuPrefetchTests(unittest.TestCase):
    """``observe`` then ``chat`` when the bot is @mentioned in a group."""

    def _make_channel(self, *, records, fetch_parent=None, bot_open_id="ou_bot"):
        from xagent.integrations.feishu import history as history_mod

        captured_calls: dict = {"list": [], "fetch": []}

        async def fake_fetch_context(self, **kwargs):  # noqa: ARG001
            captured_calls["fetch_kwargs"] = kwargs
            return list(records)

        # Patch the fetcher's fetch_context to bypass real API calls.
        self._patch_targets = []

        original = history_mod.FeishuHistoryFetcher.fetch_context

        async def patched(self_fetcher, **kw):
            return await fake_fetch_context(self_fetcher, **kw)

        history_mod.FeishuHistoryFetcher.fetch_context = patched
        self._patch_targets.append((history_mod.FeishuHistoryFetcher, "fetch_context", original))

        channel = _FakeChannel(bot_open_id=bot_open_id)
        # Add attributes the fetcher checks for so it constructs successfully.
        channel.client = SimpleNamespace()
        channel.captured_calls = captured_calls
        return channel

    def tearDown(self):
        for owner, name, original in getattr(self, "_patch_targets", []):
            setattr(owner, name, original)
        self._patch_targets = []

    def test_group_mention_runs_observe_before_chat_with_recap(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord

        records = [
            FeishuMessageRecord(
                message_id="om_old1",
                sender_id="ou_alice",
                sender_name="Alice",
                text="hi all",
                create_time_ms=1,
                source="history",
            ),
            FeishuMessageRecord(
                message_id="om_old2",
                sender_id="ou_bob",
                sender_name="Bob",
                text="ready?",
                create_time_ms=2,
                source="parent",
            ),
        ]
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = self._make_channel(records=records)
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_at",
            sender_id="ou_user",
            content_text="@Mono what's up",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
            reply_to_message_id="om_old2",
            conversation=SimpleNamespace(thread_id=None),
        )

        asyncio.run(adapter._dispatch(msg))

        # observe ran first with a recap that mentions the prefetched lines.
        self.assertEqual(len(agent.observe_calls), 1)
        observe_kwargs = agent.observe_calls[0]
        self.assertIn("Alice", observe_kwargs["context"])
        self.assertIn("hi all", observe_kwargs["context"])
        self.assertIn("Bob", observe_kwargs["context"])
        self.assertEqual(observe_kwargs["event_type"], "history_recap")
        self.assertFalse(observe_kwargs["metadata"]["addressed_to_agent"])
        self.assertTrue(observe_kwargs["metadata"]["context_only"])
        self.assertEqual(observe_kwargs["metadata"]["record_count"], 2)
        # Prefetched observe MUST be ingest-only to avoid swallowing the
        # @-reply that follows.
        self.assertTrue(observe_kwargs["no_reply"])

        # observe's (discarded) reply did NOT cause anything to be sent.
        # Then chat ran and produced the actual reply.
        self.assertEqual(len(agent.chat_calls), 1)
        self.assertEqual(adapter._channel.sent[0][0], "oc_group")
        self.assertEqual(adapter._channel.sent[0][2], {"reply_to": "om_at"})

        # Fetcher received the expected parent + chat history request.
        fetch_kwargs = adapter._channel.captured_calls["fetch_kwargs"]
        self.assertEqual(fetch_kwargs["chat_id"], "oc_group")
        self.assertEqual(fetch_kwargs["current_message_id"], "om_at")
        self.assertEqual(fetch_kwargs["parent_message_id"], "om_old2")
        self.assertIsNone(fetch_kwargs["thread_id"])
        self.assertEqual(fetch_kwargs["history_count"], 10)

    def test_topic_mention_passes_thread_id_to_fetcher(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord

        records = [
            FeishuMessageRecord(
                message_id="om_root",
                sender_id="ou_alice",
                sender_name="Alice",
                text="topic seed",
                create_time_ms=1,
                source="thread",
            )
        ]
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = self._make_channel(records=records)
        msg = SimpleNamespace(
            chat_type="topic",
            chat_id="oc_topic",
            message_id="om_topic_at",
            sender_id="ou_user",
            content_text="@Mono ?",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
            reply=None,
            conversation=SimpleNamespace(thread_id="omt_thread_1"),
        )

        asyncio.run(adapter._dispatch(msg))

        fetch_kwargs = adapter._channel.captured_calls["fetch_kwargs"]
        self.assertEqual(fetch_kwargs["thread_id"], "omt_thread_1")
        self.assertEqual(len(agent.observe_calls), 1)
        self.assertEqual(len(agent.chat_calls), 1)

    def test_prefetch_skipped_when_no_context_signals(self):
        # p2p with no reply parent and group history disabled -> no fetch,
        # no observe call inserted.
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(
                app_id="cli_test",
                app_secret="secret",
                chat_history_count=0,
            ),
        )
        channel = _FakeChannel()
        channel.client = SimpleNamespace()
        adapter._channel = channel
        msg = SimpleNamespace(
            chat_type="p2p",
            chat_id="oc_dm",
            message_id="om_user",
            sender_id="ou_user",
            content_text="hi",
            reply=None,
            conversation=SimpleNamespace(thread_id=None),
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.observe_calls, [])
        self.assertEqual(len(agent.chat_calls), 1)

    def test_prefetch_disabled_by_config(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(
                app_id="cli_test",
                app_secret="secret",
                prefetch_context=False,
            ),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_at",
            sender_id="ou_user",
            content_text="@Mono hi",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
            reply_to_message_id="om_old",
            conversation=SimpleNamespace(thread_id="omt_thread"),
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.observe_calls, [])
        self.assertEqual(len(agent.chat_calls), 1)

    def test_prefetch_empty_result_skips_observe(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
        )
        adapter._channel = self._make_channel(records=[])
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_at",
            sender_id="ou_user",
            content_text="@Mono hi",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
            reply=None,
            conversation=SimpleNamespace(thread_id=None),
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.observe_calls, [])
        self.assertEqual(len(agent.chat_calls), 1)


class FeishuHistoryFetcherTests(unittest.TestCase):
    def test_render_content_extracts_text_payload(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        self.assertEqual(
            FeishuHistoryFetcher._render_content("text", '{"text": "hello world"}'),
            "hello world",
        )

    def test_render_content_handles_rich_post(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        payload = {
            "title": "Notice",
            "content": [
                [
                    {"tag": "text", "text": "Hi "},
                    {"tag": "at", "user_name": "Alice"},
                    {"tag": "text", "text": ", see this:"},
                ],
                [{"tag": "a", "text": "link", "href": "https://example.com"}],
            ],
        }
        rendered = FeishuHistoryFetcher._render_content("post", payload)
        self.assertIn("Notice", rendered)
        self.assertIn("Hi @Alice, see this:", rendered)
        self.assertIn("link", rendered)

    def test_render_content_unknown_type_returns_placeholder(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        self.assertEqual(
            FeishuHistoryFetcher._render_content("image", '{"image_key": "x"}'),
            "[image]",
        )

    def test_normalize_item_from_dict_payload(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        item = {
            "message_id": "om_1",
            "msg_type": "text",
            "create_time": "1700000000000",
            "sender": {"id": "ou_alice", "name": "Alice"},
            "body": {"content": '{"text": "hi"}'},
        }
        rec = FeishuHistoryFetcher._normalize_item(item, source="history")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.message_id, "om_1")
        self.assertEqual(rec.sender_id, "ou_alice")
        self.assertEqual(rec.sender_name, "Alice")
        self.assertEqual(rec.text, "hi")
        self.assertEqual(rec.create_time_ms, 1700000000000)
        self.assertEqual(rec.source, "history")

    def test_fetch_context_gracefully_handles_missing_channel_attrs(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        fetcher = FeishuHistoryFetcher(channel=SimpleNamespace())
        records = asyncio.run(
            fetcher.fetch_context(
                chat_id="oc_x",
                current_message_id="om_x",
                parent_message_id="om_parent",
                thread_id="omt_thread",
                history_count=5,
            )
        )
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
