"""Shared workspace attachment metadata helpers."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..utils.image_utils import workspace_blob_relative_path, workspace_blob_url


ATTACHMENT_KIND_FILE = "file"
ATTACHMENT_KIND_IMAGE = "image"
ATTACHMENT_METADATA_KEY = "attachments"
DEFAULT_FEISHU_ATTACHMENT_DIR = "temp/attachments/feishu"
DEFAULT_WEB_ATTACHMENT_DIR = "temp/attachments/web"
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_MESSAGE_ATTACHMENT_BYTES = 200 * 1024 * 1024

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceAttachment(BaseModel):
    """Channel-neutral metadata for a file saved under the agent workspace."""

    model_config = ConfigDict(extra="ignore")

    kind: str = Field(default=ATTACHMENT_KIND_FILE)
    path: str = Field(default="")
    blob_url: str = Field(default="")
    mime_type: str = Field(default="application/octet-stream")
    file_name: str = Field(default="")
    caption: str = Field(default="")
    size_bytes: Optional[int] = None
    source_channel: Optional[str] = None
    source_message_id: Optional[str] = None
    source_resource_id: Optional[str] = None
    source_resource_type: Optional[str] = None


def safe_attachment_filename(file_name: str, *, fallback: str = "attachment.bin", max_length: int = 180) -> str:
    """Return a conservative basename safe for workspace storage."""
    fallback_name = Path(fallback or "attachment.bin").name or "attachment.bin"
    name = Path(str(file_name or "").strip() or fallback_name).name
    name = _SAFE_FILENAME_RE.sub("_", name).strip("._ ")
    if not name:
        name = fallback_name
    if len(name) <= max_length:
        return name
    path = Path(name)
    suffix = path.suffix[:32]
    budget = max(8, max_length - len(suffix))
    return f"{path.stem[:budget]}{suffix}"


def attachment_kind(mime_type: str = "", file_name: str = "") -> str:
    """Classify an attachment into the small kind set used by channels."""
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if not normalized and file_name:
        normalized, _ = mimetypes.guess_type(file_name)
        normalized = normalized or ""
    return ATTACHMENT_KIND_IMAGE if normalized.startswith("image/") else ATTACHMENT_KIND_FILE


def normalize_attachment(value: Any) -> Optional[dict[str, Any]]:
    """Normalize user/channel metadata to the workspace attachment shape."""
    if isinstance(value, WorkspaceAttachment):
        raw = value.model_dump()
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        return None

    blob_url = str(raw.get("blob_url") or "").strip()
    path = str(raw.get("path") or raw.get("workspace_path") or "").strip().strip("/")
    if not path and blob_url:
        path = workspace_blob_relative_path(blob_url)
    if not blob_url and path:
        blob_url = workspace_blob_url(path)
    if not path and not blob_url:
        return None

    file_name = safe_attachment_filename(str(raw.get("file_name") or raw.get("original_name") or Path(path).name or "attachment.bin"))
    mime_type = str(raw.get("mime_type") or "").split(";", 1)[0].strip().lower()
    if not mime_type:
        mime_type, _ = mimetypes.guess_type(file_name or path)
        mime_type = mime_type or "application/octet-stream"

    raw_kind = str(raw.get("kind") or "").strip().lower()
    kind = raw_kind if raw_kind in {ATTACHMENT_KIND_FILE, ATTACHMENT_KIND_IMAGE} else attachment_kind(mime_type, file_name)
    normalized: dict[str, Any] = {
        "kind": kind,
        "path": path,
        "blob_url": blob_url,
        "mime_type": mime_type,
        "file_name": file_name,
        "caption": str(raw.get("caption") or "").strip(),
        "size_bytes": _optional_int(raw.get("size_bytes") or raw.get("size")),
        "source_channel": _optional_str(raw.get("source_channel")),
        "source_message_id": _optional_str(raw.get("source_message_id")),
        "source_resource_id": _optional_str(raw.get("source_resource_id")),
        "source_resource_type": _optional_str(raw.get("source_resource_type")),
    }
    return {key: item for key, item in normalized.items() if item not in (None, "")}


def dedupe_attachments(values: list[Any]) -> list[dict[str, Any]]:
    """Normalize and dedupe attachments while preserving order."""
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        attachment = normalize_attachment(value)
        if not attachment:
            continue
        path = str(attachment.get("path") or "").strip()
        blob_url = str(attachment.get("blob_url") or "").strip()
        key = ("path", path) if path else ("blob_url", blob_url)
        if not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(attachment)
    return deduped


def attachment_markdown(value: Any) -> str:
    """Render one attachment as markdown for channels that understand links."""
    attachment = normalize_attachment(value)
    if not attachment:
        return ""
    blob_url = str(attachment.get("blob_url") or "").strip()
    if not blob_url:
        return ""
    label = _markdown_label(str(
        attachment.get("caption") or attachment.get("file_name") or Path(str(attachment.get("path") or "")).name or "Attachment"
    ))
    if attachment.get("kind") == ATTACHMENT_KIND_IMAGE:
        return f"![{label}]({blob_url})"
    return f"[{label}]({blob_url})"


def attachment_manifest_markdown(values: list[Any], *, title: str = "Attached files") -> str:
    """Render a compact file manifest for model/user-visible text."""
    attachments = dedupe_attachments(values)
    if not attachments:
        return ""
    lines = [f"{title}:"]
    for attachment in attachments:
        link = attachment_markdown(attachment)
        if not link:
            continue
        details = []
        mime_type = str(attachment.get("mime_type") or "").strip()
        if mime_type:
            details.append(mime_type)
        size_bytes = attachment.get("size_bytes")
        if isinstance(size_bytes, int) and size_bytes >= 0:
            details.append(f"{size_bytes} bytes")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"- {link}{suffix}")
    return "\n".join(lines) if len(lines) > 1 else ""


def attachment_image_sources(values: list[Any]) -> list[str]:
    """Return current-turn image sources represented by workspace attachments."""
    sources: list[str] = []
    seen: set[str] = set()
    for attachment in dedupe_attachments(values):
        if attachment.get("kind") != ATTACHMENT_KIND_IMAGE:
            continue
        source = str(attachment.get("blob_url") or attachment.get("path") or "").strip()
        if source and source not in seen:
            seen.add(source)
            sources.append(source)
    return sources


def workspace_attachment_from_path(
    path: str | Path,
    workspace_root: str | Path,
    *,
    caption: str = "",
    source_channel: Optional[str] = None,
    source_message_id: Optional[str] = None,
    source_resource_id: Optional[str] = None,
    source_resource_type: Optional[str] = None,
) -> dict[str, Any]:
    """Create normalized metadata for an existing file under workspace_root."""
    root = Path(workspace_root).expanduser().resolve()
    resolved = Path(path).expanduser().resolve()
    relative_path = resolved.relative_to(root).as_posix()
    mime_type, _ = mimetypes.guess_type(resolved.name)
    return normalize_attachment({
        "kind": attachment_kind(mime_type or "", resolved.name),
        "path": relative_path,
        "blob_url": workspace_blob_url(relative_path),
        "mime_type": mime_type or "application/octet-stream",
        "file_name": resolved.name,
        "caption": caption,
        "size_bytes": resolved.stat().st_size,
        "source_channel": source_channel,
        "source_message_id": source_message_id,
        "source_resource_id": source_resource_id,
        "source_resource_type": source_resource_type,
    }) or {}


def save_workspace_attachment_bytes(
    data: bytes,
    workspace_root: str | Path,
    *,
    directory: str,
    file_name: str = "attachment.bin",
    mime_type: str = "",
    caption: str = "",
    source_channel: Optional[str] = None,
    source_message_id: Optional[str] = None,
    source_resource_id: Optional[str] = None,
    source_resource_type: Optional[str] = None,
) -> dict[str, Any]:
    """Save bytes under workspace and return normalized attachment metadata."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("Attachment data cannot be empty")
    root = Path(workspace_root).expanduser().resolve()
    output_dir = (root / directory.strip("/")).resolve()
    if not output_dir.is_relative_to(root):
        raise ValueError("Attachment output directory must stay inside workspace")
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_name = safe_attachment_filename(file_name)
    guessed_mime_type, _ = mimetypes.guess_type(safe_name)
    normalized_mime_type = str(mime_type or guessed_mime_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    digest = hashlib.sha1(data).hexdigest()[:12]
    identity = hashlib.sha1(
        f"{source_channel or ''}:{source_message_id or ''}:{source_resource_id or ''}:{source_resource_type or ''}:{digest}".encode("utf-8")
    ).hexdigest()[:12]
    name_path = Path(safe_name)
    suffix = name_path.suffix
    stem = name_path.stem or "attachment"
    target = output_dir / f"{stem}-{identity}{suffix}"
    target.write_bytes(data)
    return workspace_attachment_from_path(
        target,
        root,
        caption=caption,
        source_channel=source_channel,
        source_message_id=source_message_id,
        source_resource_id=source_resource_id,
        source_resource_type=source_resource_type,
    )


def _optional_str(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _markdown_label(value: str) -> str:
    label = str(value or "Attachment").strip() or "Attachment"
    return label.replace("[", "(").replace("]", ")").replace("\n", " ")