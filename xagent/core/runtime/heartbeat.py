"""Generic runtime heartbeat for long-lived xAgent processes."""

from __future__ import annotations

import asyncio
import logging
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

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._task = asyncio.create_task(self._run_loop(), name="xagent-runtime-heartbeat")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._logger.warning("Runtime heartbeat stopped after failure: %s", exc)

    async def run_once(self) -> None:
        await self._run_memory_maintenance()
        today = self._today_provider()
        if today.weekday() == 0:
            await self._generate_previous_weekly_summary(today)
        if self._subconscious_loop is not None:
            await self._subconscious_loop.maybe_think()

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning("Runtime heartbeat tick failed: %s", exc)
            await asyncio.sleep(self.interval_seconds)

    async def _run_memory_maintenance(self) -> None:
        flusher = getattr(self.agent, "run_memory_maintenance", None)
        if flusher is None:
            return
        try:
            await flusher(trigger="count")
        except Exception as exc:
            self._logger.warning("Runtime heartbeat memory maintenance failed: %s", exc)

    async def _generate_previous_weekly_summary(self, today: date) -> None:
        memory_handler = getattr(self.agent, "memory_handler", None)
        generator = getattr(memory_handler, "generate_previous_weekly_summary_if_missing", None)
        if generator is None:
            return
        try:
            await generator(today=today)
        except Exception as exc:
            self._logger.warning("Runtime heartbeat weekly maintenance failed: %s", exc)


def create_runtime_heartbeat(
    agent: Any,
    runtime_config: Optional[Mapping[str, Any]],
    *,
    logger_: Optional[logging.Logger] = None,
    subconscious_delivery_sink: Optional[Callable[..., Any]] = None,
    subconscious_deliverable_channels: Optional[Iterable[str]] = None,
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
            probability=getattr(agent, "subconscious_activity", None),
            pure_thought=getattr(agent, "subconscious_pure_thought", None),
            delivery_sink=subconscious_delivery_sink,
            deliverable_channels=subconscious_deliverable_channels,
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
    workspace_dir = getattr(agent, "workspace_dir", None)
    if workspace_dir is not None:
        return Path(workspace_dir)
    return None
