import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx

from xagent.components.memory import ExperienceMemoryStore
from xagent.interfaces.server import AgentHTTPServer
from xagent.schemas import Message, RoleType


class FakeMessageStorage:
    def get_stream_info(self):
        return {"backend": "sqlite"}

    async def get_message_count(self):
        return 0

    async def clear_messages(self):
        return None


class MemoryApiAgent:
    model = "test-model"
    tools = {}

    def __init__(self, store: ExperienceMemoryStore):
        self.memory_store = store
        self.message_storage = FakeMessageStorage()
        self.system_prompt = "# Identity\n\nTest agent."
        self.message_handler = SimpleNamespace(system_prompt=self.system_prompt)


class MemoryDashboardApiTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, server: AgentHTTPServer):
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def test_memory_dashboard_returns_latest_schema_collections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "xagent_memory.sqlite3"
            store = ExperienceMemoryStore(str(db_path))
            message = Message.create(
                "I prefer concise implementation plans.",
                role=RoleType.USER,
                sender_id="Alice",
            )
            await store.add_messages(message)
            memory_id = await store.remember(
                "Alice prefers concise implementation plans.",
                kind="preference",
                subject_type="person",
                subject_key="Alice",
                evidence_event_ids=[message.metadata["event_id"]],
                evidence_note="I prefer concise implementation plans.",
            )
            await store.correct_memory(
                memory_id=memory_id,
                correction="Alice prefers concise implementation plans and fast feedback.",
                reason="updated preference",
            )
            await store.add_summary(
                summary_type="weekly",
                scope_type="self",
                scope_key="self",
                period_start=date(2026, 5, 12),
                period_end=date(2026, 5, 18),
                content="Weekly summary for the test workspace.",
                source_memory_ids=[memory_id],
            )

            server = AgentHTTPServer(agent=MemoryApiAgent(store), enable_web=False)

            async with await self._client(server) as client:
                response = await client.get("/api/memory/dashboard", params={"preview_limit": 20})

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["stats"]["memory_items"], 2)
            self.assertEqual(payload["stats"]["events"], 1)
            self.assertEqual(payload["collections"]["people"]["items"][0]["person_key"], "Alice")
            self.assertEqual(payload["collections"]["summaries"]["items"][0]["summary_type"], "weekly")
            self.assertEqual(payload["collections"]["revisions"]["items"][0]["memory_id"], memory_id)
            self.assertTrue(
                any(
                    row["event_id"] == message.metadata["event_id"]
                    for row in payload["collections"]["evidence"]["items"]
                )
            )
            self.assertEqual(payload["collections"]["memories"]["items"][0]["kind"], "summary")
            self.assertTrue(any(row["label"] == "active" for row in payload["breakdowns"]["memory_status"]))

    async def test_memory_query_endpoint_accepts_readonly_sql(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "xagent_memory.sqlite3"
            store = ExperienceMemoryStore(str(db_path))
            await store.remember(
                "Project uses FastAPI.",
                kind="project_state",
                subject_type="project",
                subject_key="xAgent",
            )
            server = AgentHTTPServer(agent=MemoryApiAgent(store), enable_web=False)

            async with await self._client(server) as client:
                response = await client.post(
                    "/api/memory/query",
                    json={
                        "sql": "SELECT COUNT(*) AS count FROM memory_items",
                        "max_rows": 5,
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["rows"][0]["count"], 1)

    async def test_memory_query_endpoint_rejects_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "xagent_memory.sqlite3"
            store = ExperienceMemoryStore(str(db_path))
            server = AgentHTTPServer(agent=MemoryApiAgent(store), enable_web=False)

            async with await self._client(server) as client:
                response = await client.post(
                    "/api/memory/query",
                    json={
                        "sql": "DELETE FROM memory_items",
                    },
                )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Only SELECT or WITH", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()