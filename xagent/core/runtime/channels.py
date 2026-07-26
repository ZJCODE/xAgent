"""Hot-swappable channel supervision inside one agent process."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .types import Delivery


logger = logging.getLogger(__name__)

CHANNEL_STOPPED = "stopped"
CHANNEL_STARTING = "starting"
CHANNEL_RUNNING = "running"
CHANNEL_DEGRADED = "degraded"
CHANNEL_STOPPING = "stopping"


@dataclass(frozen=True)
class ManagedChannel:
    run: Callable[[], Awaitable[None]]
    stop: Callable[[], Awaitable[None] | None]
    send: Callable[[Delivery], Awaitable[None]] | None = None


@dataclass
class ChannelSlot:
    name: str
    factory: Callable[[], Awaitable[ManagedChannel] | ManagedChannel]
    desired_enabled: bool = False
    state: str = CHANNEL_STOPPED
    error: str = ""
    instance: ManagedChannel | None = None
    task: asyncio.Task[None] | None = None


class ChannelManager:
    """Keep channel failures inside their own restartable task."""

    def __init__(
        self,
        *,
        persist_enabled: Callable[[str, bool], Awaitable[None] | None] | None = None,
        restart_delay_seconds: float = 2.0,
        stop_timeout_seconds: float = 10.0,
    ) -> None:
        self._slots: dict[str, ChannelSlot] = {}
        self._persist_enabled = persist_enabled
        self._restart_delay_seconds = max(0.1, float(restart_delay_seconds))
        self._stop_timeout_seconds = max(1.0, float(stop_timeout_seconds))
        self._lock = asyncio.Lock()
        self._shutting_down = False

    def register(
        self,
        name: str,
        factory: Callable[[], Awaitable[ManagedChannel] | ManagedChannel],
        *,
        enabled: bool = False,
    ) -> None:
        normalized = str(name or "").strip().lower()
        if not normalized:
            raise ValueError("channel name is required")
        if normalized in self._slots:
            raise ValueError(f"channel already registered: {normalized}")
        self._slots[normalized] = ChannelSlot(
            name=normalized,
            factory=factory,
            desired_enabled=bool(enabled),
        )

    def names(self) -> tuple[str, ...]:
        return tuple(self._slots)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": slot.name,
                "enabled": slot.desired_enabled,
                "state": slot.state,
                "error": slot.error,
            }
            for slot in self._slots.values()
        ]

    async def start_enabled(self) -> None:
        self._shutting_down = False
        for slot in self._slots.values():
            if slot.desired_enabled:
                await self.start(slot.name, persist=False)

    async def start(self, name: str, *, persist: bool = True) -> dict[str, Any]:
        slot = self._slot(name)
        async with self._lock:
            slot.desired_enabled = True
            slot.error = ""
            if persist:
                await self._persist(slot.name, True)
            if slot.task is None or slot.task.done():
                slot.task = asyncio.create_task(
                    self._supervise(slot),
                    name=f"xagent-channel-{slot.name}",
                )
        return await self._settled_start_snapshot(slot)

    async def stop(self, name: str, *, persist: bool = True) -> dict[str, Any]:
        slot = self._slot(name)
        async with self._lock:
            slot.desired_enabled = False
            if persist:
                await self._persist(slot.name, False)
            slot.state = CHANNEL_STOPPING
            instance = slot.instance
            task = slot.task
        if instance is not None:
            try:
                result = instance.stop()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("Channel stop failed: %s", slot.name, exc_info=True)
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._stop_timeout_seconds)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        slot.instance = None
        slot.task = None
        slot.state = CHANNEL_STOPPED
        return self._snapshot_slot(slot)

    async def restart(self, name: str) -> dict[str, Any]:
        await self.stop(name, persist=False)
        return await self.start(name, persist=True)

    async def stop_all(self) -> None:
        self._shutting_down = True
        for name in tuple(self._slots):
            await self.stop(name, persist=False)

    async def send(self, delivery: Delivery) -> None:
        slot = self._slot(delivery.channel)
        if not slot.desired_enabled:
            raise ChannelDisabled(delivery.channel)
        if slot.state != CHANNEL_RUNNING or slot.instance is None:
            raise ChannelUnavailable(delivery.channel)
        sender = slot.instance.send
        if sender is None:
            raise ChannelUnavailable(delivery.channel)
        await sender(delivery)

    async def _supervise(self, slot: ChannelSlot) -> None:
        failures = 0
        while slot.desired_enabled and not self._shutting_down:
            slot.state = CHANNEL_STARTING
            slot.error = ""
            instance: ManagedChannel | None = None
            try:
                value = slot.factory()
                instance = await value if inspect.isawaitable(value) else value
                slot.instance = instance
                slot.state = CHANNEL_RUNNING
                await instance.run()
                if slot.desired_enabled and not self._shutting_down:
                    failures += 1
                    slot.state = CHANNEL_DEGRADED
                    slot.error = "channel exited unexpectedly"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                slot.state = CHANNEL_DEGRADED
                slot.error = str(exc)
                logger.exception("Channel failed: %s", slot.name)
            finally:
                if instance is not None:
                    try:
                        result = instance.stop()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.warning(
                            "Channel cleanup failed: %s",
                            slot.name,
                            exc_info=True,
                        )
                slot.instance = None
            if slot.desired_enabled and not self._shutting_down:
                delay = min(
                    60.0,
                    self._restart_delay_seconds
                    * (2 ** min(6, max(0, failures - 1))),
                )
                await asyncio.sleep(delay)
        slot.state = CHANNEL_STOPPED

    def _slot(self, name: str) -> ChannelSlot:
        normalized = str(name or "").strip().lower()
        try:
            return self._slots[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown channel: {normalized}") from exc

    async def _persist(self, name: str, enabled: bool) -> None:
        if self._persist_enabled is None:
            return
        result = self._persist_enabled(name, enabled)
        if inspect.isawaitable(result):
            await result

    async def _settled_start_snapshot(
        self,
        slot: ChannelSlot,
        *,
        timeout_seconds: float = 1.0,
    ) -> dict[str, Any]:
        """Return the adapter's first meaningful state instead of stale stopped."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            if slot.state in {CHANNEL_RUNNING, CHANNEL_DEGRADED}:
                break
            if slot.task is not None and slot.task.done():
                break
            await asyncio.sleep(0.01)
        return self._snapshot_slot(slot)

    @staticmethod
    def _snapshot_slot(slot: ChannelSlot) -> dict[str, Any]:
        return {
            "name": slot.name,
            "enabled": slot.desired_enabled,
            "state": slot.state,
            "error": slot.error,
        }


class ChannelDisabled(RuntimeError):
    pass


class ChannelUnavailable(RuntimeError):
    pass
