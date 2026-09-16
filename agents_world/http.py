"""HTTP sidecar on the world port: inhabitant page and neighbor discovery.

This is not world physics. The WebSocket upgrade continues into World.
GET /neighbors only helps the page knock on already-running minds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from .neighbors import list_local_agents
from .paths import validate_id
from .store import WorldStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
NEIGHBORS_ROOT_ENV = "AGENTS_WORLD_NEIGHBORS_ROOT"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}


def is_websocket_upgrade(request: Request) -> bool:
    upgrade = request.headers.get("Upgrade") or ""
    return "websocket" in upgrade.lower()


def page_neighbors_root() -> Optional[str]:
    raw = os.environ.get(NEIGHBORS_ROOT_ENV, "").strip()
    return raw or None


def process_http_request(
    connection: ServerConnection,
    request: Request,
    *,
    store: Optional[WorldStore] = None,
) -> Optional[Response]:
    """Serve GET / , GET /neighbors, GET /files/:id; return None to continue the WS handshake."""
    if is_websocket_upgrade(request):
        return None
    path = urlparse(request.path).path or "/"
    if path == "/neighbors":
        body = {"agents": list_local_agents(root=page_neighbors_root())}
        return _bytes_response(
            200,
            "OK",
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )
    if path.startswith("/files/"):
        return _file_response(path, store)
    if path == "/":
        path = "/index.html"
    rel = path.lstrip("/")
    target = (STATIC_DIR / rel).resolve()
    try:
        target.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return _not_found()
    if not target.is_file():
        return _not_found()
    suffix = target.suffix.lower()
    content_type = _MIME.get(suffix, "application/octet-stream")
    return _bytes_response(200, "OK", target.read_bytes(), content_type)


def _file_response(path: str, store: Optional[WorldStore]) -> Response:
    if store is None:
        return _not_found()
    file_id = path[len("/files/") :].strip("/")
    if "/" in file_id or not file_id:
        return _not_found()
    try:
        validate_id(file_id, label="file_id")
    except ValueError:
        return _not_found()
    found = store.get_file(file_id)
    if found is None:
        return _not_found()
    attachment, blob_path = found
    data = blob_path.read_bytes()
    headers_extra = {
        "Content-Disposition": _content_disposition(attachment.name, inline=attachment.mime.startswith("image/")),
    }
    return _bytes_response(
        200,
        "OK",
        data,
        attachment.mime or "application/octet-stream",
        extra=headers_extra,
    )


def _content_disposition(name: str, *, inline: bool) -> str:
    from urllib.parse import quote

    disposition = "inline" if inline else "attachment"
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in {";", '"', "\\"} else "_" for ch in name) or "file"
    encoded = quote(name)
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _not_found() -> Response:
    return _bytes_response(404, "Not Found", b"not found\n", "text/plain; charset=utf-8")


def _bytes_response(
    status: int,
    reason: str,
    body: bytes,
    content_type: str,
    extra: Optional[dict[str, str]] = None,
) -> Response:
    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    headers["Cache-Control"] = "no-store"
    headers["Connection"] = "close"
    if extra:
        for key, value in extra.items():
            headers[key] = value
    return Response(status, reason, headers, body)
