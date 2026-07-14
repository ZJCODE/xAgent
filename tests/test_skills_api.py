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

    async def get_messages(self, count: int = 50, offset: int = 0):
        return []

    async def clear_messages(self):
        return None


class SkillsAgent:
    model = "test-model"
    tools = {"run_command": object()}

    def __init__(self, memory_root: Path, workspace_dir: Path):
        self.message_storage = FakeMessageStorage()
        self.markdown_memory = SimpleNamespace(root=str(memory_root))
        self.workspace_dir = workspace_dir
        self.system_prompt = "Test agent"


class SkillsApiTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, server: AgentHTTPServer):
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    def _server(self, root: Path) -> AgentHTTPServer:
        memory_root = root / "memory"
        workspace_dir = root / "workspace"
        memory_root.mkdir()
        workspace_dir.mkdir()
        server = AgentHTTPServer(
            agent=SkillsAgent(memory_root=memory_root, workspace_dir=workspace_dir),
        )
        server.workspace = root
        server.workspace_dir = workspace_dir
        return server

    async def test_skills_api_create_read_search_state_validate_and_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._server(Path(tmpdir))

            async with await self._client(server) as client:
                create_response = await client.post(
                    "/api/skills/create",
                    json={
                        "name": "code-review",
                        "description": "Reviews code changes. Use when reviewing diffs or PRs.",
                        "body": "# Code Review\n\nreview marker",
                    },
                )
                tree_response = await client.get("/api/skills/tree")
                read_response = await client.get("/api/skills/read", params={"path": "code-review/SKILL.md"})
                search_response = await client.get("/api/skills/search", params={"query": "review marker"})
                disable_response = await client.put(
                    "/api/skills/state",
                    json={"name": "code-review", "enabled": False},
                )
                info_response = await client.get("/api/skills/info")
                validate_response = await client.get("/api/skills/validate", params={"name": "code-review"})
                delete_response = await client.delete(
                    "/api/skills/delete",
                    params={"path": "code-review", "recursive": "true"},
                )
                missing_response = await client.get("/api/skills/read", params={"path": "code-review/SKILL.md"})

            self.assertEqual(create_response.status_code, 200)
            self.assertEqual(create_response.json()["skill"]["name"], "code-review")
            self.assertEqual(tree_response.status_code, 200)
            self.assertEqual(tree_response.json()["skills"][0]["skill_file"], "code-review/SKILL.md")
            self.assertEqual(read_response.status_code, 200)
            self.assertIn("review marker", read_response.json()["content"])
            self.assertEqual(search_response.status_code, 200)
            self.assertEqual(search_response.json()["results"][0]["path"], "code-review/SKILL.md")
            self.assertEqual(disable_response.status_code, 200)
            self.assertFalse(disable_response.json()["skill"]["enabled"])
            self.assertEqual(info_response.json()["disabled_count"], 1)
            self.assertTrue(validate_response.json()["valid"])
            self.assertEqual(delete_response.status_code, 200)
            self.assertEqual(missing_response.status_code, 404)

    async def test_skills_api_path_traversal_is_denied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "secret.txt").write_text("secret", encoding="utf-8")
            server = self._server(root)

            async with await self._client(server) as client:
                response = await client.get("/api/skills/read", params={"path": "../secret.txt"})

            self.assertEqual(response.status_code, 403)

    async def test_skills_api_safe_write_validation_conflict_and_entry_management(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._server(Path(tmpdir))

            async with await self._client(server) as client:
                created_skill = await client.post(
                    "/api/skills/create",
                    json={"name": "code-review", "description": "Reviews code. Use for review tasks."},
                )
                initial = await client.get("/api/skills/read", params={"path": "code-review/SKILL.md"})
                created_dir = await client.post(
                    "/api/skills/entries",
                    json={"parent_path": "code-review", "name": "references", "kind": "directory"},
                )
                created_file = await client.post(
                    "/api/skills/entries",
                    json={
                        "parent_path": "code-review/references",
                        "name": "guide.md",
                        "kind": "file",
                        "content": "# Guide\n",
                    },
                )
                file_read = await client.get(
                    "/api/skills/read", params={"path": "code-review/references/guide.md"}
                )
                write = await client.put(
                    "/api/skills/write",
                    json={
                        "path": "code-review/references/guide.md",
                        "content": "# Updated guide\n",
                        "expected_revision": file_read.json()["revision"],
                    },
                )
                stale = await client.put(
                    "/api/skills/write",
                    json={
                        "path": "code-review/references/guide.md",
                        "content": "# Stale\n",
                        "expected_revision": file_read.json()["revision"],
                    },
                )
                invalid = await client.put(
                    "/api/skills/write",
                    json={
                        "path": "code-review/SKILL.md",
                        "content": "---\nname: wrong\ndescription: ''\n---\n",
                        "expected_revision": initial.json()["revision"],
                    },
                )
                moved = await client.patch(
                    "/api/skills/entries",
                    json={
                        "path": "code-review/references/guide.md",
                        "new_parent_path": "code-review",
                        "new_name": "guide.md",
                        "expected_revision": write.json()["revision"],
                    },
                )
                protected = await client.delete(
                    "/api/skills/entries", params={"path": "code-review/SKILL.md"}
                )
                deleted = await client.delete(
                    "/api/skills/entries",
                    params={"path": "code-review/guide.md", "expected_revision": write.json()["revision"]},
                )

            self.assertEqual(created_skill.status_code, 200)
            self.assertEqual(created_dir.status_code, 200)
            self.assertEqual(created_file.status_code, 200)
            self.assertEqual(write.status_code, 200)
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["detail"]["code"], "revision_conflict")
            self.assertEqual(stale.json()["detail"]["current"]["content"], "# Updated guide\n")
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(invalid.json()["detail"]["code"], "skill_validation_failed")
            self.assertEqual(moved.status_code, 200)
            self.assertEqual(moved.json()["entry"]["path"], "code-review/guide.md")
            self.assertEqual(protected.status_code, 403)
            self.assertEqual(deleted.status_code, 200)


if __name__ == "__main__":
    unittest.main()
