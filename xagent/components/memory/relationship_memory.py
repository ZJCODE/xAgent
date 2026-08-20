"""Per-person relationship cards derived from the unified diary memory.

Relationship cards are a *derived view* over the agent's single diary stream,
not a second source of truth. The diary remains the authoritative memory
carrier; each card is a regenerable projection that keeps durable relational
facts about one person (who they are to the agent, shared history, open
commitments, disclosure boundaries) readily available for dialogue and
subconscious routing.

This class owns file layout and I/O only. Deciding *what* a relationship
contains and *when* to update it lives in higher layers (journal service and
memory handler), mirroring how :class:`MarkdownMemory` separates storage from
policy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_SLUG_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")
_PLATFORM_ID_PREFIXES = ("ou_", "on_", "cli_", "oc_")
_GENERIC_DISPLAY_NAMES = frozenset({
    "feishu user",
    "weixin user",
    "wechat user",
    "unknown",
})
_CHANNEL_CONTACT_LABELS = {
    "feishu": "Feishu contact",
    "weixin": "Weixin contact",
    "wechat": "Weixin contact",
    "api": "API contact",
    "voice": "Voice contact",
}
_IDENTITY_CHANNELS = frozenset(_CHANNEL_CONTACT_LABELS)


def human_display_name(value: Any, *, user_id: str = "", key: str = "") -> str:
    """Return a personal name, or empty when the value is missing or an id."""
    name = str(value or "").strip()
    if not name:
        return ""
    if user_id and name == str(user_id).strip():
        return ""
    if key and name == str(key).strip():
        return ""
    if name.lower() in _GENERIC_DISPLAY_NAMES:
        return ""
    if name.startswith(_PLATFORM_ID_PREFIXES):
        return ""
    channel, separator, rest = name.partition(":")
    if separator and channel in _IDENTITY_CHANNELS and rest.strip():
        return ""
    return name


def anonymous_contact_label(channel: str = "") -> str:
    """Fallback header when a card has no personal name yet."""
    return _CHANNEL_CONTACT_LABELS.get((channel or "").strip().lower(), "contact")


def format_speaker_label(user_id: str = "", display_name: str = "") -> str:
    """Return ``Name(id)`` when both are known, otherwise whichever exists."""
    stable_id = str(user_id or "").strip()
    name = human_display_name(display_name, user_id=stable_id)
    if name and stable_id and name != stable_id:
        return f"{name}({stable_id})"
    return name or stable_id


def speaker_address_name(user_id: str = "", display_name: str = "") -> str:
    """Name to call this person in prompts and replies.

    ``Name(id)`` is a transcript marker. Gluing the id into ``Current speaker``
    makes the model treat the whole blob as metadata it must not mention, then
    claim it does not know them.
    """
    stable_id = str(user_id or "").strip()
    return human_display_name(display_name, user_id=stable_id) or stable_id


@dataclass(frozen=True)
class RelationshipCard:
    """A single person's relationship card.

    ``key`` is the stable identity (``channel:user_id``). ``body`` is the
    first-person card prose produced by the journal LLM service.
    """

    key: str
    body: str
    display_name: str = ""
    channel: str = ""
    user_id: str = ""
    updated: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.body.strip()


class RelationshipStore:
    """Store relationship cards as one markdown file per person.

    Files live under ``<root>/<channel>/<user_id>.md``. Each file starts with
    YAML frontmatter owned by this store, followed by the LLM-managed card body.
    """

    def __init__(self, relationships_dir: str) -> None:
        self.root = Path(relationships_dir).expanduser()
        self._write_lock = asyncio.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Key / path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(channel: Optional[str], user_id: Optional[str]) -> str:
        """Build the stable person key from channel and user id."""
        safe_channel = (channel or "unknown").strip() or "unknown"
        safe_user = (user_id or "unknown").strip() or "unknown"
        return f"{safe_channel}:{safe_user}"

    @staticmethod
    def split_key(key: str) -> Tuple[str, str]:
        """Split a person key back into ``(channel, user_id)``."""
        channel, _, user_id = key.partition(":")
        return channel, user_id

    @staticmethod
    def _safe_segment(value: str) -> str:
        sanitized = _SLUG_UNSAFE.sub("_", value.strip()).strip("_") or "unknown"
        return sanitized[:64]

    def card_path(self, key: str) -> Path:
        channel, user_id = self.split_key(key)
        return self.root / self._safe_segment(channel) / f"{self._safe_segment(user_id)}.md"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def read_card(self, key: str) -> Optional[RelationshipCard]:
        text = await asyncio.to_thread(self._read_text_sync, self.card_path(key))
        if not text.strip():
            return None
        return self._parse(key, text)

    async def read_cards(self, keys: List[str]) -> List[RelationshipCard]:
        """Read multiple cards, preserving order and skipping missing ones."""
        cards: List[RelationshipCard] = []
        seen: set[str] = set()
        for key in keys:
            if not key or key in seen:
                continue
            seen.add(key)
            card = await self.read_card(key)
            if card is not None and not card.is_empty:
                cards.append(card)
        return cards

    async def list_keys(self) -> List[str]:
        """List the person keys of all stored cards."""
        paths = await asyncio.to_thread(self._list_paths_sync, self.root)
        keys: List[str] = []
        for path in paths:
            text = await asyncio.to_thread(self._read_text_sync, path)
            meta, _ = self._split_frontmatter(text)
            key = str(meta.get("key") or "").strip()
            if key:
                keys.append(key)
        return keys

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def write_card(self, card: RelationshipCard) -> Path:
        """Write (overwrite) a person's relationship card atomically."""
        path = self.card_path(card.key)
        rendered = self._render(card)
        async with self._write_lock:
            await asyncio.to_thread(self._write_atomic_sync, path, rendered)
        logger.debug("Wrote relationship card: %s (%d chars)", path, len(card.body))
        return path

    # ------------------------------------------------------------------
    # Render / parse
    # ------------------------------------------------------------------

    def _render(self, card: RelationshipCard) -> str:
        updated = card.updated or date.today().isoformat()
        frontmatter: Dict[str, Any] = {"key": card.key}
        name = human_display_name(card.display_name, user_id=card.user_id, key=card.key)
        if name:
            frontmatter["name"] = name
        frontmatter["updated"] = updated
        header = yaml.safe_dump(
            frontmatter,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ).strip()
        return f"---\n{header}\n---\n\n{card.body.strip()}\n"

    def _parse(self, key: str, text: str) -> RelationshipCard:
        meta, body = self._split_frontmatter(text)
        resolved_key = str(meta.get("key") or key).strip() or key
        channel, user_id = self.split_key(resolved_key)
        return RelationshipCard(
            key=resolved_key,
            body=body.strip(),
            display_name=human_display_name(
                meta.get("name", ""),
                user_id=user_id,
                key=resolved_key,
            ),
            channel=channel,
            user_id=user_id,
            updated=str(meta.get("updated") or "").strip(),
        )

    @staticmethod
    def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
        lines = str(text or "").splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            return {}, str(text or "").strip()
        closing_index = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                closing_index = index
                break
        if closing_index is None:
            return {}, str(text or "").strip()
        raw = "".join(lines[1:closing_index])
        body = "".join(lines[closing_index + 1:]).strip()
        try:
            loaded = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            logger.warning(
                "Relationship frontmatter is not valid YAML, falling back to body only: %s",
                exc,
            )
            return {}, body
        if not isinstance(loaded, dict):
            return {}, body
        return loaded, body

    # ------------------------------------------------------------------
    # Sync I/O primitives
    # ------------------------------------------------------------------

    @staticmethod
    def _read_text_sync(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _write_atomic_sync(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)

    @staticmethod
    def _list_paths_sync(search_dir: Path) -> List[Path]:
        if not search_dir.exists():
            return []
        return sorted(path for path in search_dir.rglob("*.md") if path.is_file())
