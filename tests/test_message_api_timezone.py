import unittest

import httpx

from xagent.interfaces.server import AgentHTTPServer
from xagent.schemas import Message, RoleType


class _MessageStorage:
    def __init__(self):
        message = Message.create("你是谁", role=RoleType.USER, sender_id="小张")
        message.timestamp = 1779093994.0
        self.messages = [message]

    async def get_message_count(self):
        return len(self.messages)

    async def get_messages(self, count=50, offset=0):
        end = len(self.messages) - offset if offset else len(self.messages)
        start = max(0, end - count)
        return self.messages[start:end]

    def get_stream_info(self):
        return {"path": "messages.sqlite3"}

    async def clear_messages(self):
        self.messages.clear()


class _Agent:
    model = "test-model"
    tools = {}

    def __init__(self):
        self.message_storage = _MessageStorage()


class MessageApiTimezoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_messages_api_returns_utc_and_local_timestamps(self):
        server = AgentHTTPServer(agent=_Agent(), enable_web=False)
        server.config = {"runtime": {"timezone": "Asia/Shanghai"}}
        transport = httpx.ASGITransport(app=server.app)

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/messages?count=1&timezone=Asia/Shanghai")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        message = payload["messages"][0]
        self.assertEqual(message["timestamp"], 1779093994.0)
        self.assertEqual(message["timestamp_utc"], "2026-05-18 08:46:34 UTC (+00:00)")
        self.assertEqual(
            message["timestamp_local"],
            "2026-05-18 16:46:34 Asia/Shanghai (+08:00)",
        )
        self.assertEqual(message["timezone"], "Asia/Shanghai")
        self.assertEqual(message["utc_offset"], "+08:00")


if __name__ == "__main__":
    unittest.main()
