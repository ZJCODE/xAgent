"""Resolve Feishu user IDs through the official contact API."""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Optional


FEISHU_USER_FALLBACK_NAME = "Feishu User"

_APP_ID_TYPES = {"app_id"}
_APP_SENDER_TYPES = {"app"}
_USER_ID_TYPES = {"open_id", "union_id", "user_id"}
_FEISHU_ID_FIELD_PRIORITY = ("open_id", "user_id", "union_id", "app_id")
_NESTED_ID_FIELDS = ("id", "sender_id")


def infer_feishu_id_type(identifier: str) -> str:
    """Infer the Feishu ID type from a visible ID prefix."""
    normalized = (identifier or "").strip()
    if normalized.startswith("cli_"):
        return "app_id"
    if normalized.startswith("ou_"):
        return "open_id"
    if normalized.startswith("on_"):
        return "union_id"
    return "user_id"


def infer_user_id_type(user_id: str) -> str:
    """Infer the Feishu ``user_id_type`` from the visible ID prefix."""
    inferred = infer_feishu_id_type(user_id)
    return inferred if inferred in _USER_ID_TYPES else "user_id"


def extract_feishu_id(identity: Any) -> tuple[Optional[str], Optional[str]]:
    """Return ``(id, id_type)`` from Feishu string, dict, or SDK identity objects."""
    direct_value = _clean_string(identity)
    if direct_value:
        return direct_value, infer_feishu_id_type(direct_value)

    explicit_id_type = _clean_id_type(
        _read_field(identity, "id_type")
        or _read_field(identity, "sender_id_type")
        or _read_field(identity, "user_id_type")
    )

    for field_name in _FEISHU_ID_FIELD_PRIORITY:
        field_value = _clean_string(_read_field(identity, field_name))
        if field_value:
            return field_value, field_name

    for field_name in _NESTED_ID_FIELDS:
        nested_value = _read_field(identity, field_name)
        if nested_value is None:
            continue
        nested_direct = _clean_string(nested_value)
        if nested_direct:
            return nested_direct, explicit_id_type or infer_feishu_id_type(nested_direct)
        nested_id, nested_id_type = extract_feishu_id(nested_value)
        if nested_id:
            return nested_id, explicit_id_type or nested_id_type

    return None, explicit_id_type


def normalize_user_id_type(user_id: str, id_type: Optional[str] = None) -> str:
    """Return a Contact API ``user_id_type`` for a Feishu user sender."""
    normalized_id_type = (id_type or "").strip().lower()
    if normalized_id_type in _USER_ID_TYPES:
        return normalized_id_type
    return infer_user_id_type(user_id)


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
    """Map Feishu sender IDs to display names through official Open APIs."""

    def __init__(self, channel: Any, logger: Optional[logging.Logger] = None) -> None:
        self._channel = channel
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._name_cache: dict[str, str] = {}
        self._logged_missing_sdk = False

    async def resolve_name(
        self,
        user_id: str,
        fallback: Optional[str] = None,
        *,
        id_type: Optional[str] = None,
        sender_type: Optional[str] = None,
    ) -> Optional[str]:
        """Return the official display name for a Feishu sender when available."""
        normalized_id = (user_id or "").strip()
        normalized_id_type = (id_type or "").strip().lower()
        normalized_sender_type = (sender_type or "").strip().lower()
        fallback_name = safe_display_name(fallback)
        if not normalized_id:
            return fallback_name

        if self._is_app_sender(normalized_id, normalized_id_type, normalized_sender_type):
            return await self._resolve_app_name(normalized_id, fallback_name)

        cache_key = f"user:{normalize_user_id_type(normalized_id, normalized_id_type)}:{normalized_id}"
        cached_name = self._name_cache.get(cache_key)
        if cached_name:
            return cached_name

        client = getattr(self._channel, "client", None)
        if client is None:
            return fallback_name

        resolved_name = await self._fetch_user_name(
            client,
            normalized_id,
            user_id_type=normalize_user_id_type(normalized_id, normalized_id_type),
        )
        if resolved_name:
            self._name_cache[cache_key] = resolved_name
            return resolved_name
        return fallback_name

    @staticmethod
    def _is_app_sender(user_id: str, id_type: str, sender_type: str) -> bool:
        return sender_type in _APP_SENDER_TYPES or id_type in _APP_ID_TYPES or user_id.startswith("cli_")

    async def _resolve_app_name(self, app_id: str, fallback_name: Optional[str]) -> Optional[str]:
        cache_key = f"app:{app_id}"
        cached_name = self._name_cache.get(cache_key)
        if cached_name:
            return cached_name

        client = getattr(self._channel, "client", None)
        if client is None:
            return fallback_name

        resolved_name = await self._fetch_app_name(client, app_id)
        if resolved_name:
            self._name_cache[cache_key] = resolved_name
            return resolved_name
        return fallback_name

    async def _fetch_user_name(self, client: Any, user_id: str, *, user_id_type: str) -> Optional[str]:
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
            .user_id_type(user_id_type)
            .department_id_type("open_department_id")
            .build()
        )

        try:
            response = await asyncio.to_thread(client.contact.v3.user.get, request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            self._logger.info(
                "Feishu get user failed (user_id_type=%s): %s",
                user_id_type,
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

    async def _fetch_app_name(self, client: Any, app_id: str) -> Optional[str]:
        try:
            from lark_oapi.api.application.v6 import GetApplicationRequest  # type: ignore
        except ImportError:  # pragma: no cover - import guard
            if not self._logged_missing_sdk:
                self._logger.debug("lark-oapi is unavailable; cannot resolve Feishu app names")
                self._logged_missing_sdk = True
            return None

        request = (
            GetApplicationRequest.builder()
            .app_id(app_id)
            .lang("zh_cn")
            .user_id_type("open_id")
            .build()
        )

        try:
            response = await asyncio.to_thread(client.application.v6.application.get, request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            self._logger.info("Feishu get application failed (app_id=%s): %s", app_id, exc)
            return None

        if not response.success():
            self._logger.info(
                "Feishu get application rejected: code=%s msg=%s log_id=%s",
                getattr(response, "code", None),
                getattr(response, "msg", None),
                response.get_log_id() if hasattr(response, "get_log_id") else None,
            )
            return None

        data = _read_field(response, "data")
        app = _read_field(data, "app") or data
        for name in _iter_app_name_candidates(app):
            display_name = safe_display_name(name)
            if display_name:
                return display_name
        return None


def _iter_app_name_candidates(app: Any):
    yield _read_field(app, "app_name")
    yield _read_field(app, "name")
    i18n = _read_field(app, "i18n")
    if isinstance(i18n, list):
        for item in i18n:
            yield _read_field(item, "name")
    else:
        yield _read_field(i18n, "name")


def _read_field(obj: Any, field_name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(field_name)
    return getattr(obj, field_name, None)


def _clean_string(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _clean_id_type(value: Any) -> Optional[str]:
    normalized = _clean_string(value)
    return normalized.lower() if normalized else None
