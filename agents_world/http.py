"""HTTP sidecar on the hub port: page, world catalog, neighbors, files.

This is not world physics. WebSocket upgrades to /ws/{world_id} continue into World.
websockets does not read request bodies, so create uses path + query.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from .neighbors import list_local_agents
from .paths import validate_id, validate_world_id

if TYPE_CHECKING:
    from .server import WorldHub

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


def page_neighbors_root(hub: Optional["WorldHub"] = None) -> Optional[str]:
    raw = os.environ.get(NEIGHBORS_ROOT_ENV, "").strip()
    if raw:
        return raw
    if hub is not None:
        return str(hub.data_root)
    return None


async def process_http_request(
    connection: ServerConnection,
    request: Request,
    *,
    hub: Optional["WorldHub"] = None,
) -> Optional[Response]:
    """Serve catalog/static; return None so /ws/{id} continues the handshake."""
    if is_websocket_upgrade(request):
        return None
    parsed = urlparse(request.path)
    path = parsed.path or "/"
    method = str(getattr(request, "method", "GET") or "GET").upper()
    query = parse_qs(parsed.query or "")

    if path == "/neighbors":
        body = {"agents": list_local_agents(root=page_neighbors_root(hub))}
        return _json_response(200, "OK", body)

    if path == "/worlds" and method == "GET":
        if hub is None:
            return _not_found()
        return _json_response(200, "OK", {"worlds": hub.list_summaries()})

    # GET /worlds/create?name=  (websockets HTTP is GET-only)
    if path == "/worlds/create" and method == "GET":
        if hub is None:
            return _not_found()
        return await _create_world(hub, name=_query_one(query, "name"))

    world_file = _parse_world_file_path(path)
    if world_file is not None and method == "GET":
        return _world_file_response(hub, *world_file)

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


def _query_one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0] if values else "").strip()


def _parse_world_file_path(path: str) -> Optional[tuple[str, str]]:
    parts = [p for p in path.split("/") if p]
    if len(parts) != 4 or parts[0] != "worlds" or parts[2] != "files":
        return None
    try:
        world_id = validate_world_id(unquote(parts[1]))
        file_id = validate_id(unquote(parts[3]), label="file_id")
    except ValueError:
        return None
    return world_id, file_id


async def _create_world(hub: "WorldHub", *, name: str) -> Response:
    try:
        world = hub.create_world(name=name)
    except FileExistsError as exc:
        return _json_response(409, "Conflict", {"error": str(exc)})
    except ValueError as exc:
        return _json_response(400, "Bad Request", {"error": str(exc)})
    await world.start()
    return _json_response(
        201,
        "Created",
        {
            "id": world.world_id,
            "name": world.store.name,
            "latest_seq": 0,
            "present_count": 0,
        },
    )


def _world_file_response(hub: Optional["WorldHub"], world_id: str, file_id: str) -> Response:
    if hub is None:
        return _not_found()
    world = hub.get(world_id)
    if world is None:
        return _not_found()
    found = world.store.get_file(file_id)
    if found is None:
        return _not_found()
    attachment, blob_path = found
    data = blob_path.read_bytes()
    headers_extra = {
        "Content-Disposition": _content_disposition(
            attachment.name,
            inline=attachment.mime.startswith("image/"),
        ),
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
    ascii_name = (
        "".join(ch if 32 <= ord(ch) < 127 and ch not in {";", '"', "\\"} else "_" for ch in name)
        or "file"
    )
    encoded = quote(name)
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _json_response(status: int, reason: str, body: Any) -> Response:
    return _bytes_response(
        status,
        reason,
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
        "application/json; charset=utf-8",
    )


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
