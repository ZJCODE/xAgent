"""SQLite recipient directory: deliverable addresses, not relationship memory.

``recipient_key`` is always ``channel:user_id`` where ``user_id`` is the
channel-stable identity. Display names are stored separately. Sensitive or
ephemeral tokens never enter this database.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

RECIPIENT_ROUTES_TABLE = "recipient_routes"
DIRECTORY_META_TABLE = "directory_meta"
LEGACY_IMPORT_META_KEY = "legacy_contacts_import"
LEGACY_CONTACTS_FILENAME = "contacts.json"

_SENSITIVE_TARGET_KEYS = {
    "context_token",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "authorization",
    "password",
    "secret",
    "account_id",
}

_FEISHU_STABLE_PREFIXES = ("ou_", "on_", "cli_")


@dataclass(frozen=True)
class RecipientRoute:
    """One deliverable direct-session address."""

    channel: str
    user_id: str
    display_name: str = ""
    target: Dict[str, Any] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    last_seen: str = ""

    @property
    def recipient_key(self) -> str:
        return make_recipient_key(self.channel, self.user_id)


def make_recipient_key(channel: Optional[str], user_id: Optional[str]) -> str:
    safe_channel = str(channel or "").strip() or "unknown"
    safe_user = str(user_id or "").strip() or "unknown"
    return f"{safe_channel}:{safe_user}"


def split_recipient_key(key: str) -> tuple[str, str]:
    channel, _, user_id = str(key or "").partition(":")
    return channel, user_id


def sanitize_target(target: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop secrets and ephemeral tokens from a channel target payload."""
    cleaned: Dict[str, Any] = {}
    for raw_key, value in dict(target or {}).items():
        key = str(raw_key).strip()
        if not key or key.lower() in _SENSITIVE_TARGET_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            cleaned[key] = value
        elif isinstance(value, dict):
            nested = sanitize_target(value)
            if nested:
                cleaned[key] = nested
        elif isinstance(value, list):
            continue
        else:
            cleaned[key] = str(value)
    return cleaned


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _looks_like_group_target(target: Dict[str, Any]) -> bool:
    if bool(target.get("is_group")):
        return True
    chat_type = str(target.get("chat_type") or "").strip().lower()
    if chat_type in {"group", "topic"}:
        return True
    for value in (target.get("group_id"), target.get("from_user_id"), target.get("to_user_id")):
        if "@chatroom" in str(value or ""):
            return True
    return False


def _feishu_stable_user_id(user_id: str, target: Dict[str, Any]) -> str:
    sender_id = str(target.get("sender_id") or "").strip()
    if sender_id:
        return sender_id
    raw = str(user_id or "").strip()
    if raw.startswith(_FEISHU_STABLE_PREFIXES):
        return raw
    return ""


class RecipientDirectory:
    """Process-shared SQLite directory of initiative-eligible recipients."""

    def __init__(self, sqlite_path: Path | str) -> None:
        self.path = Path(sqlite_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {RECIPIENT_ROUTES_TABLE} (
                    recipient_key TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    target_json TEXT NOT NULL DEFAULT '{{}}',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    last_seen TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{RECIPIENT_ROUTES_TABLE}_channel_user
                ON {RECIPIENT_ROUTES_TABLE} (channel, user_id)
                """
            )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {DIRECTORY_META_TABLE} (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def upsert(
        self,
        *,
        channel: str,
        user_id: str,
        display_name: str = "",
        target: Optional[Dict[str, Any]] = None,
        aliases: Optional[Iterable[str]] = None,
        last_seen: Optional[str] = None,
        allow_group: bool = False,
    ) -> Optional[RecipientRoute]:
        """Insert or update a direct-session route. Groups are ignored."""
        channel_name = str(channel or "").strip()
        stable_id = str(user_id or "").strip()
        if not channel_name or not stable_id:
            return None
        payload = sanitize_target(target)
        if not allow_group and _looks_like_group_target(payload):
            return None
        incoming_aliases = self._normalize_aliases(aliases, extra=(display_name, stable_id))
        seen_at = str(last_seen or "").strip() or _now_iso()
        key = make_recipient_key(channel_name, stable_id)

        with self._connect() as connection:
            existing = connection.execute(
                f"SELECT * FROM {RECIPIENT_ROUTES_TABLE} WHERE recipient_key = ?",
                (key,),
            ).fetchone()
            merged_aliases = incoming_aliases
            display = str(display_name or "").strip()
            if existing is not None:
                previous_aliases = self._load_aliases(existing["aliases_json"])
                merged_aliases = self._normalize_aliases(
                    [*previous_aliases, *incoming_aliases],
                    extra=(display, existing["display_name"], stable_id),
                )
                if not display:
                    display = str(existing["display_name"] or "").strip()
                previous_target = self._load_target(existing["target_json"])
                if previous_target and not payload:
                    payload = previous_target
            connection.execute(
                f"""
                INSERT INTO {RECIPIENT_ROUTES_TABLE} (
                    recipient_key, channel, user_id, display_name,
                    target_json, aliases_json, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recipient_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    target_json = excluded.target_json,
                    aliases_json = excluded.aliases_json,
                    last_seen = excluded.last_seen
                """,
                (
                    key,
                    channel_name,
                    stable_id,
                    display,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(merged_aliases, ensure_ascii=False),
                    seen_at,
                ),
            )
            connection.commit()
        return self.get(key)

    def get(self, recipient_key: str) -> Optional[RecipientRoute]:
        key = str(recipient_key or "").strip()
        if not key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {RECIPIENT_ROUTES_TABLE} WHERE recipient_key = ?",
                (key,),
            ).fetchone()
        return self._row_to_route(row) if row is not None else None

    def resolve(self, hint: Any, *, channel: Optional[str] = None) -> Optional[RecipientRoute]:
        """Resolve an exact recipient_key, user_id, or stored alias."""
        raw = str(hint or "").strip()
        if not raw:
            return None
        direct = self.get(raw)
        if direct is not None:
            return direct
        lowered = raw.lower()
        scoped_channel = str(channel or "").strip().lower()
        with self._connect() as connection:
            rows = connection.execute(f"SELECT * FROM {RECIPIENT_ROUTES_TABLE}").fetchall()
        matches: list[RecipientRoute] = []
        for row in rows:
            route = self._row_to_route(row)
            if scoped_channel and route.channel.strip().lower() != scoped_channel:
                continue
            tokens = {
                route.recipient_key.lower(),
                route.user_id.lower(),
                route.display_name.lower(),
                *[alias.lower() for alias in route.aliases],
            }
            tokens.discard("")
            if lowered in tokens:
                matches.append(route)
        if len(matches) == 1:
            return matches[0]
        return None

    def list_routes(self) -> List[RecipientRoute]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {RECIPIENT_ROUTES_TABLE} ORDER BY last_seen DESC"
            ).fetchall()
        return [self._row_to_route(row) for row in rows]

    def import_legacy_contacts(self, contacts_file: Path | str) -> int:
        """Idempotent import of verifiable direct sessions from contacts.json.

        Old JSON is never rewritten or deleted. Re-running is a no-op for
        already-imported keys and still UPSERTs any newly-valid entries.
        """
        path = Path(contacts_file)
        imported = 0
        entries = self._load_legacy_contacts(path)
        for item in entries:
            route = self._legacy_entry_to_route(item)
            if route is None:
                continue
            stored = self.upsert(
                channel=route.channel,
                user_id=route.user_id,
                display_name=route.display_name,
                target=route.target,
                aliases=route.aliases,
                last_seen=route.last_seen or None,
            )
            if stored is not None:
                imported += 1
        self._set_meta(
            LEGACY_IMPORT_META_KEY,
            json.dumps(
                {
                    "source": str(path),
                    "imported": imported,
                    "at": _now_iso(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        return imported

    def legacy_import_recorded(self) -> bool:
        return self._get_meta(LEGACY_IMPORT_META_KEY) is not None

    def _set_meta(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {DIRECTORY_META_TABLE} (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            connection.commit()

    def _get_meta(self, key: str) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT value FROM {DIRECTORY_META_TABLE} WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["value"])

    @staticmethod
    def _load_legacy_contacts(path: Path) -> List[Dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, dict):
            return []
        entries = raw.get("contacts")
        if not isinstance(entries, list):
            return []
        return [item for item in entries if isinstance(item, dict)]

    @classmethod
    def _legacy_entry_to_route(cls, item: Dict[str, Any]) -> Optional[RecipientRoute]:
        channel = str(item.get("channel") or "").strip()
        if not channel:
            return None
        target = sanitize_target(item.get("target") if isinstance(item.get("target"), dict) else {})
        if _looks_like_group_target(target):
            return None
        raw_user_id = str(item.get("user_id") or "").strip()
        display_name = str(
            target.get("sender_name")
            or target.get("display_name")
            or ""
        ).strip()
        aliases: list[str] = []
        if channel.lower() == "feishu":
            stable_id = _feishu_stable_user_id(raw_user_id, target)
            if not stable_id:
                return None
            if raw_user_id and raw_user_id != stable_id:
                aliases.append(raw_user_id)
            if display_name:
                aliases.append(display_name)
            user_id = stable_id
            if not display_name and raw_user_id != stable_id:
                display_name = raw_user_id
        else:
            user_id = raw_user_id or str(target.get("user_id") or "").strip()
            if not user_id:
                return None
            if display_name:
                aliases.append(display_name)
        if not user_id:
            return None
        return RecipientRoute(
            channel=channel,
            user_id=user_id,
            display_name=display_name or user_id,
            target=target,
            aliases=cls._normalize_aliases(aliases, extra=(display_name, user_id, raw_user_id)),
            last_seen=str(item.get("last_seen") or "").strip(),
        )

    @staticmethod
    def _row_to_route(row: sqlite3.Row) -> RecipientRoute:
        return RecipientRoute(
            channel=str(row["channel"] or ""),
            user_id=str(row["user_id"] or ""),
            display_name=str(row["display_name"] or ""),
            target=RecipientDirectory._load_target(row["target_json"]),
            aliases=RecipientDirectory._load_aliases(row["aliases_json"]),
            last_seen=str(row["last_seen"] or ""),
        )

    @staticmethod
    def _load_target(raw: str) -> Dict[str, Any]:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _load_aliases(raw: str) -> List[str]:
        try:
            payload = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return RecipientDirectory._normalize_aliases(str(item) for item in payload if item is not None)

    @staticmethod
    def _normalize_aliases(
        aliases: Optional[Iterable[str]],
        extra: Iterable[str] = (),
    ) -> List[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in [*(aliases or []), *extra]:
            token = str(value or "").strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(token)
        return result


def resolve_legacy_contacts_path(workspace: Path) -> Path:
    """Path of the ignored legacy contacts.json (never written by new code)."""
    return Path(workspace).expanduser().resolve() / LEGACY_CONTACTS_FILENAME
