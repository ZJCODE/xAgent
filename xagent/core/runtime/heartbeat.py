"""Generic runtime heartbeat for long-lived xAgent processes."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from ..config import AgentConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeHeartbeatConfig:
    """Configuration for the generic runtime heartbeat."""

    enabled: bool = AgentConfig.RUNTIME_HEARTBEAT_ENABLED
    interval_seconds: float = AgentConfig.RUNTIME_HEARTBEAT_INTERVAL_SECONDS

    @classmethod
    def from_mapping(cls, runtime_config: Optional[Mapping[str, Any]]) -> "RuntimeHeartbeatConfig":
        data = runtime_config if isinstance(runtime_config, Mapping) else {}
        enabled = data.get("heartbeat_enabled", AgentConfig.RUNTIME_HEARTBEAT_ENABLED)
        interval = data.get(
            "heartbeat_interval_seconds",
            AgentConfig.RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
        )
        if not isinstance(enabled, bool):
            enabled = AgentConfig.RUNTIME_HEARTBEAT_ENABLED
        try:
            interval_value = float(interval)
        except (TypeError, ValueError):
            interval_value = float(AgentConfig.RUNTIME_HEARTBEAT_INTERVAL_SECONDS)
        if interval_value <= 0:
            interval_value = float(AgentConfig.RUNTIME_HEARTBEAT_INTERVAL_SECONDS)
        return cls(enabled=enabled, interval_seconds=interval_value)


class RuntimeHeartbeat:
    """Periodic maintenance loop for long-lived runtimes."""

    def __init__(
        self,
        agent: Any,
        *,
        interval_seconds: float = AgentConfig.RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
        today_provider: Callable[[], date] = date.today,
        logger_: Optional[logging.Logger] = None,
        subconscious_loop: Any = None,
    ) -> None:
        self.agent = agent
        self.interval_seconds = max(0.001, float(interval_seconds))
        self._today_provider = today_provider
        self._logger = logger_ or logger
        self._task: Optional[asyncio.Task[None]] = None
        self._subconscious_loop = subconscious_loop
        self._reflection_task: Optional[asyncio.Task] = None

    @property
    def subconscious_loop(self):
        return self._subconscious_loop

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._task = asyncio.create_task(self._run_loop(), name="xagent-runtime-heartbeat")

    async def stop(self) -> None:
        tasks = [task for task in (self._task, self._reflection_task) if task is not None]
        self._task = self._reflection_task = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_once(self) -> None:
        handler = getattr(self.agent, "memory_handler", None)
        if handler is not None:
            try:
                handler.request_maintenance(today=self._today_provider())
            except Exception:
                self._logger.warning("Could not wake memory maintenance", exc_info=True)
        if self._reflection_task is None or self._reflection_task.done():
            self._reflection_task = asyncio.create_task(
                self._reflect_and_flush(), name="xagent-reflection")

    async def _reflect_and_flush(self) -> None:
        try:
            if self._subconscious_loop is not None:
                await self._subconscious_loop.maybe_think()
        except Exception:
            self._logger.warning("Subconscious cycle failed", exc_info=True)
        finally:
            observability = getattr(self.agent, "observability", None)
            if observability is not None:
                try:
                    await observability.flush()
                except Exception:
                    self._logger.warning("Observability flush failed", exc_info=True)

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning("Runtime heartbeat tick failed: %s", exc)
            delay = self.interval_seconds
            autonomous = getattr(self._subconscious_loop, "_autonomous", None)
            if (autonomous is not None and getattr(self._subconscious_loop._agent, "subconscious_mode", "reflect") == "act"
                    and not self._subconscious_loop._is_agent_busy()):
                delay = min(delay, max(1.0, autonomous.state["next_run_at"] - time.time()))
            await asyncio.sleep(delay)


def create_runtime_heartbeat(
    agent: Any,
    runtime_config: Optional[Mapping[str, Any]],
    *,
    logger_: Optional[logging.Logger] = None,
    subconscious_delivery_sink: Optional[Callable[..., Any]] = None,
    subconscious_deliverable_channels: Optional[Iterable[str]] = None,
    subconscious_contact_deliverable: Optional[Callable[..., bool]] = None,
) -> Optional[RuntimeHeartbeat]:
    config = RuntimeHeartbeatConfig.from_mapping(runtime_config)
    if not config.enabled:
        return None

    # Resolve workspace path for the subconscious loop
    workspace = _resolve_agent_workspace(agent)
    subconscious_loop = None
    if workspace is not None and AgentConfig.SUBCONSCIOUS_ENABLED:
        from .subconscious import SubconsciousLoop

        subconscious_loop = SubconsciousLoop(
            agent,
            workspace=workspace,
            interval_seconds=getattr(agent, "subconscious_interval_seconds", None),
            delivery_sink=subconscious_delivery_sink,
            deliverable_channels=subconscious_deliverable_channels,
            contact_deliverable=subconscious_contact_deliverable,
            logger_=logger_,
        )

    return RuntimeHeartbeat(
        agent,
        interval_seconds=config.interval_seconds,
        logger_=logger_,
        subconscious_loop=subconscious_loop,
    )


def _resolve_agent_workspace(agent: Any) -> Optional[Path]:
    """Resolve the agent workspace directory path."""
    markdown_memory = getattr(agent, "markdown_memory", None)
    if markdown_memory is not None:
        root = getattr(markdown_memory, "root", None)
        if root is not None:
            # root is the memory/ directory; workspace is its parent
            return Path(root).parent
    workspace = getattr(agent, "workspace", None)
    if workspace is not None:
        return Path(workspace)
    workspace_dir = getattr(agent, "workspace_dir", None)
    if workspace_dir is not None:
        workspace_path = Path(workspace_dir)
        # workspace_dir is typically <root>/workspace; contacts live at <root>.
        if workspace_path.name == AgentConfig.WORKSPACE_DIRNAME:
            return workspace_path.parent
        return workspace_path
    return None
