import asyncio
import io
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from lark_oapi import LogLevel
except ImportError:  # pragma: no cover - optional dependency
    LogLevel = None

from xagent.integrations.feishu import adapter as feishu_adapter_module
from xagent.integrations.feishu.adapter import FeishuAdapter, _FeishuOutboundAttachment
from xagent.integrations.feishu.config import FeishuAdapterConfig
from xagent.core.runtime import enqueue_scheduled_task, list_task_records


class _FakeAgent:
    output_type = None
    supports_vision = True

    def __init__(self):
        self.chat_calls = []
        self.observe_calls = []
        self.flush_count = 0

    async def chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        return "agent reply"

    async def chat_events(self, **kwargs):
        self.chat_calls.append(kwargs)
        yield {"type": "message_start", "message_id": "m1", "phase": "final"}
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": "agent reply"}
        yield {"type": "done"}

    async def observe(self, **kwargs):
        self.observe_calls.append(kwargs)
        return SimpleNamespace(replied=False, reply=None)

    async def flush_memory(self):
        self.flush_count += 1


class _AttachmentEventAgent(_FakeAgent):
    def __init__(self, *, content, attachments):
        super().__init__()
        self.content = content
        self.attachments = attachments

    async def chat_events(self, **kwargs):
        self.chat_calls.append(kwargs)
        yield {
            "type": "message_done",
            "message_id": "m1",
            "phase": "final",
            "content": self.content,
            "attachments": self.attachments,
        }
        yield {"type": "done"}


class _SlowChatAgent(_FakeAgent):
    def __init__(self, *, started: asyncio.Event, release: asyncio.Event):
        super().__init__()
        self.started = started
        self.release = release

    async def chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        return "agent reply"

    async def chat_events(self, **kwargs):
        self.chat_calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": "agent reply"}
        yield {"type": "done"}


class _FakeChannel:
    def __init__(self, bot_open_id="ou_bot", bot_name="Mono", client=None, results=None):
        self.bot_identity = SimpleNamespace(open_id=bot_open_id, name=bot_name)
        self.client = client
        self.sent = []
        self.results = list(results or [])

    async def send(self, chat_id, message, opts=None):
        self.sent.append((chat_id, message, opts))
        if self.results:
            return self.results.pop(0)
        return SimpleNamespace(success=True, message_id="om_reply", error=None, raw=None)


class _FakeDownloadChannel(_FakeChannel):
    def __init__(self, *args, download_data=b"\x89PNG\r\n\x1a\nimage", **kwargs):
        super().__init__(*args, **kwargs)
        self.download_data = download_data
        self.download_calls = []

    async def download_resource(self, **kwargs):
        self.download_calls.append(kwargs)
        return {"data": self.download_data, "file_name": "public.png", "mime_type": "image/png"}


class _FakeMessageResourceApi:
    def __init__(self, data=b"\x89PNG\r\n\x1a\nimage", file_name="tiny.png", content_type="image/png"):
        self.data = data
        self.file_name = file_name
        self.content_type = content_type
        self.requests = []

    async def aget(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            code=0,
            file=io.BytesIO(self.data),
            file_name=self.file_name,
            raw=SimpleNamespace(headers={"content-type": self.content_type}),
        )


class _FailingMessageResourceApi:
    def __init__(self):
        self.requests = []

    async def aget(self, request):
        self.requests.append(request)
        return SimpleNamespace(code=999, msg="download failed", file=None, raw=None)


def _failed_send_result():
    return SimpleNamespace(
        success=False,
        message_id=None,
        error=SimpleNamespace(code=SimpleNamespace(value="failed"), raw_code=500, hint="failed"),
        raw=None,
    )


def _noisy_jpeg_bytes(*, size=(1400, 1400), quality=95) -> bytes:
    from PIL import Image

    image = Image.effect_noise(size, 96).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


class _FakeUserResolver:
    def __init__(self, names=None):
        self.names = dict(names or {})
        self.calls = []

    async def resolve_name(self, user_id, fallback=None, *, id_type=None, sender_type=None):
        self.calls.append((user_id, fallback, id_type, sender_type))
        fallback_name = fallback.strip() if isinstance(fallback, str) and fallback.strip() else None
        if fallback_name and fallback_name.startswith(("ou_", "on_", "cli_")):
            fallback_name = None
        return self.names.get(user_id) or fallback_name or "Feishu User"


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

    def test_advanced_block_forwards(self):
        cfg = FeishuAdapterConfig.from_dict(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "advanced": {"policy": "marker"},
            }
        )

        self.assertEqual(cfg.advanced, {"policy": "marker"})

    def test_unknown_top_level_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, r"Unsupported Feishu config key\(s\): custom_sdk_kwarg"):
            FeishuAdapterConfig.from_dict(
                {
                    "app_id": "cli_test",
                    "app_secret": "secret",
                    "custom_sdk_kwarg": "unsupported",
                }
            )

    def test_config_defaults_hide_sender_ids(self):
        cfg = FeishuAdapterConfig.from_dict(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
            }
        )

        self.assertFalse(cfg.show_sender_ids)

    def test_config_accepts_show_sender_ids(self):
        cfg = FeishuAdapterConfig.from_dict(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "show_sender_ids": True,
            }
        )

        self.assertTrue(cfg.show_sender_ids)

    def test_stop_flushes_agent_memory(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))

        asyncio.run(adapter.stop())

        self.assertEqual(agent.flush_count, 1)

    def test_scheduled_task_dispatch_sends_to_feishu_chat(self):
        async def run_test():
            agent = _FakeAgent()
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel()
            with tempfile.TemporaryDirectory() as tmpdir:
                enqueue_scheduled_task(
                    task_type="message",
                    content="走两步",
                    run_at="2026-06-01 14:30:00",
                    tasks_dir=tmpdir,
                    channel="feishu",
                    target={"chat_id": "oc_group", "message_id": "om_anchor", "is_group": True},
                    user_id="ou_user",
                )
                task = list_task_records(tmpdir)[0]
                await adapter._dispatch_scheduled_task(task)

            self.assertEqual(adapter._channel.sent[0][0], "oc_group")
            self.assertEqual(adapter._channel.sent[0][1], {"markdown": "走两步"})
            self.assertEqual(adapter._channel.sent[0][2]["reply_to"], "om_anchor")
            self.assertTrue(adapter._channel.sent[0][2]["uuid"].startswith("scheduled:"))

        asyncio.run(run_test())

    def test_scheduled_agent_task_dispatch_sends_agent_reply_to_feishu_chat(self):
        async def run_test():
            agent = _FakeAgent()
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel()
            with tempfile.TemporaryDirectory() as tmpdir:
                enqueue_scheduled_task(
                    task_type="agent",
                    content="Check system temperature",
                    run_at="2026-06-01 14:30:00",
                    tasks_dir=tmpdir,
                    channel="feishu",
                    target={"chat_id": "oc_group", "message_id": "om_anchor", "is_group": True},
                    user_id="ou_user",
                )
                task = list_task_records(tmpdir)[0]
                await adapter._dispatch_scheduled_task(task)

            self.assertEqual(adapter._channel.sent[0][0], "oc_group")
            self.assertEqual(adapter._channel.sent[0][1], {"markdown": "agent reply"})
            self.assertEqual(agent.chat_calls[0]["user_id"], "ou_user")
            self.assertIn("Check system temperature", agent.chat_calls[0]["user_message"])

        asyncio.run(run_test())

    def test_on_message_routes_to_owner_event_loop(self):
        async def run_test():
            agent = _FakeAgent()
            adapter = FeishuAdapter(
                agent=agent,
                config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"),
            )
            adapter._owner_loop = asyncio.get_running_loop()
            message = object()
            routed = asyncio.Event()
            routed_messages = []

            def fake_create_dispatch_task(routed_message):
                routed_messages.append(routed_message)
                routed.set()

            adapter._create_dispatch_task = fake_create_dispatch_task

            async def invoke_from_other_loop():
                await adapter._on_message(message)

            await asyncio.to_thread(lambda: asyncio.run(invoke_from_other_loop()))
            await asyncio.wait_for(routed.wait(), timeout=1.0)

            self.assertEqual(routed_messages, [message])

        asyncio.run(run_test())

    def test_reconnect_callbacks_are_sync_sdk_hooks(self):
        adapter = FeishuAdapter(agent=_FakeAgent(), config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))

        self.assertFalse(asyncio.iscoroutinefunction(adapter._on_reconnecting))
        self.assertFalse(asyncio.iscoroutinefunction(adapter._on_reconnected))
        self.assertIsNone(adapter._on_reconnecting())
        self.assertIsNone(adapter._on_reconnected())

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
        for item in logging.getLogger("Lark").filters:
            item.filter(record)

        message = record.getMessage()
        self.assertIn("access_key=[redacted]", message)
        self.assertIn("ticket=[redacted]", message)
        self.assertNotIn("access_key=abc", message)
        self.assertNotIn("ticket=def", message)

    def test_direct_chat_reply_does_not_quote_source_message_or_forward_legacy_flags(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
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
        self.assertEqual(agent.chat_calls[0]["user_message"], "hello")
        self.assertNotIn("private", agent.chat_calls[0])
        self.assertEqual(adapter._channel.sent[0][2], {"uuid": "om_user"})

    def test_direct_chat_uses_resolved_name_for_agent_identity(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel()
        adapter._user_resolver = _FakeUserResolver({"ou_57abefd441c9b068703fa7b18543047e": "Alice"})
        msg = SimpleNamespace(
            chat_type="p2p",
            chat_id="oc_dm",
            message_id="om_user",
            sender_id="ou_57abefd441c9b068703fa7b18543047e",
            content_text="hello",
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.chat_calls[0]["user_id"], "Alice")
        self.assertNotIn("ou_57abefd441c9b068703fa7b18543047e", repr(agent.chat_calls[0]))

    def test_direct_image_message_downloads_resource_for_vision_chat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            resource_api = _FakeMessageResourceApi()
            client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource_api)))
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel(client=client)
            msg = SimpleNamespace(
                chat_type="p2p",
                chat_id="oc_dm",
                message_id="om_image",
                sender_id="ou_user",
                message_type="image",
                content='{"image_key":"img_test"}',
            )

            asyncio.run(adapter._dispatch(msg))

            saved_images = list((workspace_dir / "temp" / "images" / "feishu").glob("*.png"))
            self.assertEqual(resource_api.requests[0].message_id, "om_image")
            self.assertEqual(resource_api.requests[0].file_key, "img_test")
            self.assertEqual(resource_api.requests[0].type, "image")
            self.assertEqual(len(saved_images), 1)
            self.assertEqual(saved_images[0].read_bytes(), resource_api.data)
            self.assertTrue(agent.chat_calls[0]["image_source"].startswith("/api/workspace/blob?path=temp%2Fimages%2Ffeishu%2F"))
            self.assertIn("![Feishu image](/api/workspace/blob?path=temp%2Fimages%2Ffeishu%2F", agent.chat_calls[0]["user_message"])
            self.assertNotIn(str(workspace_dir), agent.chat_calls[0]["user_message"])

    def test_direct_large_image_message_compresses_before_workspace_and_model(self):
        original_max_bytes = feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_BYTES
        original_max_edge = feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_EDGE
        feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_BYTES = 180_000
        feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_EDGE = 512
        try:
            image_bytes = _noisy_jpeg_bytes()
            with tempfile.TemporaryDirectory() as tmpdir:
                workspace_dir = Path(tmpdir).resolve()
                agent = _FakeAgent()
                agent.workspace_dir = workspace_dir
                resource_api = _FakeMessageResourceApi(
                    data=image_bytes,
                    file_name="large.jpg",
                    content_type="image/jpeg",
                )
                client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource_api)))
                adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
                adapter._channel = _FakeChannel(client=client)
                msg = SimpleNamespace(
                    chat_type="p2p",
                    chat_id="oc_dm",
                    message_id="om_large_image",
                    sender_id="ou_user",
                    message_type="image",
                    content='{"image_key":"img_large"}',
                )

                asyncio.run(adapter._dispatch(msg))

                saved_images = list((workspace_dir / "temp" / "images" / "feishu").glob("*.jpg"))
                self.assertEqual(len(saved_images), 1)
                self.assertLess(saved_images[0].stat().st_size, len(image_bytes))
                self.assertLessEqual(saved_images[0].stat().st_size, feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_BYTES)
                self.assertEqual(agent.chat_calls[0]["attachments"][0]["size_bytes"], saved_images[0].stat().st_size)
        finally:
            feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_BYTES = original_max_bytes
            feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_EDGE = original_max_edge

    def test_direct_image_message_prefers_channel_download_resource(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            resource_api = _FakeMessageResourceApi(data=b"fallback")
            client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource_api)))
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeDownloadChannel(client=client)
            msg = SimpleNamespace(
                chat_type="p2p",
                chat_id="oc_dm",
                message_id="om_image_public_download",
                sender_id="ou_user",
                message_type="image",
                content='{"image_key":"img_test"}',
            )

            asyncio.run(adapter._dispatch(msg))

            saved_images = list((workspace_dir / "temp" / "images" / "feishu").glob("*.png"))
            self.assertEqual(adapter._channel.download_calls[0]["file_key"], "img_test")
            self.assertEqual(resource_api.requests, [])
            self.assertEqual(len(saved_images), 1)
            self.assertEqual(saved_images[0].read_bytes(), adapter._channel.download_data)

    def test_direct_file_message_downloads_resource_as_attachment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            resource_api = _FakeMessageResourceApi(
                data=b"%PDF-1.4\n",
                file_name="report.pdf",
                content_type="application/pdf",
            )
            client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource_api)))
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel(client=client)
            msg = SimpleNamespace(
                chat_type="p2p",
                chat_id="oc_dm",
                message_id="om_file",
                sender_id="ou_user",
                message_type="file",
                content='{"file_key":"file_test","file_name":"report.pdf"}',
            )

            asyncio.run(adapter._dispatch(msg))

            saved_files = list((workspace_dir / "temp" / "attachments" / "feishu").glob("*.pdf"))
            self.assertEqual(resource_api.requests[0].message_id, "om_file")
            self.assertEqual(resource_api.requests[0].file_key, "file_test")
            self.assertEqual(resource_api.requests[0].type, "file")
            self.assertEqual(len(saved_files), 1)
            self.assertEqual(saved_files[0].read_bytes(), resource_api.data)
            self.assertNotIn("image_source", agent.chat_calls[0])
            self.assertEqual(agent.chat_calls[0]["attachments"][0]["kind"], "file")
            self.assertTrue(agent.chat_calls[0]["attachments"][0]["path"].startswith("temp/attachments/feishu/"))

    def test_direct_image_message_strips_redundant_inline_feishu_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            resource_api = _FakeMessageResourceApi()
            client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource_api)))
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel(client=client)
            msg = SimpleNamespace(
                chat_type="p2p",
                chat_id="oc_dm",
                message_id="om_image_markdown",
                sender_id="ou_user",
                message_type="image",
                content='{"image_key":"img_test"}',
                content_text="![image](img_test)",
            )

            asyncio.run(adapter._dispatch(msg))

            user_message = agent.chat_calls[0]["user_message"]
            self.assertNotIn("![image](img_test)", user_message)
            self.assertEqual(user_message.count("![Feishu image](/api/workspace/blob?path=temp%2Fimages%2Ffeishu%2F"), 1)

    def test_direct_image_message_routes_attachment_when_provider_lacks_vision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            agent.supports_vision = False
            resource_api = _FakeMessageResourceApi()
            client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource_api)))
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel(client=client)
            msg = SimpleNamespace(
                chat_type="p2p",
                chat_id="oc_dm",
                message_id="om_image_no_vision",
                sender_id="ou_user",
                message_type="image",
                content='{"image_key":"img_test"}',
            )

            asyncio.run(adapter._dispatch(msg))

            saved_images = list((workspace_dir / "temp" / "images" / "feishu").glob("*.png"))
            self.assertEqual(len(agent.chat_calls), 1)
            self.assertNotIn("image_source", agent.chat_calls[0])
            self.assertEqual(agent.chat_calls[0]["attachments"][0]["kind"], "image")
            self.assertTrue(agent.chat_calls[0]["attachments"][0]["path"].startswith("temp/images/feishu/"))
            self.assertIn("![Feishu image](/api/workspace/blob?path=temp%2Fimages%2Ffeishu%2F", agent.chat_calls[0]["user_message"])
            self.assertEqual(len(saved_images), 1)
            self.assertEqual(saved_images[0].read_bytes(), resource_api.data)
            self.assertEqual(adapter._channel.sent[0][1], {"markdown": "agent reply"})
            self.assertEqual(adapter._channel.sent[0][2], {"uuid": "om_image_no_vision"})

    def test_direct_image_download_failure_replies_without_calling_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            resource_api = _FailingMessageResourceApi()
            client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource_api)))
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel(client=client)
            msg = SimpleNamespace(
                chat_type="p2p",
                chat_id="oc_dm",
                message_id="om_image_download_fail",
                sender_id="ou_user",
                message_type="image",
                content='{"image_key":"img_test"}',
            )

            asyncio.run(adapter._dispatch(msg))

            self.assertEqual(agent.chat_calls, [])
            self.assertEqual(adapter._channel.sent[0][1], {"markdown": "图片下载失败，请重试或重新发送。"})
            self.assertEqual(adapter._channel.sent[0][2], {"uuid": "om_image_download_fail"})

    def test_group_image_mention_keeps_workspace_blob_markdown_in_room_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            resource_api = _FakeMessageResourceApi()
            client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource_api)))
            adapter = FeishuAdapter(
                agent=agent,
                config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret", group_history_count=0),
            )
            adapter._channel = _FakeChannel(bot_open_id="ou_bot", client=client)
            msg = SimpleNamespace(
                chat_type="group",
                chat_id="oc_group",
                message_id="om_group_image",
                sender_id="ou_user",
                message_type="image",
                content='{"image_key":"img_test"}',
                mentioned_bot=True,
                mentions=[SimpleNamespace(open_id="ou_bot")],
            )

            asyncio.run(adapter._dispatch(msg))

            user_message = agent.chat_calls[0]["user_message"]
            self.assertIn("[room context]", user_message)
            self.assertIn("![Feishu image](/api/workspace/blob?path=temp%2Fimages%2Ffeishu%2F", user_message)
            self.assertTrue(agent.chat_calls[0]["image_source"].startswith("/api/workspace/blob?path=temp%2Fimages%2Ffeishu%2F"))
            self.assertNotIn(str(workspace_dir), user_message)

    def test_direct_chat_passes_explicit_sender_id_type_to_resolver(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel()
        resolver = _FakeUserResolver({"user_123": "Alice"})
        adapter._user_resolver = resolver
        msg = {
            "chat_type": "p2p",
            "chat_id": "oc_dm",
            "message_id": "om_user",
            "sender": {"id": "user_123", "id_type": "user_id", "sender_type": "user"},
            "content": SimpleNamespace(text="hello"),
        }

        asyncio.run(adapter._dispatch(SimpleNamespace(**msg)))

        self.assertEqual(agent.chat_calls[0]["user_id"], "Alice")
        self.assertEqual(resolver.calls[0], ("user_123", None, "user_id", "user"))

    def test_group_mention_reads_receive_v1_nested_sender_and_mention_ids(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret", group_history_count=0),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        resolver = _FakeUserResolver({"ou_sender": "Alice"})
        adapter._user_resolver = resolver
        msg = {
            "event": {
                "sender": {
                    "sender_id": {
                        "union_id": "on_sender",
                        "user_id": "sender_employee_id",
                        "open_id": "ou_sender",
                    },
                    "sender_type": "user",
                },
                "message": {
                    "message_id": "om_at",
                    "create_time": "1700000000000",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"@_user_1 @_user_2 hi"}',
                    "mentions": [
                        {
                            "key": "@_user_1",
                            "id": {"open_id": "ou_bot"},
                            "mentioned_type": "bot",
                            "name": "Mono",
                        },
                        {
                            "key": "@_user_2",
                            "id": {
                                "union_id": "on_tom",
                                "user_id": "tom_employee_id",
                                "open_id": "ou_tom",
                            },
                            "mentioned_type": "user",
                            "name": "Tom",
                        },
                    ],
                },
            }
        }

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.chat_calls), 1)
        self.assertEqual(resolver.calls[0], ("ou_sender", None, "open_id", "user"))
        user_message = agent.chat_calls[0]["user_message"]
        self.assertIn("Alice ", user_message)
        self.assertNotIn("Alice(ou_sender)", user_message)
        self.assertIn("@Mono @Tom hi", user_message)

    def test_group_mention_from_other_bot_sender_is_routed(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret", group_history_count=0),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        adapter._user_resolver = _FakeUserResolver({"ou_helper_bot": "Helper Bot"})
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_bot_at",
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_helper_bot"),
                sender_type="bot",
            ),
            content_text="@Mono ping",
            create_time="1700000000000",
            mentioned_bot=False,
            mentions=[SimpleNamespace(open_id="ou_bot", name="Mono")],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(len(agent.chat_calls), 1)
        self.assertIn("Helper Bot ", agent.chat_calls[0]["user_message"])
        self.assertNotIn("Helper Bot(ou_helper_bot)", agent.chat_calls[0]["user_message"])

    def test_direct_chat_does_not_use_id_like_sender_name_fallback(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel()
        msg = SimpleNamespace(
            chat_type="p2p",
            chat_id="oc_dm",
            message_id="om_user",
            sender_id="ou_user",
            sender_name="ou_user",
            content_text="hello",
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.chat_calls[0]["user_id"], "Feishu User")
        self.assertNotIn("ou_user", repr(agent.chat_calls[0]))

    def test_group_mention_detects_mentions_matching_bot_identity(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
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
        self.assertEqual(agent.chat_calls[0]["user_id"], "Feishu User")
        self.assertEqual(adapter._channel.sent[0][2], {"reply_to": "om_group_msg", "uuid": "om_group_msg"})
        self.assertNotIn("reply_in_thread", adapter._channel.sent[0][2])

    def test_group_mention_routes_to_chat_when_bot_identity_unresolved(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
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

    def test_group_mention_with_empty_text_still_replies(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
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

        self.assertIn("[room context]", agent.chat_calls[0]["user_message"])
        self.assertIn("room_id: oc_group", agent.chat_calls[0]["user_message"])
        self.assertIn(": The user mentioned you without adding any text.", agent.chat_calls[0]["user_message"])
        self.assertIn("[/room context]", agent.chat_calls[0]["user_message"])
        self.assertEqual(adapter._channel.sent[0][2], {"reply_to": "om_empty_at", "uuid": "om_empty_at"})

    def test_group_mention_matches_raw_nested_open_id_dict(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
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

    def test_unmentioned_group_message_is_ignored(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_ambient",
            sender_id="ou_user",
            sender_name="Alice",
            content_text="ambient group message",
            mentioned_bot=False,
            mentions=[],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.chat_calls, [])
        self.assertEqual(agent.observe_calls, [])
        self.assertEqual(adapter._channel.sent, [])

    def test_unmentioned_topic_message_is_ignored(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="topic",
            chat_id="oc_topic",
            message_id="om_topic",
            sender_id="ou_user",
            sender_name="Bob",
            content_text="hello everyone",
            mentioned_bot=False,
            mentions=[],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.chat_calls, [])
        self.assertEqual(agent.observe_calls, [])
        self.assertEqual(adapter._channel.sent, [])

    def test_bot_sender_message_is_ignored(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="p2p",
            chat_id="oc_dm",
            message_id="om_bot",
            sender_id="ou_bot",
            sender_type="bot",
            content_text="loop?",
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.chat_calls, [])
        self.assertEqual(adapter._channel.sent, [])

    def test_send_opts_never_uses_thread_reply(self):
        adapter = FeishuAdapter(agent=_FakeAgent(), config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))

        self.assertEqual(adapter._send_opts(message_id="om_x", is_group=False), {"uuid": "om_x"})
        opts = adapter._send_opts(message_id="om_x", is_group=True)

        self.assertEqual(opts, {"reply_to": "om_x", "uuid": "om_x"})
        self.assertNotIn("reply_in_thread", opts)

    def test_topic_group_anchors_reply_to_root_but_uuid_uses_trigger_message(self):
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="topic",
            chat_id="oc_topic",
            message_id="om_inside_thread",
            sender_id="ou_user",
            content_text="@Mono hi",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
            root_id="om_topic_root",
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(adapter._channel.sent[0][2], {"reply_to": "om_topic_root", "uuid": "om_inside_thread"})

    def test_message_uuid_hashes_long_ids(self):
        long_id = "om_" + "x" * 100
        uuid = FeishuAdapter._message_uuid(long_id)

        self.assertIsNotNone(uuid)
        self.assertLessEqual(len(uuid), 50)
        self.assertNotEqual(uuid, long_id)

    def test_markdown_workspace_blob_is_sent_as_feishu_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir)
            image_path = workspace_dir / "temp" / "images" / "result.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel()

            asyncio.run(adapter._send_markdown(
                chat_id="oc_dm",
                message_id="om_user",
                uuid_message_id="om_user",
                text="![Generated image](/api/workspace/blob?path=temp%2Fimages%2Fresult.png)",
                is_group=False,
            ))

        self.assertEqual(len(adapter._channel.sent), 1)
        sent_payload = adapter._channel.sent[0][1]
        self.assertEqual(sent_payload["image"]["source"], str(image_path.resolve()))
        self.assertEqual(adapter._channel.sent[0][2], {"uuid": "om_user:media:1"})

    def test_large_workspace_blob_is_compressed_before_feishu_upload(self):
        original_max_bytes = feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_BYTES
        original_max_edge = feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_EDGE
        feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_BYTES = 180_000
        feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_EDGE = 512
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                workspace_dir = Path(tmpdir).resolve()
                image_path = workspace_dir / "temp" / "attachments" / "web" / "large.jpg"
                image_path.parent.mkdir(parents=True)
                image_path.write_bytes(_noisy_jpeg_bytes())
                agent = _FakeAgent()
                agent.workspace_dir = workspace_dir
                adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
                adapter._channel = _FakeChannel()

                asyncio.run(adapter._send_markdown(
                    chat_id="oc_dm",
                    message_id="om_user",
                    uuid_message_id="om_user",
                    text="![Photo](/api/workspace/blob?path=temp%2Fattachments%2Fweb%2Flarge.jpg)",
                    is_group=False,
                ))

                sent_path = Path(adapter._channel.sent[0][1]["image"]["source"])
                self.assertNotEqual(sent_path, image_path.resolve())
                self.assertTrue(sent_path.is_file())
                self.assertTrue(sent_path.relative_to(workspace_dir).as_posix().startswith("temp/images/feishu/outbound/"))
                self.assertLess(sent_path.stat().st_size, image_path.stat().st_size)
                self.assertLessEqual(sent_path.stat().st_size, feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_BYTES)
        finally:
            feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_BYTES = original_max_bytes
            feishu_adapter_module._FEISHU_IMAGE_TRANSPORT_MAX_EDGE = original_max_edge

    def test_structured_attachment_is_sent_once_when_markdown_duplicates_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            image_path = workspace_dir / "temp" / "images" / "result.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            blob_url = "/api/workspace/blob?path=temp%2Fimages%2Fresult.png"
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel()

            asyncio.run(adapter._send_markdown(
                chat_id="oc_dm",
                message_id="om_user",
                uuid_message_id="om_user",
                text=f"Here\n\n![Generated image]({blob_url})",
                is_group=False,
                attachments=[
                    _FeishuOutboundAttachment(
                        kind="image",
                        path=image_path.resolve(),
                        blob_url=blob_url,
                    )
                ],
            ))

        self.assertEqual(len(adapter._channel.sent), 2)
        self.assertEqual(adapter._channel.sent[0][1], {"markdown": "Here"})
        self.assertEqual(adapter._channel.sent[1][1]["image"]["source"], str(image_path.resolve()))
        self.assertEqual(adapter._channel.sent[1][2], {"uuid": "om_user:media:1"})

    def test_message_done_structured_attachment_is_sent_without_markdown_dependency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            image_path = workspace_dir / "temp" / "images" / "result.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            agent = _AttachmentEventAgent(
                content="Here is the processed image.",
                attachments=[{
                    "kind": "image",
                    "path": "temp/images/result.png",
                    "blob_url": "/api/workspace/blob?path=temp%2Fimages%2Fresult.png",
                    "mime_type": "image/png",
                    "file_name": "result.png",
                    "caption": "",
                }],
            )
            agent.workspace_dir = workspace_dir
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel()

            asyncio.run(adapter._dispatch(SimpleNamespace(
                chat_type="p2p",
                chat_id="oc_dm",
                message_id="om_user",
                sender_id="ou_user",
                content_text="send it",
            )))

        self.assertEqual(len(adapter._channel.sent), 2)
        self.assertEqual(adapter._channel.sent[0][1], {"markdown": "Here is the processed image."})
        self.assertEqual(adapter._channel.sent[1][1]["image"]["source"], str(image_path.resolve()))

    def test_image_attachment_retries_then_falls_back_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            image_path = workspace_dir / "result.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel(results=[_failed_send_result(), _failed_send_result()])

            asyncio.run(adapter._send_markdown(
                chat_id="oc_dm",
                message_id="om_user",
                uuid_message_id="om_user",
                text="![Generated image](/api/workspace/blob?path=result.png)",
                is_group=False,
            ))

        self.assertEqual(len(adapter._channel.sent), 3)
        self.assertIn("image", adapter._channel.sent[0][1])
        self.assertIn("image", adapter._channel.sent[1][1])
        self.assertEqual(adapter._channel.sent[0][2], {"uuid": "om_user:media:1"})
        self.assertEqual(adapter._channel.sent[1][2], {"uuid": "om_user:media:1"})
        self.assertEqual(adapter._channel.sent[2][1]["file"]["source"], str(image_path.resolve()))
        self.assertEqual(adapter._channel.sent[2][2], {"uuid": "om_user:media:1:file"})

    def test_attachment_failure_sends_workspace_blob_notice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_dir = Path(tmpdir).resolve()
            report_path = workspace_dir / "reports" / "out.pdf"
            report_path.parent.mkdir(parents=True)
            report_path.write_bytes(b"%PDF")
            agent = _FakeAgent()
            agent.workspace_dir = workspace_dir
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel(results=[_failed_send_result(), _failed_send_result()])

            asyncio.run(adapter._send_markdown(
                chat_id="oc_dm",
                message_id="om_user",
                uuid_message_id="om_user",
                text="[Report](/api/workspace/blob?path=reports%2Fout.pdf)",
                is_group=False,
            ))

        self.assertEqual(len(adapter._channel.sent), 3)
        self.assertIn("file", adapter._channel.sent[0][1])
        self.assertIn("file", adapter._channel.sent[1][1])
        self.assertIn("文件发送失败", adapter._channel.sent[2][1]["markdown"])
        self.assertIn("/api/workspace/blob?path=reports%2Fout.pdf", adapter._channel.sent[2][1]["markdown"])

    def test_message_text_falls_back_to_content_text_for_sdk_batches(self):
        msg = SimpleNamespace(content_text="", content=SimpleNamespace(text="merged text"))

        self.assertEqual(FeishuAdapter._message_text(msg), "merged text")

    def test_on_message_returns_before_slow_chat_finishes(self):
        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()
            agent = _SlowChatAgent(started=started, release=release)
            adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
            adapter._channel = _FakeChannel(bot_open_id="ou_bot")
            msg = SimpleNamespace(
                chat_type="p2p",
                chat_id="oc_dm",
                message_id="om_slow_chat",
                sender_id="ou_user",
                content_text="slow direct message",
            )

            await asyncio.wait_for(adapter._on_message(msg), timeout=0.05)
            await asyncio.wait_for(started.wait(), timeout=1.0)
            self.assertEqual(len(adapter._processing_tasks), 1)
            release.set()
            while adapter._processing_tasks:
                await asyncio.sleep(0.01)
            self.assertEqual(len(agent.chat_calls), 1)

        asyncio.run(scenario())


class FeishuGroupHistoryTests(unittest.TestCase):
    def _patch_fetcher(self, *, records=None, error=None):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        captured: dict = {}
        original = FeishuHistoryFetcher.fetch_recent_messages

        async def patched(self_fetcher, **kwargs):  # noqa: ARG001
            captured["kwargs"] = kwargs
            if error is not None:
                raise error
            return list(records or [])

        FeishuHistoryFetcher.fetch_recent_messages = patched
        self.addCleanup(setattr, FeishuHistoryFetcher, "fetch_recent_messages", original)
        return captured

    def test_group_mention_includes_recent_history_in_chat_input(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp

        captured = self._patch_fetcher(
            records=[
                FeishuMessageRecord("om_old1", "ou_alice", "Alice", "hi all", 1700000000000, source="chat"),
                FeishuMessageRecord("om_old2", "ou_bob", "Bob", "ready?", 1700000001000, source="chat"),
            ]
        )
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        adapter._user_resolver = _FakeUserResolver({"ou_user": "Carol"})
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_at",
            sender_id="ou_user",
            sender_name="User",
            content_text="@Mono what's up",
            create_time="1700000002000",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
            conversation=SimpleNamespace(thread_id=None),
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(agent.observe_calls, [])
        user_message = agent.chat_calls[0]["user_message"]
        self.assertIn("[room context]", user_message)
        self.assertIn("room_id: oc_group", user_message)
        self.assertNotIn("room_name:", user_message)
        self.assertIn("[/room context]", user_message)
        self.assertNotIn("[Feishu group context]", user_message)
        self.assertNotIn("The following recent group messages are context only", user_message)
        self.assertNotIn("[Current mention]", user_message)
        self.assertIn(f"Alice {format_feishu_timestamp(1700000000000)}: hi all", user_message)
        self.assertIn(f"Bob {format_feishu_timestamp(1700000001000)}: ready?", user_message)
        self.assertIn(f"Carol {format_feishu_timestamp(1700000002000)}: @Mono what's up", user_message)
        self.assertNotIn("metadata", agent.chat_calls[0])
        self.assertEqual(captured["kwargs"]["chat_id"], "oc_group")
        self.assertEqual(captured["kwargs"]["current_message_id"], "om_at")
        self.assertIsNone(captured["kwargs"]["thread_id"])
        self.assertEqual(captured["kwargs"]["history_count"], 10)
        self.assertFalse(captured["kwargs"]["show_sender_ids"])

    def test_group_mention_can_show_sender_ids_when_enabled(self):
        from xagent.integrations.feishu.history import format_feishu_timestamp

        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret", group_history_count=0),
            show_sender_ids=True,
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        adapter._user_resolver = _FakeUserResolver({"ou_user": "Telos"})
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_at",
            sender_id="ou_user",
            content_text="@Mono hey",
            create_time="1700000000000",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertIn(
            f"Telos(ou_user) {format_feishu_timestamp(1700000000000)}: @Mono hey",
            agent.chat_calls[0]["user_message"],
        )

    def test_topic_mention_passes_thread_id_to_fetcher(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord

        captured = self._patch_fetcher(records=[FeishuMessageRecord("om_root", "ou_alice", "Alice", "topic seed", 1, source="thread")])
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="topic",
            chat_id="oc_topic",
            message_id="om_topic_at",
            sender_id="ou_user",
            content_text="@Mono ?",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
            conversation=SimpleNamespace(thread_id="omt_thread_1"),
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(captured["kwargs"]["thread_id"], "omt_thread_1")
        self.assertEqual(len(agent.chat_calls), 1)

    def test_group_history_count_zero_skips_fetch(self):
        captured = self._patch_fetcher(records=[])
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret", group_history_count=0),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_at",
            sender_id="ou_user",
            content_text="@_user_1 hi",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot"), SimpleNamespace(key="@_user_1", name="Tom")],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertNotIn("kwargs", captured)
        self.assertIn("[room context]", agent.chat_calls[0]["user_message"])
        self.assertIn("room_id: oc_group", agent.chat_calls[0]["user_message"])
        self.assertIn(": @Tom hi", agent.chat_calls[0]["user_message"])
        self.assertIn("[/room context]", agent.chat_calls[0]["user_message"])

    def test_group_mention_uses_chat_name_for_room_context_when_available(self):
        from xagent.integrations.feishu.history import format_feishu_timestamp

        class FakeChatApi:
            def __init__(self):
                self.requests = []

            def get(self, request):
                self.requests.append(request)
                return SimpleNamespace(success=lambda: True, data=SimpleNamespace(name="Project Room"))

        chat_api = FakeChatApi()
        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(chat=chat_api)))
        agent = _FakeAgent()
        adapter = FeishuAdapter(
            agent=agent,
            config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret", group_history_count=0),
        )
        adapter._channel = _FakeChannel(bot_open_id="ou_bot", client=client)
        adapter._user_resolver = _FakeUserResolver({"ou_user": "Telos"})
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_at",
            sender_id="ou_user",
            content_text="@Mono hey",
            create_time="1700000000000",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertEqual(chat_api.requests[0].chat_id, "oc_group")
        self.assertEqual(chat_api.requests[0].user_id_type, "open_id")
        self.assertIn("[room context]", agent.chat_calls[0]["user_message"])
        self.assertIn("room_name: Project Room", agent.chat_calls[0]["user_message"])
        self.assertIn("room_id: oc_group", agent.chat_calls[0]["user_message"])
        self.assertIn(f"Telos {format_feishu_timestamp(1700000000000)}: @Mono hey", agent.chat_calls[0]["user_message"])

    def test_history_failure_still_replies_to_current_mention(self):
        self._patch_fetcher(error=RuntimeError("no scope"))
        agent = _FakeAgent()
        adapter = FeishuAdapter(agent=agent, config=FeishuAdapterConfig(app_id="cli_test", app_secret="secret"))
        adapter._channel = _FakeChannel(bot_open_id="ou_bot")
        msg = SimpleNamespace(
            chat_type="group",
            chat_id="oc_group",
            message_id="om_at",
            sender_id="ou_user",
            content_text="@Mono hi",
            mentioned_bot=True,
            mentions=[SimpleNamespace(open_id="ou_bot")],
        )

        asyncio.run(adapter._dispatch(msg))

        self.assertIn("[room context]", agent.chat_calls[0]["user_message"])
        self.assertIn("room_id: oc_group", agent.chat_calls[0]["user_message"])
        self.assertIn(": @Mono hi", agent.chat_calls[0]["user_message"])
        self.assertEqual(adapter._channel.sent[0][2], {"reply_to": "om_at", "uuid": "om_at"})


class FeishuHistoryFetcherTests(unittest.TestCase):
    def test_infer_user_id_type_uses_feishu_id_prefixes(self):
        from xagent.integrations.feishu.users import extract_feishu_id, infer_feishu_id_type, infer_user_id_type, safe_display_name

        self.assertEqual(infer_feishu_id_type("cli_agent"), "app_id")
        self.assertEqual(infer_user_id_type("ou_57abefd441c9b068703fa7b18543047e"), "open_id")
        self.assertEqual(infer_user_id_type("on_union"), "union_id")
        self.assertEqual(infer_user_id_type("plain_user_id"), "user_id")
        self.assertEqual(
            extract_feishu_id({"sender_id": {"user_id": "employee_id", "open_id": "ou_nested"}}),
            ("ou_nested", "open_id"),
        )
        self.assertEqual(safe_display_name(" Alice "), "Alice")
        self.assertIsNone(safe_display_name("ou_57abefd441c9b068703fa7b18543047e"))
        self.assertIsNone(safe_display_name("cli_aa8be4ff193b9cdd"))

    def test_render_content_extracts_text_payload(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        self.assertEqual(FeishuHistoryFetcher._render_content("text", '{"text": "hello world"}'), "hello world")

    def test_render_content_handles_rich_post(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher
        payload = {
            "title": "Notice",
            "content": [
                [{"tag": "text", "text": "Hi "}, {"tag": "at", "user_name": "Alice"}, {"tag": "text", "text": ", see this:"}],
                [{"tag": "a", "text": "link", "href": "https://example.com"}],
            ],
        }

        rendered = FeishuHistoryFetcher._render_content("post", payload)

        self.assertIn("Notice", rendered)
        self.assertIn("Hi @Alice, see this:", rendered)
        self.assertIn("link", rendered)

    def test_render_content_unknown_type_returns_placeholder(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        self.assertEqual(FeishuHistoryFetcher._render_content("image", '{"image_key": "x"}'), "[image]")

    def test_normalize_item_from_dict_payload(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher
        item = {
            "message_id": "om_1",
            "msg_type": "text",
            "create_time": "1700000000000",
            "sender": {"id": "ou_alice", "name": "Alice"},
            "body": {"content": '{"text": "hi"}'},
        }

        rec = FeishuHistoryFetcher._normalize_item(item, source="chat")

        self.assertIsNotNone(rec)
        self.assertEqual(rec.message_id, "om_1")
        self.assertEqual(rec.sender_id, "ou_alice")
        self.assertEqual(rec.sender_name, "Alice")
        self.assertEqual(rec.text, "hi")
        self.assertEqual(rec.create_time_ms, 1700000000000)
        self.assertEqual(rec.source, "chat")

    def test_normalize_item_keeps_sender_type_and_id_type(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher
        item = {
            "message_id": "om_agent",
            "msg_type": "text",
            "create_time": "1700000000000",
            "sender": {
                "id": "cli_john_agent",
                "id_type": "app_id",
                "sender_type": "app",
            },
            "body": {"content": '{"text": "agent answer"}'},
        }

        rec = FeishuHistoryFetcher._normalize_item(item, source="chat")

        self.assertIsNotNone(rec)
        self.assertEqual(rec.sender_id, "cli_john_agent")
        self.assertEqual(rec.sender_id_type, "app_id")
        self.assertEqual(rec.sender_type, "app")

    def test_normalize_item_replaces_mention_keys_with_names(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        item = {
            "message_id": "om_mention",
            "msg_type": "text",
            "create_time": "1700000000000",
            "sender": {"id": "ou_alice", "name": "Alice"},
            "body": {"content": '{"text": "@_user_1 hey"}'},
            "mentions": [{"key": "@_user_1", "id": "ou_tom", "name": "Tom"}],
        }

        rec = FeishuHistoryFetcher._normalize_item(item, source="chat")

        self.assertIsNotNone(rec)
        self.assertEqual(rec.text, "@Tom hey")

    def test_normalize_item_can_render_mention_ids_when_enabled(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        item = {
            "message_id": "om_mention",
            "msg_type": "text",
            "create_time": "1700000000000",
            "sender": {"sender_id": {"open_id": "ou_alice"}, "name": "Alice"},
            "body": {"content": '{"text": "@_user_1 hey"}'},
            "mentions": [
                {
                    "key": "@_user_1",
                    "id": {"open_id": "ou_tom", "user_id": "tom_employee_id"},
                    "name": "Tom",
                }
            ],
        }

        rec = FeishuHistoryFetcher._normalize_item(item, source="chat", show_sender_ids=True)

        self.assertIsNotNone(rec)
        self.assertEqual(rec.sender_id, "ou_alice")
        self.assertEqual(rec.sender_id_type, "open_id")
        self.assertEqual(rec.text, "@Tom(ou_tom) hey")

    def test_normalize_item_skips_deleted_messages(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher
        item = {
            "message_id": "om_1",
            "deleted": True,
            "msg_type": "text",
            "sender": {"id": "ou_alice"},
            "body": {"content": '{"text": "hi"}'},
        }

        self.assertIsNone(FeishuHistoryFetcher._normalize_item(item, source="chat"))

    def test_fetch_recent_messages_gracefully_handles_missing_channel_attrs(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher

        fetcher = FeishuHistoryFetcher(channel=SimpleNamespace())
        records = asyncio.run(
            fetcher.fetch_recent_messages(
                chat_id="oc_x",
                current_message_id="om_x",
                thread_id="omt_thread",
                history_count=5,
            )
        )

        self.assertEqual(records, [])

    def test_history_record_names_resolve_through_user_resolver(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher, FeishuMessageRecord

        fetcher = FeishuHistoryFetcher(
            channel=SimpleNamespace(),
            user_resolver=_FakeUserResolver({"ou_alice": "Alice From Contact"}),
        )

        records = asyncio.run(
            fetcher._resolve_record_names(
                [FeishuMessageRecord("om_1", "ou_alice", None, "hi", 1)]
            )
        )

        self.assertEqual(records[0].sender_name, "Alice From Contact")

    def test_history_record_names_pass_sender_metadata_to_resolver(self):
        from xagent.integrations.feishu.history import FeishuHistoryFetcher, FeishuMessageRecord

        resolver = _FakeUserResolver({"cli_john_agent": "john的智能助手"})
        fetcher = FeishuHistoryFetcher(
            channel=SimpleNamespace(),
            user_resolver=resolver,
        )

        records = asyncio.run(
            fetcher._resolve_record_names(
                [
                    FeishuMessageRecord(
                        "om_1",
                        "cli_john_agent",
                        None,
                        "你好呀",
                        1,
                        sender_type="app",
                        sender_id_type="app_id",
                    )
                ]
            )
        )

        self.assertEqual(records[0].sender_name, "john的智能助手")
        self.assertEqual(resolver.calls[0], ("cli_john_agent", None, "app_id", "app"))

    def test_format_group_history_does_not_expose_open_id_fallback(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_group_history

        text = format_group_history([FeishuMessageRecord("om_1", "ou_alice", "ou_alice", "hi", 1)])

        self.assertEqual(text, f"Feishu User {format_feishu_timestamp(1)}: hi")
        self.assertNotIn("ou_alice", text)

    def test_format_group_history_hides_sender_id_by_default(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_group_history

        text = format_group_history([FeishuMessageRecord("om_1", "ou_alice", "Alice", "hi", 1)])

        self.assertEqual(text, f"Alice {format_feishu_timestamp(1)}: hi")

    def test_format_group_history_appends_sender_id_when_enabled(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_group_history

        text = format_group_history(
            [FeishuMessageRecord("om_1", "ou_alice", "Alice", "hi", 1)],
            show_sender_ids=True,
        )

        self.assertEqual(text, f"Alice(ou_alice) {format_feishu_timestamp(1)}: hi")

    def test_format_group_history_uses_sender_id_when_user_name_unresolved(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_group_history

        text = format_group_history(
            [FeishuMessageRecord("om_1", "ou_unknown", None, "hi", 1, sender_type="user")],
            show_sender_ids=True,
        )

        self.assertEqual(text, f"Feishu User(ou_unknown) {format_feishu_timestamp(1)}: hi")

    def test_format_group_history_marks_unresolved_app_sender_as_bot_with_id(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_group_history

        text = format_group_history(
            [FeishuMessageRecord("om_app", "cli_other_agent", None, "hello", 1, sender_type="app")],
            show_sender_ids=True,
        )

        self.assertEqual(text, f"Feishu Bot(cli_other_agent) {format_feishu_timestamp(1)}: hello")

    @unittest.skipIf(LogLevel is None, "lark-oapi is not installed")
    def test_user_resolver_calls_contact_v3_user_get(self):
        from xagent.integrations.feishu.users import FeishuUserResolver

        class FakeUserApi:
            def __init__(self):
                self.requests = []

            def get(self, request):
                self.requests.append(request)
                user = SimpleNamespace(name="", nickname="Nickname", en_name="English Name")
                return SimpleNamespace(success=lambda: True, data=SimpleNamespace(user=user))

        user_api = FakeUserApi()
        client = SimpleNamespace(contact=SimpleNamespace(v3=SimpleNamespace(user=user_api)))
        resolver = FeishuUserResolver(SimpleNamespace(client=client))

        name = asyncio.run(resolver.resolve_name("ou_57abefd441c9b068703fa7b18543047e"))
        cached_name = asyncio.run(resolver.resolve_name("ou_57abefd441c9b068703fa7b18543047e"))

        self.assertEqual(name, "Nickname")
        self.assertEqual(cached_name, "Nickname")
        self.assertEqual(len(user_api.requests), 1)
        self.assertEqual(user_api.requests[0].user_id, "ou_57abefd441c9b068703fa7b18543047e")
        self.assertEqual(user_api.requests[0].user_id_type, "open_id")
        self.assertEqual(user_api.requests[0].department_id_type, "open_department_id")

    @unittest.skipIf(LogLevel is None, "lark-oapi is not installed")
    def test_user_resolver_uses_explicit_user_id_type(self):
        from xagent.integrations.feishu.users import FeishuUserResolver

        class FakeUserApi:
            def __init__(self):
                self.requests = []

            def get(self, request):
                self.requests.append(request)
                user = SimpleNamespace(name="Plain User", nickname="", en_name="")
                return SimpleNamespace(success=lambda: True, data=SimpleNamespace(user=user))

        user_api = FakeUserApi()
        client = SimpleNamespace(contact=SimpleNamespace(v3=SimpleNamespace(user=user_api)))
        resolver = FeishuUserResolver(SimpleNamespace(client=client))

        name = asyncio.run(resolver.resolve_name("plain_user_id", id_type="open_id", sender_type="user"))

        self.assertEqual(name, "Plain User")
        self.assertEqual(user_api.requests[0].user_id_type, "open_id")

    @unittest.skipIf(LogLevel is None, "lark-oapi is not installed")
    def test_user_resolver_uses_contact_lookup_for_bot_open_id(self):
        from xagent.integrations.feishu.users import FeishuUserResolver

        class FakeUserApi:
            def __init__(self):
                self.requests = []

            def get(self, request):
                self.requests.append(request)
                user = SimpleNamespace(name="Helper Bot", nickname="", en_name="")
                return SimpleNamespace(success=lambda: True, data=SimpleNamespace(user=user))

        user_api = FakeUserApi()
        client = SimpleNamespace(contact=SimpleNamespace(v3=SimpleNamespace(user=user_api)))
        resolver = FeishuUserResolver(SimpleNamespace(client=client))

        name = asyncio.run(resolver.resolve_name("ou_helper_bot", id_type="open_id", sender_type="bot"))

        self.assertEqual(name, "Helper Bot")
        self.assertEqual(user_api.requests[0].user_id, "ou_helper_bot")
        self.assertEqual(user_api.requests[0].user_id_type, "open_id")

    @unittest.skipIf(LogLevel is None, "lark-oapi is not installed")
    def test_user_resolver_calls_application_get_for_app_sender(self):
        from xagent.integrations.feishu.users import FeishuUserResolver

        class FakeApplicationApi:
            def __init__(self):
                self.requests = []

            def get(self, request):
                self.requests.append(request)
                app = SimpleNamespace(app_name="john的智能助手", name="")
                return SimpleNamespace(success=lambda: True, data=SimpleNamespace(app=app))

        app_api = FakeApplicationApi()
        client = SimpleNamespace(application=SimpleNamespace(v6=SimpleNamespace(application=app_api)))
        resolver = FeishuUserResolver(SimpleNamespace(client=client))

        name = asyncio.run(resolver.resolve_name("cli_john_agent", id_type="app_id", sender_type="app"))
        cached_name = asyncio.run(resolver.resolve_name("cli_john_agent", id_type="app_id", sender_type="app"))

        self.assertEqual(name, "john的智能助手")
        self.assertEqual(cached_name, "john的智能助手")
        self.assertEqual(len(app_api.requests), 1)
        self.assertEqual(app_api.requests[0].app_id, "cli_john_agent")
        self.assertEqual(app_api.requests[0].lang, "zh_cn")
        self.assertEqual(app_api.requests[0].user_id_type, "open_id")

    def test_format_group_history_marks_bot_sender_as_you(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_group_history

        text = format_group_history(
            [FeishuMessageRecord("om_bot", "ou_bot", "Mono", "previous answer", 1)],
            bot_open_id="ou_bot",
        )

        self.assertEqual(text, f"you {format_feishu_timestamp(1)}: previous answer")

    def test_format_room_context_wraps_group_history(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_room_context

        text = format_room_context(
            "oc_group",
            [FeishuMessageRecord("om_1", "ou_alice", "Alice", "hi", 1)],
            room_name="Agent Test",
        )

        self.assertEqual(
            text,
            f"[room context]\nroom_name: Agent Test\nroom_id: oc_group\n\nAlice {format_feishu_timestamp(1)}: hi\n[/room context]",
        )

    def test_format_room_context_omits_room_name_when_missing(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_room_context

        text = format_room_context(
            "oc_group",
            [FeishuMessageRecord("om_1", "ou_alice", "Alice", "hi", 1)],
        )

        self.assertEqual(
            text,
            f"[room context]\nroom_id: oc_group\n\nAlice {format_feishu_timestamp(1)}: hi\n[/room context]",
        )

    def test_format_room_context_can_show_sender_ids_when_enabled(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_room_context

        text = format_room_context(
            "oc_group",
            [FeishuMessageRecord("om_1", "ou_alice", "Alice", "hi", 1)],
            show_sender_ids=True,
        )

        self.assertEqual(
            text,
            f"[room context]\nroom_id: oc_group\n\nAlice(ou_alice) {format_feishu_timestamp(1)}: hi\n[/room context]",
        )

    def test_format_group_history_marks_bot_app_id_as_you(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_group_history

        text = format_group_history(
            [FeishuMessageRecord("om_bot", "cli_aa8be4ff193b9cdd", None, "hey", 1)],
            bot_app_id="cli_aa8be4ff193b9cdd",
        )

        self.assertEqual(text, f"you {format_feishu_timestamp(1)}: hey")

    def test_format_group_history_marks_bot_app_id_name_as_you(self):
        from xagent.integrations.feishu.history import FeishuMessageRecord, format_feishu_timestamp, format_group_history

        text = format_group_history(
            [FeishuMessageRecord("om_bot", "", "cli_aa8be4ff193b9cdd", "where", 1)],
            bot_app_id="cli_aa8be4ff193b9cdd",
        )

        self.assertEqual(text, f"you {format_feishu_timestamp(1)}: where")


if __name__ == "__main__":
    unittest.main()
