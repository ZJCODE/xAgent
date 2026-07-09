"""QR authentication session management for web channel setup."""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..cli.setup import _register_feishu_app_via_qr


@dataclass
class ChannelQrSession:
    """In-memory QR auth session for Feishu or Weixin channel setup."""

    id: str
    channel: str
    status: str = "pending"
    qr_url: Optional[str] = None
    expire_in: Optional[int] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    _cancel_event: Optional[threading.Event] = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "channel": self.channel,
            "status": self.status,
            "qr_url": self.qr_url,
            "expire_in": self.expire_in,
            "result": self.result,
            "error": self.error,
        }


class ChannelQrSessionManager:
    """Tracks short-lived QR authentication sessions for the web client."""

    SESSION_TTL_SECONDS = 300

    def __init__(self) -> None:
        self._sessions: Dict[str, ChannelQrSession] = {}
        self._lock = threading.Lock()

    def _cleanup_expired(self) -> None:
        cutoff = time.monotonic() - self.SESSION_TTL_SECONDS
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.created_at < cutoff
        ]
        for session_id in expired:
            session = self._sessions.pop(session_id, None)
            if session and session._cancel_event is not None:
                session._cancel_event.set()

    def get(self, session_id: str) -> Optional[ChannelQrSession]:
        with self._lock:
            self._cleanup_expired()
            return self._sessions.get(session_id)

    def cancel(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            if session._cancel_event is not None:
                session._cancel_event.set()
            session.status = "cancelled"
            return True

    def start_feishu(self) -> ChannelQrSession:
        cancel_event = threading.Event()
        session = ChannelQrSession(
            id=secrets.token_urlsafe(16),
            channel="feishu",
            _cancel_event=cancel_event,
        )
        with self._lock:
            self._cleanup_expired()
            self._sessions[session.id] = session

        thread = threading.Thread(
            target=self._run_feishu_registration,
            args=(session, cancel_event),
            daemon=True,
        )
        thread.start()
        return session

    def _run_feishu_registration(
        self,
        session: ChannelQrSession,
        cancel_event: threading.Event,
    ) -> None:
        def on_qr_update(url: str, expire_in: Optional[int]) -> None:
            session.qr_url = url
            session.expire_in = expire_in
            session.status = "waiting"

        def on_status(status: str) -> None:
            if status == "cancelled":
                session.status = "cancelled"
                session.error = "Registration cancelled."
            elif status == "denied":
                session.status = "error"
                session.error = "Authorization was denied."
            elif status == "expired":
                session.status = "expired"
                session.error = "The authorization request expired."
            elif status.startswith("error"):
                session.status = "error"
                session.error = status.split(":", 1)[-1].strip() or "Registration failed."

        credentials = _register_feishu_app_via_qr(
            on_qr_update=on_qr_update,
            on_status=on_status,
            cancel_event=cancel_event,
            source="xagent-web",
        )
        if credentials is None:
            if session.status in {"pending", "waiting"}:
                session.status = "error"
                session.error = session.error or "Feishu registration did not complete."
            return

        app_id, app_secret = credentials
        session.status = "confirmed"
        session.result = {
            "app_id": app_id,
            "app_secret": app_secret,
            "credential_mode": "one_click",
        }

    def start_weixin(self, *, config_dir: Path) -> ChannelQrSession:
        cancel_event = threading.Event()
        session = ChannelQrSession(
            id=secrets.token_urlsafe(16),
            channel="weixin",
            _cancel_event=cancel_event,
        )
        with self._lock:
            self._cleanup_expired()
            self._sessions[session.id] = session

        thread = threading.Thread(
            target=self._run_weixin_login,
            args=(session, config_dir, cancel_event),
            daemon=True,
        )
        thread.start()
        return session

    def _run_weixin_login(
        self,
        session: ChannelQrSession,
        config_dir: Path,
        cancel_event: threading.Event,
    ) -> None:
        del config_dir
        from xagent.integrations.weixin.client import QrLoginCancelled, qr_login
        from xagent.integrations.weixin.config import ILINK_BASE_URL, WEIXIN_CDN_BASE_URL

        if cancel_event.is_set():
            session.status = "cancelled"
            return

        def render_qr(url: str) -> None:
            session.qr_url = url
            session.status = "waiting"

        def log(message: str) -> None:
            if "QR scanned" in message:
                session.status = "scanned"
            elif "expired" in message.lower():
                session.status = "expired"

        try:
            credentials = asyncio.run(
                qr_login(
                    base_url=ILINK_BASE_URL,
                    log=log,
                    render_qr_url=render_qr,
                    cancel_event=cancel_event,
                )
            )
        except QrLoginCancelled:
            session.status = "cancelled"
            session.error = "Weixin login cancelled."
            return
        except Exception as exc:
            if cancel_event.is_set():
                session.status = "cancelled"
                session.error = "Weixin login cancelled."
            else:
                session.status = "error"
                session.error = str(exc)
            return

        if cancel_event.is_set():
            session.status = "cancelled"
            return

        session.status = "confirmed"
        session.result = {
            "account_id": credentials.account_id,
            "owner_user_id": credentials.user_id,
            "base_url": credentials.base_url or ILINK_BASE_URL,
            "cdn_base_url": WEIXIN_CDN_BASE_URL,
            "credentials": {
                "token": credentials.token,
                "base_url": credentials.base_url,
                "account_id": credentials.account_id,
                "user_id": credentials.user_id,
                "saved_at": credentials.saved_at,
            },
        }


_qr_session_manager = ChannelQrSessionManager()


def get_qr_session_manager() -> ChannelQrSessionManager:
    return _qr_session_manager
