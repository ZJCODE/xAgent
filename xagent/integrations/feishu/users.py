"""Resolve Feishu user IDs through the official contact API."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional


FEISHU_USER_FALLBACK_NAME = "Feishu User"


def infer_user_id_type(user_id: str) -> str:
    """Infer the Feishu ``user_id_type`` from the visible ID prefix."""
    normalized = (user_id or "").strip()
    if normalized.startswith("ou_"):
        return "open_id"
    if normalized.startswith("on_"):
        return "union_id"
    return "user_id"


def safe_display_name(value: Any) -> Optional[str]:
    """Return a non-ID-looking display name, or ``None`` when unsafe."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.startswith(("ou_", "on_", "cli_")):
        return None
    return normalized


class FeishuUserResolver:
    """Map Feishu user IDs to display names via ``contact.v3.user.get``."""

    def __init__(self, channel: Any, logger: Optional[logging.Logger] = None) -> None:
        self._channel = channel
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._name_cache: dict[str, str] = {}
        self._logged_missing_sdk = False

    async def resolve_name(self, user_id: str, fallback: Optional[str] = None) -> Optional[str]:
        """Return the official display name for a Feishu user ID when available."""
        normalized_id = (user_id or "").strip()
        fallback_name = safe_display_name(fallback)
        if not normalized_id:
            return fallback_name
        if normalized_id.startswith("cli_"):
            return fallback_name

        cached_name = self._name_cache.get(normalized_id)
        if cached_name:
            return cached_name

        client = getattr(self._channel, "client", None)
        if client is None:
            return fallback_name

        resolved_name = await self._fetch_user_name(client, normalized_id)
        if resolved_name:
            self._name_cache[normalized_id] = resolved_name
            return resolved_name
        return fallback_name

    async def _fetch_user_name(self, client: Any, user_id: str) -> Optional[str]:
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest  # type: ignore
        except ImportError:  # pragma: no cover - import guard
            if not self._logged_missing_sdk:
                self._logger.debug("lark-oapi is unavailable; cannot resolve Feishu user names")
                self._logged_missing_sdk = True
            return None

        request = (
            GetUserRequest.builder()
            .user_id(user_id)
            .user_id_type(infer_user_id_type(user_id))
            .department_id_type("open_department_id")
            .build()
        )

        try:
            response = await asyncio.to_thread(client.contact.v3.user.get, request)
        except Exception as exc:
            self._logger.info(
                "Feishu get user failed (user_id_type=%s): %s",
                infer_user_id_type(user_id),
                exc,
            )
            return None

        if not response.success():
            self._logger.info(
                "Feishu get user rejected: code=%s msg=%s log_id=%s",
                getattr(response, "code", None),
                getattr(response, "msg", None),
                response.get_log_id() if hasattr(response, "get_log_id") else None,
            )
            return None

        user = response.data.user
        for field_name in ("name", "nickname", "en_name"):
            name = safe_display_name(getattr(user, field_name, None))
            if name:
                return name
        return None
