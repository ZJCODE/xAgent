import tempfile
import unittest
from pathlib import Path

from xagent.core.agent import Agent
from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.core.tooling.executor import ToolExecutor
from xagent.core.tooling.manager import ToolManager
from xagent.tools.see_image_tool import create_see_image_tool, see_image_observation
from xagent.utils.image_utils import (
    extract_workspace_image_paths_from_text,
    workspace_blob_url,
)


class _FakeMessageStorage:
    async def add_messages(self, *messages):
        return None


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )


class SeeImageToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_workspace_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            image = workspace / "assets" / "shot.png"
            _write_png(image)
            tool = create_see_image_tool(workspace_dir=str(workspace))

            result = await tool(path="assets/shot.png")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["type"], "see_image")
            self.assertEqual(result["path"], "assets/shot.png")
            self.assertEqual(result["blob_url"], workspace_blob_url("assets/shot.png"))
            self.assertEqual(result["mime_type"], "image/png")
            self.assertNotIn("I see", see_image_observation(result))
            self.assertIn("assets/shot.png", see_image_observation(result))
            self.assertIn("not a description of contents", see_image_observation(result))

    async def test_rejects_missing_outside_and_non_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "notes.txt").write_text("hello", encoding="utf-8")
            outside = Path(tmpdir).parent / "outside.png"
            _write_png(outside)
            tool = create_see_image_tool(workspace_dir=str(workspace))

            (workspace / "pics").mkdir()
            missing = await tool(path="missing.png")
            text_file = await tool(path="notes.txt")
            directory = await tool(path="pics")
            outside_result = await tool(path=str(outside))
            traversal = await tool(path="../outside.png")

            self.assertEqual(missing["status"], "error")
            self.assertEqual(text_file["status"], "error")
            self.assertEqual(directory["status"], "error")
            self.assertEqual(outside_result["status"], "error")
            self.assertEqual(traversal["status"], "error")
            self.assertIn("does not exist", missing["message"])
            self.assertIn("not an image", text_file["message"])
            self.assertIn("not a directory", directory["message"])
            self.assertIn("inside the workspace", outside_result["message"])


class WorkspaceImagePathExtractTests(unittest.TestCase):
    def test_extracts_relative_image_paths_and_ignores_bare_names(self):
        text = "Look at assets/inbound/local/images/shot.png and also chart.png please"

        self.assertEqual(
            extract_workspace_image_paths_from_text(text),
            ["assets/inbound/local/images/shot.png"],
        )

    def test_extracts_markdown_relative_image_path(self):
        self.assertEqual(
            extract_workspace_image_paths_from_text("see ![shot](assets/shots/a.png)"),
            ["assets/shots/a.png"],
        )

    def test_extracts_dot_slash_relative_image_path(self):
        self.assertEqual(
            extract_workspace_image_paths_from_text("open ./assets/shot.png"),
            ["./assets/shot.png"],
        )


class SeeImageInjectTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_user_message_opens_named_workspace_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "assets" / "shot.png"
            _write_png(image)
            handler = MessageHandler(
                message_storage=_FakeMessageStorage(),
                workspace_dir=tmpdir,
            )
            stored = await handler.store_user_message(
                "please look at assets/shot.png",
                "Joy",
            )

            self.assertTrue(stored.images)
            self.assertEqual(
                stored.images[0].source,
                workspace_blob_url("assets/shot.png"),
            )
            current = MessageHandler._current_message_images(stored, "Joy", workspace_dir=tmpdir)
            self.assertTrue(current[0].startswith("data:image/"))

    async def test_store_user_message_ignores_missing_named_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = MessageHandler(
                message_storage=_FakeMessageStorage(),
                workspace_dir=tmpdir,
            )
            stored = await handler.store_user_message(
                "please look at assets/missing.png",
                "Joy",
            )

            self.assertFalse(stored.images)

    async def test_store_user_message_dedupes_path_and_blob_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "assets" / "shot.png"
            _write_png(image)
            handler = MessageHandler(
                message_storage=_FakeMessageStorage(),
                workspace_dir=tmpdir,
            )
            stored = await handler.store_user_message(
                f"look at assets/shot.png and {workspace_blob_url('assets/shot.png')}",
                "Joy",
            )

            self.assertEqual(len(stored.images), 1)
            self.assertEqual(stored.images[0].source, workspace_blob_url("assets/shot.png"))

    async def test_executor_queues_see_image_without_user_display(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            image = workspace / "assets" / "shot.png"
            _write_png(image)
            tool = create_see_image_tool(workspace_dir=str(workspace))
            executor = ToolExecutor(
                tool_manager=ToolManager(tools=[tool]),
                message_storage=None,
                client=None,
                workspace_dir=workspace,
            )
            executor.begin_see_image_turn(already_visible=0)

            class Call:
                name = "see_image"
                arguments = '{"path": "assets/shot.png"}'
                id = "call-1"

            tool_message, display_result = await executor.execute_single(Call())

            self.assertIsNone(display_result)
            self.assertIn("Image is now visible to you: assets/shot.png", tool_message["content"])
            self.assertNotIn("login", tool_message["content"])
            self.assertEqual(executor.pending_see_image_paths(), ["assets/shot.png"])

    async def test_executor_fails_closed_over_image_cap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            image = workspace / "assets" / "shot.png"
            _write_png(image)
            tool = create_see_image_tool(workspace_dir=str(workspace))
            executor = ToolExecutor(
                tool_manager=ToolManager(tools=[tool]),
                message_storage=None,
                client=None,
                workspace_dir=workspace,
            )
            executor.begin_see_image_turn(already_visible=5)

            class Call:
                name = "see_image"
                arguments = '{"path": "assets/shot.png"}'
                id = "call-1"

            tool_message, display_result = await executor.execute_single(Call())

            self.assertIsNone(display_result)
            self.assertIn("At most 5 images", tool_message["content"])
            self.assertEqual(executor.pending_see_image_paths(), [])

    async def test_executor_surfaces_see_image_error_as_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            tool = create_see_image_tool(workspace_dir=str(workspace))
            executor = ToolExecutor(
                tool_manager=ToolManager(tools=[tool]),
                message_storage=None,
                client=None,
                workspace_dir=workspace,
            )
            executor.begin_see_image_turn(already_visible=0)

            class Call:
                name = "see_image"
                arguments = '{"path": "assets/missing.png"}'
                id = "call-1"

            tool_message, display_result = await executor.execute_single(Call())

            self.assertIsNone(display_result)
            self.assertIn("does not exist", tool_message["content"])
            self.assertNotIn("{", tool_message["content"])
            self.assertEqual(executor.pending_see_image_paths(), [])

    def test_apply_see_image_paths_adds_image_url_to_current_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "assets" / "shot.png"
            _write_png(image)
            messages = [
                {
                    "role": "user",
                    "name": AgentConfig.CURRENT_TASK_NAME,
                    "content": "What does the label say?",
                }
            ]
            MessageHandler.apply_see_image_paths(
                messages,
                ["assets/shot.png"],
                workspace_dir=tmpdir,
            )
            content = messages[0]["content"]
            self.assertEqual(content[0]["type"], "text")
            self.assertEqual(content[1]["type"], "image_url")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/"))


class SeeImageBindingTests(unittest.TestCase):
    def test_agent_binds_see_image_only_when_vision_is_on(self):
        with tempfile.TemporaryDirectory() as vision_dir, tempfile.TemporaryDirectory() as blind_dir:
            with_vision = Agent(client=object(), workspace=vision_dir, supports_vision=True)
            without_vision = Agent(client=object(), workspace=blind_dir, supports_vision=False)

            self.assertIn("see_image", with_vision.tools)
            self.assertNotIn("see_image", without_vision.tools)


if __name__ == "__main__":
    unittest.main()
