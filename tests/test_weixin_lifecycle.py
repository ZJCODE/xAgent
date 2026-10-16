"""Reconnect with saved Weixin credentials and cursor, without a QR login."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xagent.integrations.weixin.adapter import WeixinAdapter
from xagent.integrations.weixin.config import WeixinAdapterConfig
from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore


class PollingClient:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.polling = asyncio.Event()

    def with_credentials(self, credentials):
        self.credentials = credentials

    async def get_updates(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return self.response
        self.polling.set()
        await asyncio.Event().wait()


class WeixinLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = WeixinStateStore(self.root)
        self.credentials = WeixinCredentials("saved-token", "https://example.test", "bot", "owner")
        self.store.save_credentials(self.credentials)
        self.store.save_sync_buf("bot", "saved-cursor")
        self.store.save_context_tokens("bot", {"owner": "saved-context"})

    def adapter(self, client):
        return WeixinAdapter(
            SimpleNamespace(workspace_dir=self.root),
            WeixinAdapterConfig(account_id="bot"), runtime_dir=self.root, client=client,
        )

    async def test_new_adapter_reuses_credentials_cursor_and_context_tokens(self):
        for cursor in ("saved-cursor", "next-cursor"):
            client = PollingClient({"ret": 0, "msgs": [], "get_updates_buf": "next-cursor"})
            adapter = self.adapter(client)
            states = []
            adapter.runtime_status = states.append
            running = asyncio.create_task(adapter.run())
            try:
                await asyncio.wait_for(client.polling.wait(), timeout=1)
                self.assertEqual(client.credentials.token, "saved-token")
                self.assertEqual(client.calls[0]["sync_buf"], cursor)
                self.assertEqual(adapter._context_tokens, {"owner": "saved-context"})
                self.assertEqual(states, ["connected"])
            finally:
                await adapter.stop()
                await asyncio.wait_for(running, timeout=1)
        self.assertIsNotNone(self.store.load_credentials("bot"))
        self.assertEqual(self.store.load_sync_buf("bot"), "next-cursor")

    async def test_expired_session_fails_and_requires_setup_without_connected_state(self):
        client = PollingClient({"ret": -14, "errmsg": "expired"})
        adapter = self.adapter(client)
        states = []
        adapter.runtime_status = states.append
        with self.assertRaisesRegex(RuntimeError, "session expired"):
            await adapter.run()
        self.assertEqual(states, [])
        self.assertIsNone(self.store.load_credentials("bot"))
        self.assertEqual(self.store.load_sync_buf("bot"), "")
        self.assertEqual(self.store.load_context_tokens("bot"), {})

    async def test_missing_credentials_fail_before_polling(self):
        self.store.delete_credentials("bot")
        client = PollingClient({"ret": 0})
        adapter = self.adapter(client)
        with self.assertRaisesRegex(RuntimeError, "credentials are missing"):
            await adapter.run()
        self.assertEqual(client.calls, [])
