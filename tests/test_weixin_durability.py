"""Crash boundaries and acknowledgements for durable Weixin delivery."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from tests import test_weixin_adapter as fixtures
from tests.test_weixin_lifecycle import PollingClient
from xagent.core.runtime import ContactEntry, SubconsciousDelivery
from xagent.core.runtime.tasks import AsyncTaskScheduler
from xagent.integrations.weixin.client import WeixinApiError, WeixinClient
from xagent.integrations.weixin.state import WeixinStateStore


class WeixinDurabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter, _, self.client, self.store = fixtures.WeixinAdapterTests()._adapter(self.root, text_max_chars=12)
        self.account = self.adapter._credentials.account_id
        self.now = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)

    def scheduler(self):
        scheduler = AsyncTaskScheduler(self.root / "tasks", can_handle=self.adapter._can_handle_scheduled_task,
            execute=self.adapter.execute_scheduled_task, deliver=self.adapter.deliver_scheduled_task,
            now_provider=lambda: self.now)
        self.adapter.enqueue_outbound = scheduler.enqueue_outbound_message
        self.adapter.delivery_context_updated = scheduler.notify_delivery_context
        return scheduler

    def delivery(self):
        return SubconsciousDelivery(content="first line\nsecond line", internal_content="private",
            recipient=ContactEntry("weixin", "owner@im.wechat", {"user_id": "owner@im.wechat"}, ""),
            created_at=self.now)

    def inbound_rows(self):
        with self.store.inbox() as connection:
            return connection.execute("SELECT status,data,error FROM messages ORDER BY rowid").fetchall()

    async def test_cursor_and_messages_roll_back_together(self):
        self.store.save_sync_buf(self.account, "old")
        with self.assertRaises(TypeError):
            self.store.commit_poll(self.account, "new", [{"message_id": 1}, {"invalid": object()}])
        self.assertEqual(self.store.load_sync_buf(self.account), "old")
        self.assertEqual(self.inbound_rows(), [])

    async def test_cancel_after_poll_commit_then_restart_replays_queued_message_once(self):
        message = {"message_id": 1, "context_token": "new"}
        self.client.get_updates = AsyncMock(return_value={"ret": 0, "get_updates_buf": "new", "msgs": [message]})
        self.adapter._dispatch_inbound = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.adapter._poll_loop()
        self.assertEqual(self.store.load_sync_buf(self.account), "new")
        self.assertEqual(self.inbound_rows()[0][0], "queued")
        adapter, _, _, _ = fixtures.WeixinAdapterTests()._adapter(self.root)
        adapter.client = PollingClient({"ret": 0, "get_updates_buf": "next", "msgs": [message]})
        processed = asyncio.Event()
        async def process(_, **kwargs):
            processed.set()
        adapter._process_message = AsyncMock(side_effect=process)
        running = asyncio.create_task(adapter.run())
        try:
            await asyncio.wait_for(adapter.client.polling.wait(), 1)
            await asyncio.wait_for(processed.wait(), 1)
        finally:
            await adapter.stop()
            await asyncio.wait_for(running, 1)
        adapter._process_message.assert_awaited_once_with(message, inbound_key="1")
        self.assertEqual(self.inbound_rows()[0][:2], ("done", "{}"))

    async def test_interrupted_processing_is_retained_for_review_not_replayed(self):
        self.store.commit_poll(self.account, "new", [{"message_id": 1}])
        key, _ = self.store.pending_inbound(self.account)[0]
        self.assertTrue(self.store.claim_inbound(self.account, key))
        restarted = WeixinStateStore(self.root)
        restarted.recover_inbound(self.account)
        self.assertEqual(restarted.pending_inbound(self.account), [])
        self.assertEqual(self.inbound_rows()[0][0], "needs_review")

    async def test_processing_failure_is_retained_for_review(self):
        self.store.commit_poll(self.account, "new", [{"message_id": 1}])
        key, message = self.store.pending_inbound(self.account)[0]
        self.adapter._process_message = AsyncMock(side_effect=RuntimeError("tool failed"))
        with self.assertRaises(RuntimeError):
            await self.adapter._process_inbound(message, key)
        self.assertEqual(self.inbound_rows()[0][0], "needs_review")

    async def test_cancel_while_waiting_for_user_lock_remains_queued(self):
        message = {"message_id": 1, "message_type": 1, "from_user_id": "owner@im.wechat",
                   "context_token": "ctx", "item_list": [{"type": 1, "text_item": {"text": "hello"}}]}
        self.store.commit_poll(self.account, "new", [message])
        key, _ = self.store.pending_inbound(self.account)[0]
        lock = asyncio.Lock()
        await lock.acquire()
        self.adapter._chat_locks["owner@im.wechat"] = SimpleNamespace(lock=lock, waiters=0)
        running = asyncio.create_task(self.adapter._process_inbound(message, key))
        await asyncio.sleep(0)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        lock.release()
        self.assertEqual(self.inbound_rows()[0][0], "queued")
        adapter, _, _, _ = fixtures.WeixinAdapterTests()._adapter(self.root)
        adapter._handle_dm = AsyncMock()
        await adapter._process_inbound(message, key)
        adapter._handle_dm.assert_awaited_once()
        self.assertEqual(self.inbound_rows()[0][0], "done")

    async def test_cancel_after_processing_starts_requires_review(self):
        message = {"message_id": 1, "message_type": 1, "from_user_id": "owner@im.wechat",
                   "context_token": "ctx", "item_list": [{"type": 1, "text_item": {"text": "hello"}}]}
        self.store.commit_poll(self.account, "new", [message])
        key, _ = self.store.pending_inbound(self.account)[0]
        started = asyncio.Event()
        async def handle(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.adapter._handle_dm = handle
        running = asyncio.create_task(self.adapter._process_inbound(message, key))
        await asyncio.wait_for(started.wait(), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(self.inbound_rows()[0][0], "needs_review")

    async def test_proactive_partial_delivery_resumes_after_restart_without_repeating_text(self):
        scheduler = self.scheduler()
        self.adapter._context_tokens["owner@im.wechat"] = "old"
        calls = []
        async def send(**kwargs):
            calls.append((kwargs["text"], kwargs["context_token"]))
            if kwargs["text"] == "second line" and kwargs["context_token"] == "old":
                raise WeixinApiError("prepare failed", code=-99)
            return {"ret": 0}
        self.client.send_text_message = send
        result = await self.adapter.deliver_subconscious_message(self.delivery())
        await scheduler.tick()
        self.assertEqual(scheduler.store.list_runs(result["task_id"])[0]["status"], "waiting_context")
        scheduler = self.scheduler()
        scheduler.recover_running_tasks()
        again = await self.adapter.deliver_subconscious_message(self.delivery())
        self.assertEqual(result, again)
        self.assertEqual(len(scheduler.store.tasks()), 1)
        self.adapter._remember_context("owner@im.wechat", "new")
        await scheduler.tick()
        self.assertEqual(calls, [("first line", "old"), ("second line", "old"), ("second line", "new")])
        self.assertEqual(scheduler.store.list_runs(result["task_id"])[0]["status"], "succeeded")

    async def test_proactive_lost_acknowledgement_is_not_retried(self):
        scheduler = self.scheduler()
        self.adapter._context_tokens["owner@im.wechat"] = "ctx"
        self.client.send_text_message = AsyncMock(side_effect=TimeoutError("lost acknowledgement"))
        result = await self.adapter.deliver_subconscious_message(self.delivery())
        await scheduler.tick()
        await self.adapter.deliver_subconscious_message(self.delivery())
        await scheduler.tick()
        self.client.send_text_message.assert_awaited_once()
        self.assertEqual(scheduler.store.list_runs(result["task_id"])[0]["status"], "needs_review")

    async def test_invalid_response_never_marks_proactive_send_successful(self):
        for body in ("", "[]", "null", "{}", '{"ret":false}'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as root:
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, text=body))) as http:
                    adapter, _, _, _ = fixtures.WeixinAdapterTests()._adapter(root)
                    adapter.client = WeixinClient(base_url="https://example.test", http_client=http)
                    adapter._context_tokens["owner@im.wechat"] = "ctx"
                    scheduler = AsyncTaskScheduler(Path(root) / "tasks", can_handle=adapter._can_handle_scheduled_task,
                        execute=adapter.execute_scheduled_task, deliver=adapter.deliver_scheduled_task,
                        now_provider=lambda: self.now)
                    adapter.enqueue_outbound = scheduler.enqueue_outbound_message
                    result = await adapter.deliver_subconscious_message(self.delivery())
                    await scheduler.tick()
                    self.assertEqual(scheduler.store.list_runs(result["task_id"])[0]["status"], "needs_review")
