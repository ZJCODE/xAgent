import asyncio
import json
import unittest

import httpx

from xagent.integrations.weixin.client import QrLoginCancelled, WeixinClient, qr_login
from xagent.integrations.weixin.state import WeixinCredentials


class WeixinClientTests(unittest.TestCase):
    def test_send_text_message_builds_ilink_body_and_headers(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={})

        async def run_test():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = WeixinClient(
                    base_url="https://example.test",
                    token="token-1",
                    channel_version="9.9.9",
                    http_client=http_client,
                )
                await client.send_text_message(
                    to_user_id="user-1",
                    text="hello",
                    context_token="ctx-1",
                    client_id="cid-1",
                )

        asyncio.run(run_test())

        self.assertEqual(captured["url"], "https://example.test/ilink/bot/sendmessage")
        self.assertEqual(captured["headers"]["authorizationtype"], "ilink_bot_token")
        self.assertEqual(captured["headers"]["authorization"], "Bearer token-1")
        self.assertIn("x-wechat-uin", captured["headers"])
        body = captured["body"]
        self.assertEqual(body["base_info"], {"channel_version": "9.9.9"})
        self.assertEqual(body["msg"]["to_user_id"], "user-1")
        self.assertEqual(body["msg"]["client_id"], "cid-1")
        self.assertEqual(body["msg"]["message_type"], 2)
        self.assertEqual(body["msg"]["message_state"], 2)
        self.assertEqual(body["msg"]["context_token"], "ctx-1")
        self.assertEqual(body["msg"]["item_list"][0]["text_item"]["text"], "hello")

    def test_getupdates_timeout_returns_empty_poll(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout")

        async def run_test():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = WeixinClient(base_url="https://example.test", token="token", http_client=http_client)
                return await client.get_updates(sync_buf="cursor", timeout_ms=1)

        response = asyncio.run(run_test())

        self.assertEqual(response, {"ret": 0, "msgs": [], "get_updates_buf": "cursor"})

    def test_qr_login_saves_confirmed_payload_shape(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "get_bot_qrcode" in str(request.url):
                return httpx.Response(200, json={"qrcode": "qr-1", "qrcode_img_content": "https://qr"})
            return httpx.Response(
                200,
                json={
                    "status": "confirmed",
                    "bot_token": "token",
                    "baseurl": "https://base",
                    "ilink_bot_id": "bot@im.bot",
                    "ilink_user_id": "owner@im.wechat",
                },
            )

        async def run_test():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                return await qr_login(
                    base_url="https://example.test",
                    timeout_seconds=5,
                    log=lambda _msg: None,
                    render_qr_url=lambda _url: None,
                    http_client=http_client,
                )

        credentials = asyncio.run(run_test())

        self.assertIsInstance(credentials, WeixinCredentials)
        self.assertEqual(credentials.token, "token")
        self.assertEqual(credentials.base_url, "https://base")
        self.assertEqual(credentials.account_id, "bot@im.bot")
        self.assertEqual(credentials.user_id, "owner@im.wechat")
        self.assertEqual(len(calls), 2)

    def test_qr_login_cancelled_mid_poll(self):
        import threading

        def handler(_request: httpx.Request) -> httpx.Response:
            if "get_bot_qrcode" in str(_request.url):
                return httpx.Response(200, json={"qrcode": "qr-1"})
            return httpx.Response(200, json={"status": "wait"})

        cancel_event = threading.Event()

        async def run_test():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                task = asyncio.create_task(
                    qr_login(
                        base_url="https://example.test",
                        timeout_seconds=5,
                        http_client=http_client,
                        cancel_event=cancel_event,
                    )
                )
                await asyncio.sleep(0.05)
                cancel_event.set()
                with self.assertRaises(QrLoginCancelled):
                    await task

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
