import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xagent.tools.image_generation_tool import (
    create_image_generation_tool,
    normalize_image_generation_provider,
)


class FakeImages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeOpenAIClient:
    def __init__(self, response):
        self.images = FakeImages(response)


class ImageGenerationToolTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_image_generation_provider_aliases(self):
        self.assertEqual(normalize_image_generation_provider("openai_images"), "openai")
        self.assertEqual(normalize_image_generation_provider("disabled"), "none")
        self.assertEqual(normalize_image_generation_provider("off"), "none")

    def test_create_image_generation_tool_returns_none_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(
                create_image_generation_tool(
                    {"provider": "none"},
                    workspace_dir=tmpdir,
                )
            )

    async def test_openai_image_generation_writes_workspace_file(self):
        image_bytes = b"fake image bytes"
        response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=base64.b64encode(image_bytes).decode("ascii"),
                    revised_prompt="A refined image prompt.",
                )
            ]
        )
        client = FakeOpenAIClient(response)

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = create_image_generation_tool(
                {"provider": "openai", "model": "gpt-image-test"},
                client=client,
                workspace_dir=tmpdir,
            )

            result = await tool(prompt="Draw a crisp product icon", output_format="png")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["type"], "generated_image")
            self.assertEqual(result["model"], "gpt-image-test")
            self.assertEqual(result["revised_prompt"], "A refined image prompt.")
            self.assertTrue(result["image"]["path"].startswith("temp/images/"))
            self.assertIn("/api/workspace/blob?path=temp/images/", result["image"]["blob_url"])
            self.assertIn(result["image"]["blob_url"], result["image"]["markdown"])

            written = Path(tmpdir) / result["image"]["path"]
            self.assertEqual(written.read_bytes(), image_bytes)

        call = client.images.calls[0]
        self.assertEqual(call["model"], "gpt-image-test")
        self.assertEqual(call["prompt"], "Draw a crisp product icon")
        self.assertEqual(call["output_format"], "png")


if __name__ == "__main__":
    unittest.main()