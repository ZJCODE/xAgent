import unittest

import httpx
from fastapi import HTTPException

from xagent.interfaces.server import AgentHTTPServer, ChatInput


class FakeMessageStorage:
    async def clear_messages(self):
        return None


class FakeAgent:
    model = "test-model"
    tools = {}

    def __init__(self):
        self.message_storage = FakeMessageStorage()

    async def __call__(self, **kwargs):
        return "ok"


class ApiServerRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, server: AgentHTTPServer):
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def test_api_server_does_not_serve_spa_routes(self):
        server = AgentHTTPServer(agent=FakeAgent())

        async with await self._client(server) as client:
            response = await client.get("/")

        self.assertEqual(response.status_code, 404)

    async def test_chat_input_rejects_too_many_images(self):
        input_data = ChatInput(
            user_id="web",
            user_message="",
            image_source=[f"https://example.com/{index}.png" for index in range(6)],
        )

        with self.assertRaises(HTTPException) as error:
            AgentHTTPServer._input_image_sources(input_data)

        self.assertEqual(error.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
