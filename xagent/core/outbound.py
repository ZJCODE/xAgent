"""Deterministic outbound normalization, attachment extraction, and marker scrubbing."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

from ..schemas.attachment import workspace_attachment_from_path
from ..utils.image_utils import workspace_blob_relative_path

_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
_LEADING_TRANSCRIPT_HEADER_RE = re.compile(
    r"^\s*(?:\[(?:speaker|ambient context|my private reflection|scheduled task)[^\n\]]*\](?:\[[^\n\]]*\])*)\s*\n?",
    re.IGNORECASE,
)
_DIARY_HEADING_RE = re.compile(
    r"^#{1,3}\s+\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?\s*$"
)


def accepts_kwarg(fn: Callable[..., Any], kwarg: str) -> bool:
    """Return True when ``fn`` accepts ``kwarg`` or ``**kwargs``."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if kwarg in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def resolve_outbound_workspace_path(
    source: str,
    workspace_root: str | Path | None,
) -> Optional[Path]:
    """Resolve a Markdown link target to an existing regular file inside ``workspace_root``."""
    if workspace_root is None or not isinstance(source, str):
        return None
    cleaned = source.strip().strip("<>")
    if not cleaned:
        return None
    root = Path(workspace_root).expanduser().resolve()
    relative_path = workspace_blob_relative_path(cleaned)
    if relative_path:
        candidate = (root / relative_path).resolve()
    else:
        parsed = urlparse(cleaned)
        if parsed.scheme and parsed.scheme != "file":
            return None
        raw_path = unquote(parsed.path if parsed.scheme == "file" else cleaned)
        if not raw_path:
            return None
        path = Path(raw_path).expanduser()
        candidate = path.resolve() if path.is_absolute() else (root / raw_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def strip_leaked_transcript_markers(text: str) -> str:
    """Remove accidentally echoed leading transcript headers from outbound replies."""
    if not isinstance(text, str) or not text:
        return ""
    cleaned = text
    while True:
        updated = _LEADING_TRANSCRIPT_HEADER_RE.sub("", cleaned, count=1)
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned


def strip_diary_headings(text: str) -> str:
    """Strip accidental leading Markdown date/time headings from diary body slices."""
    if not isinstance(text, str) or not text:
        return ""
    lines = text.splitlines()
    while lines and (not lines[0].strip() or _DIARY_HEADING_RE.match(lines[0].strip())):
        lines.pop(0)
    return "\n".join(lines).strip()


def extract_outbound_workspace_attachments(
    text: str,
    workspace_root: str | Path | None,
    *,
    delivered_paths: Optional[set[str]] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Extract workspace file/image Markdown links into structured attachments."""
    if not isinstance(text, str) or not text or workspace_root is None:
        return text or "", []
    root = Path(workspace_root).expanduser().resolve()
    seen = delivered_paths if delivered_paths is not None else set()
    attachments: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []

    def collect(match: re.Match[str], *, is_image: bool) -> None:
        label = (match.group(1) or "").strip()
        target = (match.group(2) or "").strip()
        resolved = resolve_outbound_workspace_path(target, root)
        if resolved is None:
            return
        spans.append(match.span())
        attachment = workspace_attachment_from_path(
            resolved,
            root,
            caption=label if is_image else "",
        )
        if not attachment:
            return
        key = str(attachment.get("path") or attachment.get("blob_url") or "")
        if key and key not in seen:
            seen.add(key)
            attachments.append(attachment)

    for match in _MARKDOWN_IMAGE_RE.finditer(text):
        collect(match, is_image=True)
    for match in _MARKDOWN_LINK_RE.finditer(text):
        collect(match, is_image=False)
    if not spans:
        return text, []
    return remove_text_spans(text, spans), attachments


def normalize_outbound_reply(
    text: str,
    *,
    workspace_dir: str | Path | None = None,
    channel: Optional[str] = None,
    delivered_paths: Optional[set[str]] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Sanitize accidental headers and lift workspace Markdown links to attachments."""
    cleaned = strip_leaked_transcript_markers(text)
    if (channel or "").strip().lower() == "voice" or workspace_dir is None:
        return cleaned, []
    return extract_outbound_workspace_attachments(
        cleaned,
        workspace_dir,
        delivered_paths=delivered_paths,
    )


def remove_text_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove character spans from ``text`` and collapse excessive blank lines."""
    if not spans:
        return text
    pieces: list[str] = []
    last_index = 0
    for start, end in sorted(spans):
        if start < last_index:
            continue
        pieces.append(text[last_index:start])
        last_index = end
    pieces.append(text[last_index:])
    return re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()
