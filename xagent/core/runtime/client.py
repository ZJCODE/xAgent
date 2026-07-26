"""Authenticated client for an agent's loopback control service."""
from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


class RuntimeUnavailable(RuntimeError):
    pass


class RuntimeIdentityError(RuntimeError):
    pass


class RuntimeClient:
    def __init__(self, config_dir: str | Path) -> None:
        self.config_dir = Path(config_dir).expanduser().resolve()
        self.info_path = self.config_dir / "run" / "runtime.json"

    def info(self) -> dict[str, Any]:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow and self.info_path.is_symlink():
            raise RuntimeIdentityError("runtime.json must not be a symbolic link")
        descriptor = -1
        try:
            descriptor = os.open(
                self.info_path,
                os.O_RDONLY | no_follow,
            )
        except FileNotFoundError as exc:
            raise RuntimeUnavailable("xAgent runtime is not running") from exc
        except OSError as exc:
            raise RuntimeIdentityError(f"cannot open runtime.json: {exc}") from exc
        try:
            file_status = os.fstat(descriptor)
            if not stat.S_ISREG(file_status.st_mode):
                raise RuntimeIdentityError("runtime.json is not a regular file")
            if file_status.st_size > 16 * 1024:
                raise RuntimeIdentityError("runtime.json exceeds 16 KiB")
            if os.name != "nt" and stat.S_IMODE(file_status.st_mode) & 0o077:
                raise RuntimeIdentityError("runtime.json permissions must be 0600")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                info = json.load(handle)
        except RuntimeIdentityError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeIdentityError(f"runtime.json is invalid: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(info, dict):
            raise RuntimeIdentityError("runtime.json must contain an object")
        url = info.get("control_url")
        token = info.get("token")
        pid = info.get("pid")
        instance_id = info.get("instance_id")
        started_at = info.get("started_at")
        if not isinstance(url, str):
            raise RuntimeIdentityError("runtime.json is invalid")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise RuntimeIdentityError("runtime.json contains an invalid control URL") from exc
        valid_url = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and port is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
        if (
            not valid_url
            or not isinstance(token, str)
            or not 32 <= len(token) <= 512
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(instance_id, str)
            or not 1 <= len(instance_id) <= 128
            or not isinstance(started_at, (int, float))
            or isinstance(started_at, bool)
            or not math.isfinite(float(started_at))
            or started_at <= 0
        ):
            raise RuntimeIdentityError("runtime.json is invalid")
        return info

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        info = self.info()
        return self._request(info, method, path, **kwargs)

    def status(self, *, timeout_seconds: float = 2.0) -> dict[str, Any]:
        info = self.info()
        status = dict(
            self._request(
                info,
                "GET",
                "/v1/runtime",
                timeout=max(0.1, float(timeout_seconds)),
            )
        )
        if (
            status.get("pid") != info["pid"]
            or status.get("instance_id") != info.get("instance_id")
        ):
            raise RuntimeIdentityError("runtime control identity mismatch")
        return status

    @staticmethod
    def _request(
        info: dict[str, Any],
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {info['token']}"
        try:
            response = httpx.request(
                method,
                f"{info['control_url']}{path}",
                headers=headers,
                timeout=kwargs.pop("timeout", 610.0),
                trust_env=False,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise RuntimeUnavailable(f"runtime control request failed: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text
            raise RuntimeError(str(detail or f"HTTP {response.status_code}"))
        return response.json()
