"""CLI and local Web access to the one agent runtime over private HTTP."""
from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator, Any

import httpx

from .ownership import runtime_paths


class RuntimeUnavailable(RuntimeError):
    pass


class RuntimeClient:
    def __init__(self, root: str | Path):
        self.paths = runtime_paths(root)

    def _client(self, *, timeout: float | None = 10.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.paths.socket_path)),
            base_url="http://xagent.local",
            timeout=timeout,
            trust_env=False,
        )

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
        except httpx.TransportError as exc:
            raise RuntimeUnavailable(f"Agent runtime is unavailable: {self.paths.root}") from exc

    async def status(self) -> dict:
        return (await self.request("GET", "/runtime/status")).json()

    async def stop(self) -> dict:
        return (await self.request("POST", "/runtime/stop")).json()

    async def abort(self, *, turn_id: str, channel: str | None = None) -> dict:
        return (await self.request("POST", "/runtime/turns/stop", json={
            "turn_id": turn_id, "channel": channel,
        })).json()

    async def observe(self, **kwargs) -> dict:
        return (await self.request("POST", "/runtime/observe", json=kwargs)).json()

    async def chat_events(self, **kwargs: Any) -> AsyncIterator[dict]:
        kwargs.setdefault("channel", "cli")
        try:
            async with self._client(timeout=None) as client:
                async with client.stream("POST", "/runtime/chat", json=kwargs) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.strip():
                            yield json.loads(line)
        except httpx.TransportError as exc:
            raise RuntimeUnavailable(f"Agent runtime disconnected: {self.paths.root}") from exc
