"""Composition root for one process, one brain, and hot-swappable channels."""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn

from ..agent_factory import AgentFactory
from ...settings import XAgentSettings
from ..config import AgentConfig
from .channels import ChannelManager, ManagedChannel
from .control import RuntimeControlServer
from .delivery import DeliveryDispatcher
from .engine import AgentRuntime, RuntimeAgentProxy
from .heartbeat import create_runtime_heartbeat
from .state import RuntimeStateStore
from .scheduling import (
    RuntimeTaskScheduler,
    RuntimeTaskStore,
    create_runtime_task_tool,
)
from .subconscious import ContactEntry, SubconsciousDelivery
from .types import AgentEvent, Delivery


logger = logging.getLogger(__name__)


class _ApiChannel:
    def __init__(self, *, agent: RuntimeAgentProxy, config_dir: Path, config: dict[str, Any]) -> None:
        from ...interfaces.server import AgentHTTPServer

        self.http = AgentHTTPServer(config_dir=str(config_dir), agent=agent)
        uvicorn_config = uvicorn.Config(
            self.http.app,
            host=str(config.get("host") or "127.0.0.1"),
            port=int(config.get("port") or 8010),
            log_level="info",
        )
        self.server = uvicorn.Server(uvicorn_config)
        self.server.install_signal_handlers = lambda: None

    async def run(self) -> None:
        await self.server.serve()

    async def stop(self) -> None:
        self.server.should_exit = True

    async def send(self, delivery: Delivery) -> None:
        user_id = str(
            delivery.target.get("user_id")
            or delivery.target.get("speaker_id")
            or ""
        ).strip()
        if not user_id:
            raise ValueError("API delivery target requires user_id")
        await self.http.api.delivery.push(
            user_id,
            {
                "type": "scheduled_message",
                "content": str(delivery.payload.get("content") or ""),
                "delivery_id": delivery.delivery_id,
            },
        )


class _VoiceChannel:
    def __init__(self, *, agent: RuntimeAgentProxy, config: dict[str, Any]) -> None:
        from ...interfaces.voice.config import VoiceChannelConfig
        from ...interfaces.voice.factory import create_local_voice_runtime
        from ...interfaces.voice.runtime import VoiceRuntimeOptions

        self.runtime = create_local_voice_runtime(
            agent=agent,
            config=VoiceChannelConfig.from_dict(config),
            options=VoiceRuntimeOptions(
                user_id="local_voice",
                stream=True,
            ),
        )

    async def run(self) -> None:
        await self.runtime.run_forever()

    async def stop(self) -> None:
        self.runtime.stop_event.set()

    async def send(self, delivery: Delivery) -> None:
        await self.runtime.send(delivery)


class RuntimeFactory:
    """Build all dependencies once; adapters never construct an Agent."""

    def __init__(self, config_dir: str | Path) -> None:
        self.config_dir = Path(config_dir).expanduser().resolve()

    def build(self) -> "UnifiedAgentRuntime":
        settings = XAgentSettings.load(self.config_dir / "config.yaml")
        agent_factory = AgentFactory(config_dir=str(self.config_dir))
        agent = agent_factory.agent
        state = RuntimeStateStore(agent_factory.message_storage.path)
        runtime = AgentRuntime(
            agent,
            state,
            turn_timeout_seconds=settings.runtime.turn_timeout_seconds,
        )
        task_store = RuntimeTaskStore(state)
        agent.tool_manager.register_tools([
            create_runtime_task_tool(task_store),
        ])
        task_scheduler = RuntimeTaskScheduler(task_store, runtime, state)
        proxy = RuntimeAgentProxy(runtime)
        channels = ChannelManager(
            persist_enabled=self._persist_channel_enabled,
        )

        self._register_channels(channels, settings, proxy, state)

        async def subconscious_contacts() -> list[ContactEntry]:
            contacts: list[ContactEntry] = []
            for person in await state.list_people():
                for account in person["accounts"]:
                    if not account["allow_proactive"]:
                        continue
                    account_id = str(account["account_id"])
                    contacts.append(
                        ContactEntry(
                            channel=str(account["channel"]),
                            user_id=account_id,
                            target={
                                "user_id": account_id,
                                "sender_id": account_id,
                                "chat_id": account_id,
                            },
                            last_seen=datetime.fromtimestamp(
                                float(account["last_seen_at"])
                            ).isoformat(timespec="seconds"),
                        )
                    )
            return contacts

        async def subconscious_delivery(value: SubconsciousDelivery) -> None:
            await state.add_delivery(
                Delivery.create(
                    event_id=value.event_id,
                    channel=value.recipient.channel,
                    target=value.recipient.target,
                    payload={
                        "content": value.content,
                        "source": "subconscious",
                    },
                )
            )

        async def submit_subconscious(event: AgentEvent) -> None:
            await runtime.submit(event)

        heartbeat = create_runtime_heartbeat(
            agent,
            settings.runtime.model_dump(mode="python"),
            logger_=logger,
            subconscious_delivery_sink=subconscious_delivery,
            subconscious_event_sink=submit_subconscious,
            subconscious_before_side_effect=state.mark_side_effect_started,
            subconscious_contacts_provider=subconscious_contacts,
            subconscious_deliverable_channels=set(channels.names()),
            operation_lock=runtime.cognitive_lock,
        )
        if heartbeat is not None and heartbeat.subconscious_loop is not None:
            runtime.set_subconscious_handler(heartbeat.subconscious_loop.process_event)
        return UnifiedAgentRuntime(
            config_dir=self.config_dir,
            agent=agent,
            state=state,
            runtime=runtime,
            channels=channels,
            heartbeat=heartbeat,
            task_scheduler=task_scheduler,
        )

    def _register_channels(
        self,
        manager: ChannelManager,
        settings: XAgentSettings,
        proxy: RuntimeAgentProxy,
        state: RuntimeStateStore,
    ) -> None:
        async def persist_delivery(delivery: Delivery) -> None:
            await state.add_delivery(delivery)

        def current_channel_config(name: str) -> dict[str, Any]:
            """Read credentials at adapter creation time so setup is hot-reloadable."""
            current = XAgentSettings.load(self.config_dir / "config.yaml")
            value = dict(current.channels.model_dump(mode="python")[name])
            value.pop("enabled", None)
            return value

        manager.register(
            "api",
            lambda: self._managed(
                _ApiChannel(
                    agent=proxy,
                    config_dir=self.config_dir,
                    config=current_channel_config("api"),
                )
            ),
            enabled=settings.channels.api.enabled,
        )

        def create_feishu() -> ManagedChannel:
            try:
                from ...integrations.feishu import FeishuAdapter, FeishuAdapterConfig
            except ImportError as exc:
                raise RuntimeError(
                    "Feishu dependencies are missing; install myxagent[feishu]"
                ) from exc
            return self._managed(
                FeishuAdapter(
                    proxy,
                    FeishuAdapterConfig.from_dict(current_channel_config("feishu")),
                    delivery_sink=persist_delivery,
                )
            )

        manager.register(
            "feishu",
            create_feishu,
            enabled=settings.channels.feishu.enabled,
        )

        def create_weixin() -> ManagedChannel:
            try:
                from ...integrations.weixin import WeixinAdapter, WeixinAdapterConfig
            except ImportError as exc:
                raise RuntimeError(
                    "Weixin dependencies are missing; install myxagent[weixin]"
                ) from exc
            return self._managed(
                WeixinAdapter(
                    proxy,
                    WeixinAdapterConfig.from_dict(current_channel_config("weixin")),
                    runtime_dir=self.config_dir,
                    delivery_sink=persist_delivery,
                )
            )

        manager.register(
            "weixin",
            create_weixin,
            enabled=settings.channels.weixin.enabled,
        )

        manager.register(
            "voice",
            lambda: self._managed(
                _VoiceChannel(
                    agent=proxy,
                    config=current_channel_config("voice"),
                )
            ),
            enabled=settings.channels.voice.enabled,
        )

    async def _persist_channel_enabled(self, name: str, enabled: bool) -> None:
        await asyncio.to_thread(self._persist_channel_enabled_sync, name, enabled)

    def _persist_channel_enabled_sync(self, name: str, enabled: bool) -> None:
        path = self.config_dir / "config.yaml"
        settings = XAgentSettings.load(path)
        settings.with_channel_enabled(name, enabled).write_atomic(path)

    @staticmethod
    def _managed(instance: Any) -> ManagedChannel:
        run = getattr(instance, "run", None) or getattr(instance, "run_forever")
        sender = getattr(instance, "send", None)
        return ManagedChannel(run=run, stop=instance.stop, send=sender)


class UnifiedAgentRuntime:
    """Own every long-lived component of one agent process."""

    def __init__(
        self,
        *,
        config_dir: Path,
        agent: Any,
        state: RuntimeStateStore,
        runtime: AgentRuntime,
        channels: ChannelManager,
        heartbeat: Any = None,
        task_scheduler: RuntimeTaskScheduler,
    ) -> None:
        self.config_dir = config_dir
        self.agent = agent
        self.state = state
        self.runtime = runtime
        self.channels = channels
        self.heartbeat = heartbeat
        self.task_scheduler = task_scheduler
        self.delivery_dispatcher = DeliveryDispatcher(state, channels)
        self._stop_event = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._stopped = True
        self.control = RuntimeControlServer(
            runtime=runtime,
            channels=channels,
            state=state,
            tasks=task_scheduler.store,
            runtime_dir=config_dir,
            shutdown=self.request_stop,
            delivery_wakeup=self.delivery_dispatcher.wake,
        )

    async def start(self) -> None:
        self._stopped = False
        await self.runtime.start()
        try:
            await self.control.start()
            if self.heartbeat is not None:
                await self.heartbeat.start()
            await self.delivery_dispatcher.start()
            await self.task_scheduler.start()
            await self.channels.start_enabled()
        except Exception:
            await self.stop()
            raise

    async def request_stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        await self.start()
        await self._stop_event.wait()
        await self.stop()

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            await self.task_scheduler.stop()
            await self.delivery_dispatcher.stop()
            await self.channels.stop_all()
            if self.heartbeat is not None:
                await self.heartbeat.stop()
            await self.runtime.stop()
            try:
                await self.agent.run_memory_maintenance(trigger="shutdown")
            except Exception:
                logger.warning("Final diary maintenance failed", exc_info=True)
            await self.control.stop()


async def run_runtime(config_dir: str | Path) -> None:
    application = RuntimeFactory(config_dir).build()
    loop = asyncio.get_running_loop()
    registered: list[int] = []

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, application._stop_event.set)
            registered.append(signum)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await application.run_forever()
    finally:
        for signum in registered:
            loop.remove_signal_handler(signum)
