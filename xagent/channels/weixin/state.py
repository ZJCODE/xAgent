"""Disk-backed state for the Weixin iLink adapter."""
from __future__ import annotations

import json
import os
import time
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

    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.root = self.runtime_dir / "weixin"
        self.accounts_dir = self.root / "accounts"
        self.accounts_dir.mkdir(parents=True, exist_ok=True)

    def credential_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{_safe_account_id(account_id)}.json"

    def sync_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{_safe_account_id(account_id)}.sync.json"

    def context_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{_safe_account_id(account_id)}.context-tokens.json"

    def last_active_path(self) -> Path:
        return self.root / "state.json"

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
        payload = _load_json(self.sync_path(account_id))
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("get_updates_buf") or "")

    def save_sync_buf(self, account_id: str, sync_buf: str) -> None:
        _atomic_write_json(self.sync_path(account_id), {"get_updates_buf": sync_buf}, mode=0o600)

    def clear_sync_buf(self, account_id: str) -> None:
        _unlink_if_exists(self.sync_path(account_id))

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

    def save_last_active_user(self, *, account_id: str, user_id: str, context_token: str = "") -> None:
        payload = {
            "account_id": account_id,
            "user_id": user_id,
            "context_token": context_token,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _atomic_write_json(self.last_active_path(), payload, mode=0o600)

    def load_last_active_user(self) -> dict[str, str]:
        payload = _load_json(self.last_active_path())
        if not isinstance(payload, dict):
            return {}
        return {key: str(value) for key, value in payload.items() if value is not None}


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
