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

from xagent.tools.search_tool import DEEPSEEK_SEARCH_BASE_URL, is_placeholder_api_key
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
    if parsed.username or parsed.password:
        raise ValueError("URL must not include credentials (username or password)")

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


def _format_success_response(url: str, title: str, content: str) -> dict:
    """Build a standardized success response dict with content truncation."""
    extracted = content.strip()
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


def _normalize_deepseek_base_url(base_url: Optional[str]) -> str:
    raw = str(base_url or DEEPSEEK_SEARCH_BASE_URL).strip().rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3].rstrip("/")
    return raw or DEEPSEEK_SEARCH_BASE_URL


async def _fetch_via_deepseek(
    url: str,
    *,
    api_key: str,
    base_url: str = DEEPSEEK_SEARCH_BASE_URL,
) -> Optional[tuple[str, str]]:
    """Fetch clean Markdown content via DeepSeek's /v1/tools/fetch endpoint."""
    endpoint = f"{_normalize_deepseek_base_url(base_url)}/v1/tools/fetch"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        ) as client:
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": url},
            )
            if response.status_code >= 400:
                return None
            data = response.json()
    except Exception as exc:
        logger.warning("DeepSeek web fetch fallback failed for %s: %s", url, exc)
        return None

    if not isinstance(data, dict):
        return None
    content = str(data.get("content") or "").strip()
    if not content:
        return None
    title = str(data.get("title") or "").strip()
    return title, content


def create_web_fetch_tool(fetch_config: Optional[dict] = None):
    """Create the built-in web_fetch tool (always enabled, no config needed).

    When ``fetch_config`` provides a valid DeepSeek ``api_key``, local HTTP +
    Trafilatura extraction runs first (L1) and transparently falls back to
    DeepSeek's ``POST /v1/tools/fetch`` endpoint (L2) if the local request
    fails or extracts no readable content.

    Returns an async callable decorated with ``@function_tool`` that has a
    ``.tool_spec`` attribute for registration with the agent's ToolManager.
    """
    cfg = fetch_config if isinstance(fetch_config, dict) else {}
    raw_api_key = str(cfg.get("api_key") or "").strip()
    deepseek_api_key = (
        raw_api_key
        if raw_api_key and not is_placeholder_api_key(raw_api_key)
        else ""
    )
    deepseek_base_url = _normalize_deepseek_base_url(cfg.get("base_url"))

    @function_tool(
        name="web_fetch",
        description=(
            "Fetch a known web page URL and extract readable main text. "
            "Use when the URL is known; if the page is unknown and web_search is available, search first. "
            "May return little content for JavaScript-heavy pages. Cite the source URL when using fetched information."
        ),
        param_descriptions={
            "url": (
                "Full http:// or https:// URL to fetch."
            ),
        },
    )
    async def web_fetch(url: str) -> dict:
        # 1. Validate URL (scheme, length, credentials, SSRF)
        try:
            url = await _validate_url(url)
        except ValueError as e:
            return _error_response(url, str(e))

        async def _try_deepseek_fallback() -> Optional[dict]:
            if not deepseek_api_key:
                return None
            fallback = await _fetch_via_deepseek(
                url,
                api_key=deepseek_api_key,
                base_url=deepseek_base_url,
            )
            if fallback is None:
                return None
            fallback_title, fallback_content = fallback
            return _format_success_response(url, fallback_title, fallback_content)

        # 2. Fetch page content via local httpx (L1)
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
            fallback_response = await _try_deepseek_fallback()
            if fallback_response is not None:
                return fallback_response
            return _error_response(
                url,
                f"Request timed out after {_REQUEST_TIMEOUT} seconds.",
            )
        except httpx.HTTPStatusError as e:
            fallback_response = await _try_deepseek_fallback()
            if fallback_response is not None:
                return fallback_response
            return _error_response(
                url,
                f"HTTP {e.response.status_code}: {e.response.reason_phrase}",
            )
        except httpx.RequestError as e:
            fallback_response = await _try_deepseek_fallback()
            if fallback_response is not None:
                return fallback_response
            return _error_response(url, f"Request failed: {e}")

        # 3. Extract content with Trafilatura as Markdown (CPU-bound → run in thread)
        try:
            extracted = await asyncio.to_thread(
                trafilatura.extract,
                html,
                output_format="markdown",
                include_tables=True,
                include_links=True,
                no_fallback=False,
            )
            metadata = await asyncio.to_thread(
                trafilatura.extract_metadata, html
            )
            title = metadata.title.strip() if metadata and metadata.title else ""
        except Exception as e:
            logger.warning("Trafilatura extraction failed for %s: %s", url, e)
            fallback_response = await _try_deepseek_fallback()
            if fallback_response is not None:
                return fallback_response
            return _error_response(
                url, "Failed to extract readable content from the page."
            )

        if not extracted or not extracted.strip():
            fallback_response = await _try_deepseek_fallback()
            if fallback_response is not None:
                return fallback_response
            return _error_response(
                url, "The page appears to have no readable content."
            )

        return _format_success_response(url, title, extracted)

    return web_fetch
