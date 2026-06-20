import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from xagent.interfaces.server import AgentHTTPServer


class FakeMessageStorage:
    def get_stream_info(self):
        return {"path": "messages.sqlite3"}

    async def get_message_count(self):
        return 0

    async def clear_messages(self):
        return None


class IdentityAgent:
    model = "test-model"
    tools = {}

    def __init__(self, identity: str, memory_root: Path):
        self.system_prompt = identity
        self.message_storage = FakeMessageStorage()
        self.markdown_memory = SimpleNamespace(root=str(memory_root))

    @property
    def identity(self) -> str:
        return self.system_prompt

    def set_identity(self, identity: str) -> None:
        self.system_prompt = identity


class AgentIdentityApiTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, server: AgentHTTPServer):
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    def _server(self, root: Path, identity: str) -> AgentHTTPServer:
        memory_root = root / "memory"
        memory_root.mkdir()
        identity_path = root / "identity.md"
        identity_path.write_text(f"{identity}\n", encoding="utf-8")

        server = AgentHTTPServer(
            agent=IdentityAgent(identity=identity, memory_root=memory_root),
            enable_web=False,
        )
        server.workspace = root
        server.identity_path = identity_path
        return server

    async def test_agent_info_returns_identity_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._server(root, "# Identity\n\nOld agent.")

            async with await self._client(server) as client:
                response = await client.get("/api/agent/info")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["identity"], "# Identity\n\nOld agent.")
            self.assertEqual(payload["system_prompt"], payload["identity"])
            self.assertEqual(payload["identity_file"], "identity.md")
            self.assertEqual(payload["identity_path"], str((root / "identity.md").resolve()))
            self.assertTrue(payload["identity_editable"])

    async def test_update_identity_saves_file_and_runtime_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._server(root, "# Identity\n\nOld agent.")
            new_identity = "# Identity\n\nNew agent."

            async with await self._client(server) as client:
                response = await client.put(
                    "/api/agent/identity",
                    json={"identity": new_identity},
                )
                read_response = await client.get("/api/agent/identity")

            self.assertEqual(response.status_code, 200)
            self.assertEqual((root / "identity.md").read_text(encoding="utf-8"), f"{new_identity}\n")
            self.assertEqual(server.agent.system_prompt, new_identity)
            self.assertEqual(server.agent.identity, new_identity)
            self.assertEqual(read_response.json()["identity"], f"{new_identity}\n")

    async def test_update_identity_rejects_empty_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._server(root, "# Identity\n\nOld agent.")

            async with await self._client(server) as client:
                response = await client.put(
                    "/api/agent/identity",
                    json={"identity": "   "},
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual((root / "identity.md").read_text(encoding="utf-8"), "# Identity\n\nOld agent.\n")
            self.assertEqual(server.agent.system_prompt, "# Identity\n\nOld agent.")


if __name__ == "__main__":
    unittest.main()
