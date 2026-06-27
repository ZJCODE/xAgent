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
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_META_PATTERN = re.compile(
    r'^<!--\s*rel\s+(?P<attrs>.*?)\s*-->\s*$',
)
_ATTR_PATTERN = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_SLUG_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")


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

    Files live under ``<root>/`` with a filesystem-safe slug derived from the
    person key plus a short hash to avoid collisions between distinct keys that
    sanitise to the same slug. Each file starts with a single metadata comment
    line owned by this store, followed by the LLM-managed card body.
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

    def _slug(self, key: str) -> str:
        sanitized = _SLUG_UNSAFE.sub("_", key.strip()).strip("_") or "person"
        digest = hashlib.blake2b(key.strip().encode("utf-8"), digest_size=4).hexdigest()
        return f"{sanitized[:48]}.{digest}"

    def card_path(self, key: str) -> Path:
        return self.root / f"{self._slug(key)}.md"

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
            meta, _ = self._parse_meta(text)
            key = meta.get("key", "")
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
        channel, user_id = card.channel, card.user_id
        if not channel or not user_id:
            split_channel, split_user = self.split_key(card.key)
            channel = channel or split_channel
            user_id = user_id or split_user
        meta = (
            f'<!-- rel key="{self._escape(card.key)}" '
            f'name="{self._escape(card.display_name)}" '
            f'channel="{self._escape(channel)}" '
            f'user_id="{self._escape(user_id)}" '
            f'updated="{self._escape(updated)}" -->'
        )
        return f"{meta}\n\n{card.body.strip()}\n"

    def _parse(self, key: str, text: str) -> RelationshipCard:
        meta, body = self._parse_meta(text)
        channel, user_id = self.split_key(meta.get("key", key))
        return RelationshipCard(
            key=meta.get("key", key),
            body=body.strip(),
            display_name=meta.get("name", ""),
            channel=meta.get("channel", channel),
            user_id=meta.get("user_id", user_id),
            updated=meta.get("updated", ""),
        )

    @staticmethod
    def _parse_meta(text: str) -> Tuple[dict, str]:
        lines = text.splitlines()
        if not lines:
            return {}, ""
        match = _META_PATTERN.match(lines[0].strip())
        if not match:
            return {}, text
        attrs = {
            attr_match.group(1): RelationshipStore._unescape(attr_match.group(2))
            for attr_match in _ATTR_PATTERN.finditer(match.group("attrs"))
        }
        body = "\n".join(lines[1:]).strip()
        return attrs, body

    @staticmethod
    def _escape(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    @staticmethod
    def _unescape(value: str) -> str:
        return value.replace('\\"', '"').replace("\\\\", "\\")

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
        return sorted(path for path in search_dir.glob("*.md") if path.is_file())
