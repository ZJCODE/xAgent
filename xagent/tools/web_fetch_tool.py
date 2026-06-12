"""Built-in web page fetch tool using Trafilatura for content extraction.

Fetches a URL, extracts clean readable text via Trafilatura, and returns
the result. All configuration is hardcoded — this is an always-on built-in
tool requiring no config.yaml exposure.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import urllib.parse
from typing import Optional

import httpx
import trafilatura

from xagent.utils.tool_decorator import function_tool

logger = logging.getLogger(__name__)

# --- Hardcoded constants (no config.yaml exposure) ---

_REQUEST_TIMEOUT = 30  # seconds
_MAX_CONTENT_LENGTH = 100_000  # characters
_MAX_REDIRECTS = 5
_MAX_BODY_SIZE = 5 * 1024 * 1024  # 5 MB
_MAX_URL_LENGTH = 8192
_SUPPORTED_SCHEMES = {"http", "https"}

# Private / reserved IP ranges to block (SSRF prevention)
_BLOCKED_IP_NETWORKS = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("224.0.0.0/4"),
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("ff00::/8"),
]


def _is_private_or_reserved_ip(host: str) -> bool:
    """Check if a host string is a private / loopback / reserved IP address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    for network in _BLOCKED_IP_NETWORKS:
        if addr in network:
            return True
    return False


async def _resolve_to_private(hostname: str) -> Optional[str]:
    """Resolve hostname and return the first private/reserved IP found, or None.

    Returns the private IP string if the hostname resolves to any address that
    is not safe to fetch, otherwise None.
    """
    try:
        addrinfo = await asyncio.to_thread(
            socket.getaddrinfo, hostname, 80, 0, 0, socket.IPPROTO_TCP
        )
    except socket.gaierror:
        return None  # DNS failure — let the HTTP client report the real error

    for _family, _socktype, _proto, _canonname, sockaddr in addrinfo:
        ip = sockaddr[0]
        if _is_private_or_reserved_ip(ip):
            return ip
    return None


async def _validate_url(url: str) -> str:
    """Validate a URL for safety. Returns the stripped URL or raises ValueError."""
    url = url.strip()
    if not url:
        raise ValueError("URL is required")
    if len(url) > _MAX_URL_LENGTH:
        raise ValueError(
            f"URL exceeds maximum length of {_MAX_URL_LENGTH} characters"
        )

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _SUPPORTED_SCHEMES:
        raise ValueError(
            f"Unsupported URL scheme '{parsed.scheme}'. "
            "Only http:// and https:// are allowed."
        )
    if not parsed.hostname:
        raise ValueError("URL must include a valid hostname")

    # SSRF check — reject URLs that resolve to private/reserved IPs
    private_ip = await _resolve_to_private(parsed.hostname)
    if private_ip is not None:
        raise ValueError(
            f"URL hostname resolves to a private/reserved IP address "
            f"({private_ip}), which is not allowed for security reasons."
        )

    return url


def _error_response(url: str, message: str) -> dict:
    """Build a standardized error response dict."""
    return {"status": "error", "url": url, "message": message}


def create_web_fetch_tool():
    """Create the built-in web_fetch tool (always enabled, no config needed).

    Returns an async callable decorated with ``@function_tool`` that has a
    ``.tool_spec`` attribute for registration with the agent's ToolManager.
    """

    @function_tool(
        name="web_fetch",
        description=(
            "Fetch a web page URL and extract clean readable text content. "
            "Strips navigation, ads, and boilerplate to return the main page "
            "content. Use this when you need to read the full content of a "
            "specific web page — articles, documentation, blog posts, etc. "
            "Use web_search first if you need to find relevant pages."
        ),
        param_descriptions={
            "url": (
                "The full URL of the web page to fetch, including the "
                "http:// or https:// scheme."
            ),
        },
    )
    async def web_fetch(url: str) -> dict:
        # 1. Validate URL (scheme, length, SSRF)
        try:
            url = await _validate_url(url)
        except ValueError as e:
            return _error_response(url, str(e))

        # 2. Fetch page content via httpx
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_REQUEST_TIMEOUT),
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()

                # Guard against oversized responses
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > _MAX_BODY_SIZE:
                    return _error_response(
                        url,
                        f"Response body size ({int(content_length)} bytes) "
                        f"exceeds the limit of {_MAX_BODY_SIZE} bytes.",
                    )
                html = response.text
        except httpx.TimeoutException:
            return _error_response(
                url,
                f"Request timed out after {_REQUEST_TIMEOUT} seconds.",
            )
        except httpx.HTTPStatusError as e:
            return _error_response(
                url,
                f"HTTP {e.response.status_code}: {e.response.reason_phrase}",
            )
        except httpx.RequestError as e:
            return _error_response(url, f"Request failed: {e}")

        # 3. Extract content with Trafilatura (CPU-bound → run in thread)
        try:
            extracted = await asyncio.to_thread(
                trafilatura.extract,
                html,
                output_format="txt",
                no_fallback=False,
            )
            metadata = await asyncio.to_thread(
                trafilatura.extract_metadata, html
            )
            title = metadata.title.strip() if metadata and metadata.title else ""
        except Exception as e:
            logger.warning("Trafilatura extraction failed for %s: %s", url, e)
            return _error_response(
                url, "Failed to extract readable content from the page."
            )

        if not extracted or not extracted.strip():
            return _error_response(
                url, "The page appears to have no readable content."
            )

        # 4. Truncate if needed
        extracted = extracted.strip()
        truncated = len(extracted) > _MAX_CONTENT_LENGTH
        if truncated:
            extracted = extracted[:_MAX_CONTENT_LENGTH]

        return {
            "status": "ok",
            "url": url,
            "title": title,
            "content": extracted,
            "content_length": len(extracted),
            "truncated": truncated,
        }

    return web_fetch
