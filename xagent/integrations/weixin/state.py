"""Disk-backed state for the Weixin iLink adapter."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class WeixinCredentials:
    token: str
    base_url: str
    account_id: str
    user_id: str
    saved_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeixinCredentials":
        token = str(data.get("token") or data.get("bot_token") or "").strip()
        base_url = str(data.get("base_url") or data.get("baseUrl") or "").strip().rstrip("/")
        account_id = str(data.get("account_id") or data.get("accountId") or data.get("ilink_bot_id") or "").strip()
        user_id = str(data.get("user_id") or data.get("userId") or data.get("ilink_user_id") or "").strip()
        saved_at = str(data.get("saved_at") or data.get("savedAt") or "").strip()
        if not token or not base_url or not account_id or not user_id:
            raise ValueError("Invalid Weixin credential payload")
        return cls(token=token, base_url=base_url, account_id=account_id, user_id=user_id, saved_at=saved_at)


class WeixinStateStore:
    """State files rooted inside an xAgent runtime directory."""

    def __init__(self, runtime_dir: str | Path, *, create: bool = True) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.root = self.runtime_dir / "weixin"
        self.accounts_dir = self.root / "accounts"
        if create:
            self.accounts_dir.mkdir(parents=True, exist_ok=True)

    def credential_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{_safe_account_id(account_id)}.json"

    def sync_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{_safe_account_id(account_id)}.sync.json"

    def context_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{_safe_account_id(account_id)}.context-tokens.json"

    def save_credentials(self, credentials: WeixinCredentials) -> None:
        saved_at = credentials.saved_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {**asdict(credentials), "saved_at": saved_at}
        _atomic_write_json(self.credential_path(credentials.account_id), payload, mode=0o600)

    def load_credentials(self, account_id: str) -> Optional[WeixinCredentials]:
        payload = _load_json(self.credential_path(account_id))
        if not isinstance(payload, dict):
            return None
        try:
            return WeixinCredentials.from_dict(payload)
        except ValueError:
            return None

    def delete_credentials(self, account_id: str) -> None:
        _unlink_if_exists(self.credential_path(account_id))

    def load_sync_buf(self, account_id: str) -> str:
        if (self.root / "inbox.sqlite3").exists():
            with self.inbox() as connection:
                row = connection.execute("SELECT cursor FROM cursors WHERE account=?", (account_id,)).fetchone()
                if row is not None:
                    return row[0]
        payload = _load_json(self.sync_path(account_id))
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("get_updates_buf") or "")

    def save_sync_buf(self, account_id: str, sync_buf: str) -> None:
        self.commit_poll(account_id, sync_buf, [])

    def clear_sync_buf(self, account_id: str) -> None:
        with self.inbox() as connection:
            connection.execute("DELETE FROM cursors WHERE account=?", (account_id,))
        _unlink_if_exists(self.sync_path(account_id))

    @contextmanager
    def inbox(self):
        """Persist receipt and poll progress together, before acknowledging either."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "inbox.sqlite3"
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(path, timeout=10)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS cursors(account TEXT PRIMARY KEY, cursor TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS messages(account TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL, error TEXT, PRIMARY KEY(account,id))")
            with connection:
                yield connection
        finally:
            connection.close()

    def commit_poll(self, account_id, cursor, messages):
        with self.inbox() as connection:
            for message in messages:
                if not isinstance(message, dict):
                    continue
                encoded = json.dumps(message, sort_keys=True, ensure_ascii=False)
                key = str(message.get("message_id") or message.get("client_id") or hashlib.sha256(encoded.encode()).hexdigest())
                connection.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,'queued',NULL)", (account_id, key, encoded))
            if cursor:
                connection.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (account_id, cursor))

    def pending_inbound(self, account_id, limit=32):
        with self.inbox() as connection:
            return [(row[0], json.loads(row[1])) for row in connection.execute(
                "SELECT id,data FROM messages WHERE account=? AND status='queued' ORDER BY rowid LIMIT ?", (account_id, limit))]

    def claim_inbound(self, account_id, key):
        with self.inbox() as connection:
            return connection.execute("UPDATE messages SET status='processing' WHERE account=? AND id=? AND status='queued'", (account_id, key)).rowcount == 1

    def finish_inbound(self, account_id, key, error=None, *, only_processing=False):
        with self.inbox() as connection:
            if only_processing:
                connection.execute("UPDATE messages SET status='needs_review',error=? WHERE account=? AND id=? AND status='processing'", (error, account_id, key))
            else:
                connection.execute("UPDATE messages SET status=?,error=?,data=CASE WHEN ? IS NULL THEN '{}' ELSE data END WHERE account=? AND id=?",
                                   ("needs_review" if error else "done", error, error, account_id, key))

    def recover_inbound(self, account_id):
        # A processing turn may already have executed external tools. Never replay it.
        with self.inbox() as connection:
            return connection.execute("UPDATE messages SET status='needs_review',error='Runtime interrupted during processing' WHERE account=? AND status='processing'", (account_id,)).rowcount

    def inbound_status(self, account_id):
        with self.inbox() as connection:
            return dict(connection.execute("SELECT status,count(*) FROM messages WHERE account=? GROUP BY status", (account_id,)))

    def load_context_tokens(self, account_id: str) -> dict[str, str]:
        payload = _load_json(self.context_path(account_id))
        if not isinstance(payload, dict):
            return {}
        tokens: dict[str, str] = {}
        for user_id, token in payload.items():
            user_text = str(user_id or "").strip()
            token_text = str(token or "").strip()
            if user_text and token_text:
                tokens[user_text] = token_text
        return tokens

    def save_context_tokens(self, account_id: str, tokens: dict[str, str]) -> None:
        payload = {str(user): str(token) for user, token in tokens.items() if str(user) and str(token)}
        _atomic_write_json(self.context_path(account_id), payload, mode=0o600)

    def clear_context_tokens(self, account_id: str) -> None:
        _unlink_if_exists(self.context_path(account_id))


def _safe_account_id(account_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", ".", "@"} else "_" for ch in str(account_id)) or "default"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        tmp_path.chmod(mode)
    except OSError:
        pass
    tmp_path.replace(path)
    try:
        path.chmod(mode)
    except OSError:
        pass


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
