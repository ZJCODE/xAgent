"""SQLite-backed transport state for the Weixin adapter."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from ...core.database import initialize_state_database


@dataclass(frozen=True)
class WeixinCredentials:
    token: str
    base_url: str
    account_id: str
    user_id: str
    saved_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeixinCredentials":
        token = str(data.get("token") or "").strip()
        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        account_id = str(data.get("account_id") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        saved_at = str(data.get("saved_at") or "").strip()
        if not token or not base_url or not account_id or not user_id:
            raise ValueError("Invalid Weixin credential payload")
        return cls(
            token=token,
            base_url=base_url,
            account_id=account_id,
            user_id=user_id,
            saved_at=saved_at,
        )


class WeixinStateStore:
    """Keep all adapter state in the Agent's single SQLite database."""

    CHANNEL = "weixin"

    def __init__(self, runtime_dir: str | Path) -> None:
        root = Path(runtime_dir).expanduser().resolve()
        self.path = initialize_state_database(root / "state.sqlite3")

    def save_credentials(self, credentials: WeixinCredentials) -> None:
        saved_at = credentials.saved_at or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        )
        self._set(
            self._key("credentials", credentials.account_id),
            {**asdict(credentials), "saved_at": saved_at},
        )

    def load_credentials(self, account_id: str) -> Optional[WeixinCredentials]:
        payload = self._get(self._key("credentials", account_id))
        if not isinstance(payload, dict):
            return None
        try:
            return WeixinCredentials.from_dict(payload)
        except ValueError:
            return None

    def delete_credentials(self, account_id: str) -> None:
        self._delete(self._key("credentials", account_id))

    def load_sync_buf(self, account_id: str) -> str:
        payload = self._get(self._key("sync", account_id))
        return str(payload.get("get_updates_buf") or "") if isinstance(payload, dict) else ""

    def save_sync_buf(self, account_id: str, sync_buf: str) -> None:
        self._set(self._key("sync", account_id), {"get_updates_buf": sync_buf})

    def clear_sync_buf(self, account_id: str) -> None:
        self._delete(self._key("sync", account_id))

    def load_context_tokens(self, account_id: str) -> dict[str, str]:
        payload = self._get(self._key("context_tokens", account_id))
        if not isinstance(payload, dict):
            return {}
        return {
            str(user): str(token)
            for user, token in payload.items()
            if str(user) and str(token)
        }

    def save_context_tokens(self, account_id: str, tokens: dict[str, str]) -> None:
        self._set(
            self._key("context_tokens", account_id),
            {
                str(user): str(token)
                for user, token in tokens.items()
                if str(user) and str(token)
            },
        )

    def clear_context_tokens(self, account_id: str) -> None:
        self._delete(self._key("context_tokens", account_id))

    def save_last_active_user(
        self,
        *,
        account_id: str,
        user_id: str,
        context_token: str = "",
    ) -> None:
        self._set(
            "last_active",
            {
                "account_id": account_id,
                "user_id": user_id,
                "context_token": context_token,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )

    def load_last_active_user(self) -> dict[str, str]:
        payload = self._get("last_active")
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in payload.items()
            if value is not None
        }

    @staticmethod
    def _key(kind: str, account_id: str) -> str:
        normalized = str(account_id or "").strip()
        if not normalized:
            raise ValueError("account_id is required")
        return f"{kind}:{normalized}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _get(self, key: str) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM channel_state WHERE channel=? AND state_key=?",
                (self.CHANNEL, key),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return None

    def _set(self, key: str, value: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO channel_state(channel, state_key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel, state_key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (
                    self.CHANNEL,
                    key,
                    json.dumps(value, ensure_ascii=False),
                    time.time(),
                ),
            )
            connection.commit()

    def _delete(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM channel_state WHERE channel=? AND state_key=?",
                (self.CHANNEL, key),
            )
            connection.commit()
