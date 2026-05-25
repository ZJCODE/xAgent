import tempfile
import unittest
from pathlib import Path

from xagent.tools.artifact_tool import create_attach_artifact_tool
from xagent.utils.image_utils import workspace_blob_url


class AttachArtifactToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_workspace_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            image = workspace / "temp" / "images" / "result.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            tool = create_attach_artifact_tool(workspace_dir=str(workspace))

            result = await tool(path="temp/images/result.png", caption="Processed")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["type"], "artifact_attachment")
            self.assertEqual(result["artifact"]["kind"], "image")
            self.assertEqual(result["artifact"]["path"], "temp/images/result.png")
            self.assertEqual(result["artifact"]["blob_url"], workspace_blob_url("temp/images/result.png"))
            self.assertEqual(result["artifact"]["caption"], "Processed")

    async def test_accepts_workspace_blob_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            report = workspace / "reports" / "out.pdf"
            report.parent.mkdir(parents=True)
            report.write_bytes(b"%PDF")
            tool = create_attach_artifact_tool(workspace_dir=str(workspace))

            result = await tool(path=workspace_blob_url("reports/out.pdf"))

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["artifact"]["kind"], "file")
            self.assertEqual(result["artifact"]["path"], "reports/out.pdf")
            self.assertEqual(result["artifact"]["mime_type"], "application/pdf")

    async def test_accepts_absolute_path_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            file_path = workspace / "notes.txt"
            file_path.write_text("hello", encoding="utf-8")
            tool = create_attach_artifact_tool(workspace_dir=str(workspace))

            result = await tool(path=str(file_path))

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["artifact"]["path"], "notes.txt")

    async def test_rejects_missing_directory_outside_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            directory = workspace / "dir"
            directory.mkdir()
            outside = Path(tmpdir).parent / "outside.txt"
            outside.write_text("no", encoding="utf-8")
            tool = create_attach_artifact_tool(workspace_dir=str(workspace))

            missing = await tool(path="missing.png")
            directory_result = await tool(path="dir")
            outside_result = await tool(path=str(outside))
            traversal_result = await tool(path="../outside.txt")

            self.assertEqual(missing["status"], "error")
            self.assertEqual(directory_result["status"], "error")
            self.assertEqual(outside_result["status"], "error")
            self.assertEqual(traversal_result["status"], "error")
            self.assertIn("does not exist", missing["message"])
            self.assertIn("not a directory", directory_result["message"])
            self.assertIn("inside the workspace", outside_result["message"])
            self.assertIn("inside the workspace", traversal_result["message"])


if __name__ == "__main__":
    unittest.main()
