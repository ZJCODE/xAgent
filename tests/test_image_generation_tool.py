import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


class FakeHTTPResponse:
    def __init__(self, payload=None, content: bytes = b""):
        self.payload = payload
        self.content = content
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncHTTPClient:
    def __init__(self, response_payload, image_bytes: bytes = b""):
        self.response_payload = response_payload
        self.image_bytes = image_bytes
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeHTTPResponse(self.response_payload)

    async def get(self, url):
        self.calls.append({"url": url})
        return FakeHTTPResponse(content=self.image_bytes)


class ImageGenerationToolTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_image_generation_provider_aliases(self):
        self.assertEqual(normalize_image_generation_provider("openai_images"), "openai")
        self.assertEqual(normalize_image_generation_provider("minimax_images"), "minimax")
        self.assertEqual(normalize_image_generation_provider("qwen_images"), "qwen")
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
            self.assertIn("/api/workspace/blob?path=temp%2Fimages%2F", result["image"]["blob_url"])
            self.assertIn(result["image"]["blob_url"], result["image"]["markdown"])

            written = Path(tmpdir) / result["image"]["path"]
            self.assertEqual(written.read_bytes(), image_bytes)

        call = client.images.calls[0]
        self.assertEqual(call["model"], "gpt-image-test")
        self.assertEqual(call["prompt"], "Draw a crisp product icon")
        self.assertEqual(call["output_format"], "png")

    async def test_openai_image_generation_uses_latest_defaults_and_options(self):
        image_bytes = b"fake image bytes"
        response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=base64.b64encode(image_bytes).decode("ascii"),
                    revised_prompt="",
                )
            ]
        )
        client = FakeOpenAIClient(response)

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = create_image_generation_tool(
                {"provider": "openai"},
                client=client,
                workspace_dir=tmpdir,
            )

            result = await tool(
                prompt="Draw a fast draft",
                output_format="jpeg",
                output_compression=45,
                moderation="low",
                n=2,
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["model"], "gpt-image-2")
            self.assertEqual(result["size"], "auto")
            self.assertEqual(result["quality"], "auto")
            self.assertEqual(result["output_format"], "jpeg")

        call = client.images.calls[0]
        self.assertEqual(call["model"], "gpt-image-2")
        self.assertEqual(call["size"], "auto")
        self.assertEqual(call["quality"], "auto")
        self.assertEqual(call["output_format"], "jpeg")
        self.assertEqual(call["output_compression"], 45)
        self.assertEqual(call["moderation"], "low")
        self.assertEqual(call["n"], 2)

    async def test_minimax_image_generation_writes_workspace_file(self):
        image_bytes = b"fake minimax image bytes"
        response_payload = {
            "data": {"image_base64": [base64.b64encode(image_bytes).decode("ascii")]},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        http_client = FakeAsyncHTTPClient(response_payload)

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = create_image_generation_tool(
                {"provider": "minimax", "api_key": "minimax-key"},
                workspace_dir=tmpdir,
            )

            with patch("xagent.tools.image_generation_tool.httpx.AsyncClient", return_value=http_client):
                result = await tool(
                    prompt="女孩在图书馆的窗户前，看向远方",
                    aspect_ratio="16:9",
                    reference_image_urls=["https://example.com/reference.jpg"],
                    n=2,
                    prompt_optimizer=True,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["provider"], "minimax")
            self.assertEqual(result["model"], "image-01")
            self.assertEqual(result["aspect_ratio"], "16:9")
            self.assertTrue(result["image"]["path"].startswith("temp/images/"))
            self.assertIn(result["image"]["blob_url"], result["image"]["markdown"])

            written = Path(tmpdir) / result["image"]["path"]
            self.assertEqual(written.read_bytes(), image_bytes)

        call = http_client.calls[0]
        self.assertEqual(call["url"], "https://api.minimaxi.com/v1/image_generation")
        self.assertEqual(call["headers"]["Authorization"], "Bearer minimax-key")
        self.assertEqual(call["json"]["model"], "image-01")
        self.assertEqual(call["json"]["prompt"], "女孩在图书馆的窗户前，看向远方")
        self.assertEqual(call["json"]["response_format"], "base64")
        self.assertEqual(call["json"]["aspect_ratio"], "16:9")
        self.assertEqual(call["json"]["n"], 2)
        self.assertTrue(call["json"]["prompt_optimizer"])
        self.assertEqual(
            call["json"]["subject_reference"],
            [{"type": "character", "image_file": "https://example.com/reference.jpg"}],
        )

    async def test_qwen_image_generation_writes_workspace_file(self):
        image_bytes = b"\x89PNG\r\n\x1a\nfake qwen image bytes"
        response_payload = {
            "request_id": "qwen-request-id",
            "output": {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": [{"image": "https://dashscope-result.example.com/image.png"}],
                        },
                    }
                ]
            },
            "usage": {"width": 2048, "height": 2048, "image_count": 1},
        }
        http_client = FakeAsyncHTTPClient(response_payload, image_bytes=image_bytes)

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = create_image_generation_tool(
                {
                    "provider": "qwen",
                    "api_key": "qwen-key",
                    "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                },
                workspace_dir=tmpdir,
            )

            with patch("xagent.tools.image_generation_tool.httpx.AsyncClient", return_value=http_client):
                result = await tool(
                    prompt="Draw a bilingual poster with clean Chinese text",
                    size="2048*2048",
                    n=2,
                    negative_prompt="文字模糊，扭曲",
                    prompt_extend=True,
                    watermark=False,
                    seed=123,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["provider"], "qwen")
            self.assertEqual(result["model"], "qwen-image-2.0-pro")
            self.assertEqual(result["size"], "2048*2048")
            self.assertEqual(result["output_format"], "png")
            self.assertEqual(result["request_id"], "qwen-request-id")
            self.assertTrue(result["prompt_extend"])
            self.assertFalse(result["watermark"])
            self.assertEqual(result["negative_prompt"], "文字模糊，扭曲")
            self.assertTrue(result["image"]["path"].startswith("temp/images/"))

            written = Path(tmpdir) / result["image"]["path"]
            self.assertEqual(written.read_bytes(), image_bytes)

        post_call = http_client.calls[0]
        self.assertEqual(
            post_call["url"],
            "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        )
        self.assertEqual(post_call["headers"]["Authorization"], "Bearer qwen-key")
        self.assertEqual(post_call["json"]["model"], "qwen-image-2.0-pro")
        self.assertEqual(
            post_call["json"]["input"]["messages"][0]["content"][0]["text"],
            "Draw a bilingual poster with clean Chinese text",
        )
        self.assertEqual(
            post_call["json"]["parameters"],
            {
                "size": "2048*2048",
                "n": 2,
                "seed": 123,
                "prompt_extend": True,
                "watermark": False,
                "negative_prompt": "文字模糊，扭曲",
            },
        )
        self.assertEqual(http_client.calls[1]["url"], "https://dashscope-result.example.com/image.png")

    async def test_openai_image_generation_rejects_unsupported_reference_params(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool = create_image_generation_tool(
                {"provider": "openai"},
                client=FakeOpenAIClient(SimpleNamespace(data=[])),
                workspace_dir=tmpdir,
            )

            result = await tool(
                prompt="Draw a product icon",
                reference_image_url="https://example.com/reference.png",
            )

            self.assertEqual(result["status"], "error")
            self.assertIn("reference_image_url", result["message"])

    async def test_image_generation_rejects_invalid_openai_options(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool = create_image_generation_tool(
                {"provider": "openai"},
                client=FakeOpenAIClient(SimpleNamespace(data=[])),
                workspace_dir=tmpdir,
            )

            result = await tool(prompt="Draw a product icon", output_format="gif")

            self.assertEqual(result["status"], "error")
            self.assertIn("output_format", result["message"])


if __name__ == "__main__":
    unittest.main()
