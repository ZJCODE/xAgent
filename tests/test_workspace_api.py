import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from xagent.interfaces.server import AgentHTTPServer
from xagent.schemas import Message, MessageType, RoleType, ToolCall


class FakeMessageStorage:
    def __init__(self, messages=None):
        self._messages = list(messages or [])

    def get_stream_info(self):
        return {"path": "messages.sqlite3"}

    async def get_message_count(self):
        return len(self._messages)

    async def get_messages(self, count: int = 50, offset: int = 0):
        end = len(self._messages) - offset
        if end <= 0:
            return []
        start = max(0, end - count)
        return list(self._messages[start:end])

    async def clear_messages(self):
        return None


class WorkspaceAgent:
    model = "test-model"
    tools = {"run_command": object()}

    def __init__(self, memory_root: Path, workspace_dir: Path, messages=None):
        self.message_storage = FakeMessageStorage(messages=messages)
        self.markdown_memory = SimpleNamespace(root=str(memory_root))
        self.workspace_dir = workspace_dir
        self.system_prompt = "Test agent"


class WorkspaceApiTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, server: AgentHTTPServer):
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    def _server(self, root: Path) -> AgentHTTPServer:
        return self._server_with_messages(root)

    def _server_with_messages(self, root: Path, messages=None) -> AgentHTTPServer:
        memory_root = root / "memory"
        workspace_dir = root / "workspace"
        memory_root.mkdir()
        workspace_dir.mkdir()
        server = AgentHTTPServer(
            agent=WorkspaceAgent(memory_root=memory_root, workspace_dir=workspace_dir, messages=messages),
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

    async def test_workspace_clear_deletes_contents_but_keeps_root_and_external_symlink_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._server(root)
            (server.workspace_dir / "notes").mkdir()
            (server.workspace_dir / "notes" / "today.md").write_text("keep?", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (server.workspace_dir / "outside-link.txt").symlink_to(outside)

            async with await self._client(server) as client:
                response = await client.post("/api/workspace/clear")
                tree_response = await client.get("/api/workspace/tree")

            self.assertEqual(response.status_code, 200)
            self.assertTrue(server.workspace_dir.is_dir())
            self.assertEqual(tree_response.json()["tree"], [])
            self.assertTrue(outside.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

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

    async def test_workspace_upload_accepts_non_image_attachment_with_blob_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._server(Path(tmpdir))
            report_bytes = b"name,value\nalpha,1\n"

            async with await self._client(server) as client:
                upload_response = await client.post(
                    "/api/workspace/upload",
                    data={"path": "incoming/"},
                    files={"file": ("report.csv", report_bytes, "text/csv")},
                )
                blob_response = await client.get("/api/workspace/blob", params={"path": "incoming/report.csv"})

            self.assertEqual(upload_response.status_code, 200)
            payload = upload_response.json()
            self.assertEqual(payload["path"], "incoming/report.csv")
            self.assertEqual(payload["blob_url"], "/api/workspace/blob?path=incoming%2Freport.csv")
            self.assertEqual(blob_response.status_code, 200)
            self.assertEqual(blob_response.content, report_bytes)

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

    async def test_message_search_matches_message_content_and_tool_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            messages = [
                Message.create("content marker from alice", role=RoleType.USER, sender_id="alice"),
                Message(
                    type=MessageType.FUNCTION_CALL_OUTPUT,
                    role=RoleType.TOOL,
                    sender_id="search_workspace",
                    content="Tool output preview",
                    tool_call=ToolCall(
                        call_id="call-1",
                        name="search_workspace",
                        output="tool marker",
                    ),
                ),
            ]
            server = self._server_with_messages(root, messages=messages)

            async with await self._client(server) as client:
                response = await client.get("/api/messages/search", params={"query": "marker"})

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["query"], "marker")
            self.assertEqual(len(payload["results"]), 2)
            self.assertEqual(payload["results"][0]["type"], "function_call_output")
            self.assertIn("tool", payload["results"][0]["matched_in"])
            self.assertIn("tool marker", payload["results"][0]["snippet"])
            self.assertEqual(payload["results"][1]["sender_id"], "alice")
            self.assertIn("content", payload["results"][1]["matched_in"])
            self.assertIn("content marker from alice", payload["results"][1]["snippet"])

    async def test_messages_api_returns_image_preview_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            message = Message.create(
                "inspect this",
                role=RoleType.USER,
                sender_id="alice",
                image_source="/api/workspace/blob?path=temp%2Fimages%2Fweb%2Finput.png",
            )
            message.metadata["images"] = [{
                "workspace_path": "temp/images/web/input.png",
                "blob_url": "/api/workspace/blob?path=temp%2Fimages%2Fweb%2Finput.png",
                "mime_type": "image/png",
            }]
            server = self._server_with_messages(root, messages=[message])

            async with await self._client(server) as client:
                response = await client.get("/api/messages")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["messages"][0]["image_count"], 1)
            self.assertEqual(payload["messages"][0]["images"][0]["workspace_path"], "temp/images/web/input.png")


if __name__ == "__main__":
    unittest.main()
