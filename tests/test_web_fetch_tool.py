"""Tests for the built-in web_fetch tool."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from xagent.tools.web_fetch_tool import (
    create_web_fetch_tool,
    _is_private_or_reserved_ip,
    _resolve_to_private,
    _validate_url,
    _MAX_CONTENT_LENGTH,
    _REQUEST_TIMEOUT,
)


class WebFetchToolCreationTests(unittest.IsolatedAsyncioTestCase):
    """Tests for tool creation and metadata."""

    def test_create_returns_callable(self):
        tool = create_web_fetch_tool()
        self.assertIsNotNone(tool)
        self.assertTrue(callable(tool))

    def test_has_tool_spec(self):
        tool = create_web_fetch_tool()
        spec = getattr(tool, "tool_spec", None)
        self.assertIsNotNone(spec)
        self.assertEqual(spec["type"], "function")
        self.assertEqual(spec["function"]["name"], "web_fetch")
        # url should be required
        self.assertIn("url", spec["function"]["parameters"]["required"])
        self.assertIn("url", spec["function"]["parameters"]["properties"])


class URLValidationTests(unittest.TestCase):
    """Tests for URL validation and SSRF prevention."""

    def test_valid_https_url(self):
        result = _validate_url("https://example.com/page")
        self.assertEqual(result, "https://example.com/page")

    def test_valid_http_url(self):
        result = _validate_url("http://example.com")
        self.assertEqual(result, "http://example.com")

    def test_strips_whitespace(self):
        result = _validate_url("  https://example.com  ")
        self.assertEqual(result, "https://example.com")

    def test_rejects_empty_url(self):
        with self.assertRaises(ValueError):
            _validate_url("")

    def test_rejects_whitespace_only_url(self):
        with self.assertRaises(ValueError):
            _validate_url("   ")

    def test_rejects_file_scheme(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_url("file:///etc/passwd")
        self.assertIn("Unsupported URL scheme", str(ctx.exception))

    def test_rejects_javascript_scheme(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_url("javascript:alert(1)")
        self.assertIn("Unsupported URL scheme", str(ctx.exception))

    def test_rejects_no_hostname(self):
        with self.assertRaises(ValueError):
            _validate_url("https:///path")

    def test_rejects_loopback_ip(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_url("http://127.0.0.1:8080/admin")
        self.assertIn("private/reserved IP", str(ctx.exception))

    def test_rejects_private_ip_10(self):
        with self.assertRaises(ValueError):
            _validate_url("http://10.0.0.1/secret")

    def test_rejects_private_ip_192_168(self):
        with self.assertRaises(ValueError):
            _validate_url("http://192.168.1.1/admin")

    def test_rejects_private_ip_172_16(self):
        with self.assertRaises(ValueError):
            _validate_url("http://172.16.0.1/api")

    def test_rejects_link_local(self):
        with self.assertRaises(ValueError):
            _validate_url("http://169.254.1.1/")


class IPCheckTests(unittest.TestCase):
    """Tests for _is_private_or_reserved_ip and _resolve_to_private."""

    def test_loopback_is_private(self):
        self.assertTrue(_is_private_or_reserved_ip("127.0.0.1"))
        self.assertTrue(_is_private_or_reserved_ip("::1"))

    def test_private_range_is_private(self):
        self.assertTrue(_is_private_or_reserved_ip("192.168.1.1"))
        self.assertTrue(_is_private_or_reserved_ip("10.10.10.10"))
        self.assertTrue(_is_private_or_reserved_ip("172.16.0.1"))

    def test_public_ip_is_not_private(self):
        self.assertFalse(_is_private_or_reserved_ip("8.8.8.8"))
        self.assertFalse(_is_private_or_reserved_ip("1.1.1.1"))

    def test_localhost_resolves_to_private(self):
        ip = _resolve_to_private("localhost")
        self.assertIsNotNone(ip)

    def test_public_domain_does_not_resolve_to_private(self):
        ip = _resolve_to_private("example.com")
        self.assertIsNone(ip)


class WebFetchToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    """Tests for the async web_fetch function behavior."""

    def setUp(self):
        self.tool = create_web_fetch_tool()

    async def test_successful_fetch(self):
        html = "<html><head><title>Test Page</title></head><body><p>Hello world.</p></body></html>"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.text = html
        mock_response.headers = {}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with patch("xagent.tools.web_fetch_tool.httpx.AsyncClient", new=FakeAsyncClient):
            result = await self.tool(url="https://example.com")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["url"], "https://example.com")
        self.assertIn("Hello world", result["content"])
        self.assertEqual(result["title"], "Test Page")
        self.assertFalse(result["truncated"])
        self.assertGreater(result["content_length"], 0)

    async def test_content_truncation(self):
        # Generate content longer than _MAX_CONTENT_LENGTH
        long_text = "word " * (_MAX_CONTENT_LENGTH // 5 + 100)
        html = f"<html><body><p>{long_text}</p></body></html>"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.text = html
        mock_response.headers = {}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with patch("xagent.tools.web_fetch_tool.httpx.AsyncClient", new=FakeAsyncClient):
            result = await self.tool(url="https://example.com/long")

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["content"]), _MAX_CONTENT_LENGTH)

    async def test_http_404_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.reason_phrase = "Not Found"
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=mock_response
            )
        )

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with patch("xagent.tools.web_fetch_tool.httpx.AsyncClient", new=FakeAsyncClient):
            result = await self.tool(url="https://example.com/not-found")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["url"], "https://example.com/not-found")
        self.assertIn("HTTP 404", result["message"])

    async def test_timeout_error(self):
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.TimeoutException("timed out")

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with patch("xagent.tools.web_fetch_tool.httpx.AsyncClient", new=FakeAsyncClient):
            result = await self.tool(url="https://example.com/slow")

        self.assertEqual(result["status"], "error")
        self.assertIn("timed out", result["message"])

    async def test_ssrf_blocked(self):
        result = await self.tool(url="http://127.0.0.1:8080/admin")
        self.assertEqual(result["status"], "error")
        self.assertIn("private/reserved IP", result["message"])

    async def test_invalid_scheme_blocked(self):
        result = await self.tool(url="file:///etc/passwd")
        self.assertEqual(result["status"], "error")
        self.assertIn("Unsupported URL scheme", result["message"])

    async def test_empty_content_extraction(self):
        # HTML that Trafilatura cannot extract meaningful text from
        html = "<html><body></body></html>"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.text = html
        mock_response.headers = {}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with patch("xagent.tools.web_fetch_tool.httpx.AsyncClient", new=FakeAsyncClient):
            result = await self.tool(url="https://example.com/empty")

        self.assertEqual(result["status"], "error")
        self.assertIn("no readable content", result["message"].lower())

    async def test_request_error(self):
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with patch("xagent.tools.web_fetch_tool.httpx.AsyncClient", new=FakeAsyncClient):
            result = await self.tool(url="https://example.com")

        self.assertEqual(result["status"], "error")
        self.assertIn("Request failed", result["message"])


if __name__ == "__main__":
    unittest.main()
