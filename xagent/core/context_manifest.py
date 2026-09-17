"""Context manifest: per-turn accounting of assembled prompt sections."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .context_budget import content_char_length, estimate_tokens

logger = logging.getLogger(__name__)

_MANIFEST_LOG_MAX_LINES = 200
_MANIFEST_LOG_NAME = ".context_manifest.jsonl"


@dataclass
class ManifestEntry:
    name: str
    role: str
    kind: str
    chars: int
    est_tokens: int
    trust: str
    priority: str
    authority: str
    provenance: dict = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ContextManifest:
    turn_id: str
    task_mode: str
    inbox_kind: str
    entries: list[ManifestEntry]
    tools_chars: int
    tools_count: int
    total_chars: int
    total_est_tokens: int
    budget_tokens: Optional[int] = None
    provider_shape: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["entries"] = [asdict(entry) for entry in self.entries]
        return payload

    def summary_line(self) -> str:
        return (
            f"context_manifest turn={self.turn_id} mode={self.task_mode} "
            f"sections={len(self.entries)} chars={self.total_chars} "
            f"est_tokens={self.total_est_tokens} tools={self.tools_count}"
        )


def manifest_entry_from_message(
    message: dict,
    *,
    kind: str,
    trust: str,
    priority: str,
    authority: str,
    provenance: Optional[dict] = None,
) -> ManifestEntry:
    content = message.get("content")
    chars = content_char_length(content)
    text_for_tokens = content if isinstance(content, str) else str(content)
    return ManifestEntry(
        name=str(message.get("name") or ""),
        role=str(message.get("role") or ""),
        kind=kind,
        chars=chars,
        est_tokens=estimate_tokens(text_for_tokens),
        trust=trust,
        priority=priority,
        authority=authority,
        provenance=dict(provenance or {}),
    )


def build_context_manifest(
    *,
    turn_id: str,
    task_mode: str,
    inbox_kind: str,
    instruction_entries: list[ManifestEntry],
    turn_entries: list[ManifestEntry],
    tool_specs: Optional[list] = None,
    budget_tokens: Optional[int] = None,
    provider_messages: Optional[list[dict]] = None,
) -> ContextManifest:
    tools = list(tool_specs or [])
    tools_text = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
    tools_chars = len(tools_text)
    entries = [*instruction_entries, *turn_entries]
    total_chars = sum(entry.chars for entry in entries) + tools_chars
    total_est_tokens = sum(entry.est_tokens for entry in entries) + estimate_tokens(tools_text)
    provider_shape = _provider_shape(provider_messages or [])
    return ContextManifest(
        turn_id=turn_id,
        task_mode=task_mode,
        inbox_kind=inbox_kind,
        entries=entries,
        tools_chars=tools_chars,
        tools_count=len(tools),
        total_chars=total_chars,
        total_est_tokens=total_est_tokens,
        budget_tokens=budget_tokens,
        provider_shape=provider_shape,
    )


def _provider_shape(messages: list[dict]) -> dict[str, int]:
    system_messages = 0
    user_messages = 0
    images = 0
    for message in messages:
        role = str(message.get("role") or "")
        if role == "system":
            system_messages += 1
        elif role == "user":
            user_messages += 1
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    images += 1
    return {
        "system_messages": system_messages,
        "user_messages": user_messages,
        "images": images,
    }


def emit_context_manifest(manifest: ContextManifest, *, workspace_dir: Optional[str | Path] = None) -> None:
    logger.debug(manifest.summary_line())
    if os.environ.get("XAGENT_CONTEXT_MANIFEST", "").strip() not in {"1", "true", "yes"}:
        return
    if workspace_dir is None:
        return
    root = Path(workspace_dir).expanduser()
    log_path = root / "messages" / _MANIFEST_LOG_NAME
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if log_path.is_file():
            existing = log_path.read_text(encoding="utf-8").splitlines()
        existing.append(json.dumps(manifest.to_dict(), ensure_ascii=False))
        trimmed = existing[-_MANIFEST_LOG_MAX_LINES:]
        log_path.write_text("\n".join(trimmed) + ("\n" if trimmed else ""), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write context manifest log: %s", exc)
