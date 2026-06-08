"""Async HTTP client for Tencent Weixin iLink Bot API."""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx

from .config import ILINK_BASE_URL, WEIXIN_CDN_BASE_URL
from .state import WeixinCredentials


ILINK_APP_CLIENT_VERSION = "1"
AUTHORIZATION_TYPE = "ilink_bot_token"
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2


class WeixinApiError(RuntimeError):
    """Raised for HTTP or iLink business errors."""

    def __init__(self, message: str, *, status_code: int = 0, code: Optional[int] = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload

    @property
    def is_session_expired(self) -> bool:
        return self.code == SESSION_EXPIRED_ERRCODE


class WeixinSessionExpired(WeixinApiError):
    """Raised when iLink reports the bot token/session is expired."""


@dataclass(frozen=True)
class QrCodePayload:
    qrcode: str
    qrcode_url: str


class WeixinClient:
    """Small iLink API wrapper with injectable ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        base_url: str = ILINK_BASE_URL,
        token: str = "",
        channel_version: str = "1.0.0",
        cdn_base_url: str = WEIXIN_CDN_BASE_URL,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.channel_version = channel_version or "1.0.0"
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self._client = http_client or httpx.AsyncClient(trust_env=True)
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "WeixinClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def with_credentials(self, credentials: WeixinCredentials) -> "WeixinClient":
        self.base_url = credentials.base_url.rstrip("/")
        self.token = credentials.token
        return self

    async def get_bot_qrcode(self, *, bot_type: str = "3", timeout_ms: int = 15_000) -> QrCodePayload:
        payload = await self._get(
            f"ilink/bot/get_bot_qrcode?bot_type={quote(str(bot_type), safe='')}",
            timeout_ms=timeout_ms,
            token="",
        )
        qrcode = str(payload.get("qrcode") or "").strip()
        qrcode_url = str(payload.get("qrcode_img_content") or "").strip()
        if not qrcode:
            raise WeixinApiError("iLink QR response missing qrcode", payload=payload)
        return QrCodePayload(qrcode=qrcode, qrcode_url=qrcode_url)

    async def get_qrcode_status(self, qrcode: str, *, timeout_ms: int = 35_000) -> dict[str, Any]:
        return await self._get(
            f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}",
            timeout_ms=timeout_ms,
            token="",
            extra_headers={"iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION},
        )

    async def get_updates(self, *, sync_buf: str, timeout_ms: int = 35_000) -> dict[str, Any]:
        try:
            return await self._post(
                "ilink/bot/getupdates",
                {"get_updates_buf": sync_buf},
                timeout_ms=timeout_ms,
            )
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}

    async def send_text_message(
        self,
        *,
        to_user_id: str,
        text: str,
        context_token: str,
        client_id: str,
        timeout_ms: int = 15_000,
    ) -> dict[str, Any]:
        if not context_token:
            raise ValueError("context_token is required to send a Weixin message")
        return await self.send_message_item(
            to_user_id=to_user_id,
            item={"type": 1, "text_item": {"text": text}},
            context_token=context_token,
            client_id=client_id,
            timeout_ms=timeout_ms,
        )

    async def send_message_item(
        self,
        *,
        to_user_id: str,
        item: dict[str, Any],
        context_token: str,
        client_id: str,
        timeout_ms: int = 15_000,
    ) -> dict[str, Any]:
        if not context_token:
            raise ValueError("context_token is required to send a Weixin message")
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [item],
            }
        }
        response = await self._post("ilink/bot/sendmessage", payload, timeout_ms=timeout_ms)
        raise_for_api_error(response, "sendmessage")
        return response

    async def get_config(self, *, user_id: str, context_token: str, timeout_ms: int = 10_000) -> dict[str, Any]:
        payload: dict[str, Any] = {"ilink_user_id": user_id}
        if context_token:
            payload["context_token"] = context_token
        response = await self._post("ilink/bot/getconfig", payload, timeout_ms=timeout_ms)
        raise_for_api_error(response, "getconfig")
        return response

    async def send_typing(
        self,
        *,
        user_id: str,
        typing_ticket: str,
        status: int,
        timeout_ms: int = 10_000,
    ) -> dict[str, Any]:
        response = await self._post(
            "ilink/bot/sendtyping",
            {"ilink_user_id": user_id, "typing_ticket": typing_ticket, "status": status},
            timeout_ms=timeout_ms,
        )
        raise_for_api_error(response, "sendtyping")
        return response

    async def get_upload_url(
        self,
        *,
        to_user_id: str,
        media_type: int,
        filekey: str,
        rawsize: int,
        rawfilemd5: str,
        filesize: int,
        aeskey_hex: str,
        timeout_ms: int = 15_000,
    ) -> dict[str, Any]:
        response = await self._post(
            "ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": filesize,
                "no_need_thumb": True,
                "aeskey": aeskey_hex,
            },
            timeout_ms=timeout_ms,
        )
        raise_for_api_error(response, "getuploadurl")
        return response

    async def upload_cdn(self, *, upload_url: str, ciphertext: bytes, timeout_ms: int = 120_000) -> str:
        response = await self._client.post(
            upload_url,
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=timeout_ms / 1000,
        )
        raw = response.text
        if response.status_code < 200 or response.status_code >= 300:
            raise WeixinApiError(f"CDN upload HTTP {response.status_code}: {raw[:200]}", status_code=response.status_code)
        encrypted_param = response.headers.get("x-encrypted-param")
        if not encrypted_param:
            raise WeixinApiError(f"CDN upload missing x-encrypted-param header: {raw[:200]}", status_code=response.status_code)
        return encrypted_param

    async def download_cdn(self, *, encrypted_query_param: str, timeout_ms: int = 120_000) -> bytes:
        url = cdn_download_url(self.cdn_base_url, encrypted_query_param)
        response = await self._client.get(url, timeout=timeout_ms / 1000)
        if response.status_code < 200 or response.status_code >= 300:
            raise WeixinApiError(f"CDN download HTTP {response.status_code}: {response.text[:200]}", status_code=response.status_code)
        return response.content

    async def _get(
        self,
        endpoint: str,
        *,
        timeout_ms: int,
        token: Optional[str] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self._client.get(
            url,
            headers=self._headers(token=token, body="", extra_headers=extra_headers),
            timeout=timeout_ms / 1000,
        )
        return _parse_response(response, endpoint)

    async def _post(self, endpoint: str, payload: dict[str, Any], *, timeout_ms: int) -> dict[str, Any]:
        body = _json_dumps({**payload, "base_info": {"channel_version": self.channel_version}})
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self._client.post(
            url,
            content=body.encode("utf-8"),
            headers=self._headers(token=self.token, body=body),
            timeout=timeout_ms / 1000,
        )
        return _parse_response(response, endpoint)

    def _headers(
        self,
        *,
        token: Optional[str],
        body: str,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": AUTHORIZATION_TYPE,
            "X-WECHAT-UIN": random_wechat_uin(),
        }
        if body:
            headers["Content-Length"] = str(len(body.encode("utf-8")))
        resolved_token = self.token if token is None else token
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"
        if extra_headers:
            headers.update(extra_headers)
        return headers


async def qr_login(
    *,
    base_url: str = ILINK_BASE_URL,
    bot_type: str = "3",
    channel_version: str = "1.0.0",
    timeout_seconds: int = 300,
    log: Optional[Callable[[str], None]] = None,
    render_qr_url: Optional[Callable[[str], None]] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> WeixinCredentials:
    logger = log or (lambda message: None)
    async with WeixinClient(base_url=base_url, channel_version=channel_version, http_client=http_client) as client:
        qr = await client.get_bot_qrcode(bot_type=bot_type)
        _emit_qr(qr.qrcode_url or qr.qrcode, logger, render_qr_url)
        qrcode_value = qr.qrcode
        deadline = time.monotonic() + timeout_seconds
        last_status = ""
        refresh_count = 0

        while time.monotonic() < deadline:
            status_payload = await client.get_qrcode_status(qrcode_value)
            status = str(status_payload.get("status") or "wait")
            if status != last_status:
                if status == "scaned":
                    logger("QR scanned. Confirm the login inside WeChat.")
                elif status == "expired":
                    logger("QR code expired. Requesting a fresh code.")
                elif status == "confirmed":
                    logger("Login confirmed.")
                last_status = status

            if status == "confirmed":
                credentials = WeixinCredentials.from_dict({
                    "token": status_payload.get("bot_token"),
                    "base_url": status_payload.get("baseurl") or base_url,
                    "account_id": status_payload.get("ilink_bot_id"),
                    "user_id": status_payload.get("ilink_user_id"),
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                return credentials

            if status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    raise WeixinApiError("QR code expired multiple times; rerun setup")
                qr = await client.get_bot_qrcode(bot_type=bot_type)
                qrcode_value = qr.qrcode
                _emit_qr(qr.qrcode_url or qr.qrcode, logger, render_qr_url)

            await asyncio.sleep(1.5)

    raise WeixinApiError("Weixin QR login timed out")


def _emit_qr(value: str, log: Callable[[str], None], render_qr_url: Optional[Callable[[str], None]]) -> None:
    log("Scan this QR code with WeChat:")
    if render_qr_url is not None:
        render_qr_url(value)
    else:
        log(value)


def random_wechat_uin() -> str:
    value = int.from_bytes(secrets.token_bytes(4), "big")
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def raise_for_api_error(payload: dict[str, Any], label: str) -> None:
    ret = payload.get("ret")
    errcode = payload.get("errcode")
    code = _error_code(ret, errcode)
    if code is None:
        return
    message = str(payload.get("errmsg") or payload.get("msg") or f"iLink {label} failed")
    exc_cls = WeixinSessionExpired if code == SESSION_EXPIRED_ERRCODE else WeixinApiError
    raise exc_cls(message, code=code, payload=payload)


def _error_code(ret: Any, errcode: Any) -> Optional[int]:
    for value in (errcode, ret):
        if value in (None, 0, "0"):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1
    return None


def _parse_response(response: httpx.Response, label: str) -> dict[str, Any]:
    raw = response.text
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise WeixinApiError(f"iLink {label} returned non-JSON response", status_code=response.status_code) from exc
    if response.status_code < 200 or response.status_code >= 300:
        message = str(payload.get("errmsg") or raw[:200] or f"HTTP {response.status_code}")
        raise WeixinApiError(message, status_code=response.status_code, code=payload.get("errcode"), payload=payload)
    return payload if isinstance(payload, dict) else {}


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
