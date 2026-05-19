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


class WorkspaceAgent:
    model = "test-model"
    tools = {"run_command": object()}

    def __init__(self, memory_root: Path, workspace_dir: Path):
        self.message_storage = FakeMessageStorage()
        self.markdown_memory = SimpleNamespace(root=str(memory_root))
        self.workspace_dir = workspace_dir
        self.system_prompt = "Test agent"


class WorkspaceApiTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, server: AgentHTTPServer):
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    def _server(self, root: Path) -> AgentHTTPServer:
        memory_root = root / "memory"
        workspace_dir = root / "workspace"
        memory_root.mkdir()
        workspace_dir.mkdir()
        server = AgentHTTPServer(
            agent=WorkspaceAgent(memory_root=memory_root, workspace_dir=workspace_dir),
            enable_web=False,
        )
        server.workspace = root
        server.workspace_dir = workspace_dir
        return server

    async def test_workspace_write_read_search_and_delete_text_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._server(Path(tmpdir))

            async with await self._client(server) as client:
                write_response = await client.put(
                    "/api/workspace/write",
                    json={"path": "notes/today.md", "content": "# Today\n\nworkspace marker"},
                )
                read_response = await client.get("/api/workspace/read", params={"path": "notes/today.md"})
                search_response = await client.get("/api/workspace/search", params={"query": "workspace marker"})
                delete_response = await client.delete(
                    "/api/workspace/delete",
                    params={"path": "notes/today.md"},
                )
                missing_response = await client.get("/api/workspace/read", params={"path": "notes/today.md"})

            self.assertEqual(write_response.status_code, 200)
            self.assertEqual(read_response.status_code, 200)
            self.assertTrue(read_response.json()["text"])
            self.assertIn("workspace marker", read_response.json()["content"])
            self.assertEqual(search_response.status_code, 200)
            self.assertEqual(search_response.json()["results"][0]["path"], "notes/today.md")
            self.assertEqual(delete_response.status_code, 200)
            self.assertEqual(missing_response.status_code, 404)

    async def test_workspace_upload_and_blob_serves_binary_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._server(Path(tmpdir))
            image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

            async with await self._client(server) as client:
                upload_response = await client.post(
                    "/api/workspace/upload",
                    data={"path": "images/"},
                    files={"file": ("tiny.png", image_bytes, "image/png")},
                )
                read_response = await client.get("/api/workspace/read", params={"path": "images/tiny.png"})
                blob_response = await client.get("/api/workspace/blob", params={"path": "images/tiny.png"})

            self.assertEqual(upload_response.status_code, 200)
            self.assertEqual(upload_response.json()["path"], "images/tiny.png")
            self.assertEqual(read_response.status_code, 200)
            self.assertTrue(read_response.json()["binary"])
            self.assertFalse(read_response.json()["text"])
            self.assertEqual(blob_response.status_code, 200)
            self.assertEqual(blob_response.content, image_bytes)

    async def test_workspace_path_traversal_is_denied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "secret.txt").write_text("secret", encoding="utf-8")
            server = self._server(root)

            async with await self._client(server) as client:
                response = await client.get("/api/workspace/read", params={"path": "../secret.txt"})

            self.assertEqual(response.status_code, 403)

    async def test_workspace_symlink_escape_is_denied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._server(root)
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (server.workspace_dir / "link.txt").symlink_to(outside)

            async with await self._client(server) as client:
                response = await client.get("/api/workspace/read", params={"path": "link.txt"})

            self.assertEqual(response.status_code, 403)

    async def test_memory_tree_and_search_exclude_legacy_people_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._server(root)
            daily_dir = root / "memory" / "daily" / "2026" / "2026-05"
            daily_dir.mkdir(parents=True)
            (daily_dir / "2026-05-19.md").write_text("daily marker", encoding="utf-8")
            people_dir = root / "memory" / "people"
            people_dir.mkdir()
            (people_dir / "alice.md").write_text("legacy people marker", encoding="utf-8")

            async with await self._client(server) as client:
                tree_response = await client.get("/api/memory/tree")
                daily_search_response = await client.get("/api/memory/search", params={"query": "daily marker"})
                people_search_response = await client.get("/api/memory/search", params={"query": "legacy people marker"})

            self.assertEqual(tree_response.status_code, 200)
            tree_names = [item["name"] for item in tree_response.json()["tree"]]
            self.assertIn("daily", tree_names)
            self.assertNotIn("people", tree_names)
            self.assertEqual(len(daily_search_response.json()["results"]), 1)
            self.assertEqual(people_search_response.json()["results"], [])


if __name__ == "__main__":
    unittest.main()
