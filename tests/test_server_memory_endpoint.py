import unittest

from fastapi.testclient import TestClient

from xagent.interfaces.server import AgentHTTPServer


class _FakeMemoryStorage:
    def __init__(self):
        self.calls = []

    async def retrieve(self, memory_key: str, query: str = "", limit: int = 5, journal_date=None):
        self.calls.append(
            {
                "memory_key": memory_key,
                "query": query,
                "limit": limit,
                "journal_date": journal_date,
            }
        )
        return [
            {
                "content": "2026-03-18 记录了东京行程。",
                "metadata": {
                    "journal_date": "2026-03-18",
                    "updated_at": 1710000000.0,
                    "matched_keywords": ["东京"],
                },
            }
        ]


class _FakeAgent:
    def __init__(self):
        self.name = "TestAgent"
        self.model = "gpt-5-mini"
        self.memory_key = "agent:TestAgent"
        self.memory_storage = _FakeMemoryStorage()
        self.message_storage = object()

    async def __call__(self, *args, **kwargs):
        return "ok"


class MemoryEndpointTests(unittest.TestCase):
    def test_memory_endpoint_forwards_query_and_date(self):
        agent = _FakeAgent()
        server = AgentHTTPServer(agent=agent, enable_web=False)
        client = TestClient(server.app)

        response = client.get(
            "/memory",
            params={"query": "东京", "date": "2026-03-18", "limit": 3},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(agent.memory_storage.calls[0]["memory_key"], "agent:TestAgent")
        self.assertEqual(agent.memory_storage.calls[0]["query"], "东京")
        self.assertEqual(agent.memory_storage.calls[0]["journal_date"], "2026-03-18")
        self.assertEqual(agent.memory_storage.calls[0]["limit"], 3)
        self.assertEqual(response.json()["memories"][0]["metadata"]["journal_date"], "2026-03-18")

    def test_memory_endpoint_uses_empty_query_when_only_date_is_provided(self):
        agent = _FakeAgent()
        server = AgentHTTPServer(agent=agent, enable_web=False)
        client = TestClient(server.app)

        response = client.get("/memory", params={"date": "2026-03-18"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(agent.memory_storage.calls[0]["query"], "")
        self.assertEqual(agent.memory_storage.calls[0]["journal_date"], "2026-03-18")


if __name__ == "__main__":
    unittest.main()
