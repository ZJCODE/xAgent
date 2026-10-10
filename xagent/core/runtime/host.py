"""One authoritative, local runtime for an agent and all of its channels."""
from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
import logging
import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from ...interfaces.server.models import ChatInput, ObserveInput

from .ownership import RuntimeOwnership, canonical_root, prepare_socket_directory, write_runtime_status

logger = logging.getLogger(__name__)


class RuntimeChatInput(ChatInput):
    stream: bool = True
    channel: str = "cli"
    room_name: str | None = None
    sender_name: str = ""
    extra_message_metadata: dict | None = None
    channel_instructions: str = ""
    inbox_kind: str = "user_turn"
    max_agent_loops: int | None = None


class RuntimeObserveInput(ObserveInput):
    room_name: str | None = None
    channel: str | None = None
    user_id: str | None = None


class TurnControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    channel: str | None = "api"
    content: str = ""


class _HostedServer(uvicorn.Server):
    """Servers share the host's signal handler and cannot own shutdown."""

    @contextmanager
    def capture_signals(self):
        yield


class RuntimeHost:
    def __init__(self, config_dir: str | Path, channels: list[str] | None = None):
        self.config_dir = canonical_root(config_dir)
        self.requested_channels = channels
        self.ownership = RuntimeOwnership(self.config_dir)
        self.paths = self.ownership.paths
        self.runner = None
        self.agent = None
        self.api = None
        self.admin = None
        self.heartbeat = None
        self.scheduler = None
        self.shutdown_timeout = 30.0
        self._shutdown_pending: set[asyncio.Task] = set()
        self._ownership_finalized = False
        self.adapters: dict[str, Any] = {}
        self.channel_status: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._servers: dict[str, _HostedServer] = {}
        self._stop_event = asyncio.Event()
        self.runtime_ready = False
        self.state = "stopped"
        self.started_at = None
        self.recovered_turns = 0
        self.control_app = None
        self._startup_fingerprint = None

    def _configuration_fingerprint(self):
        digest = hashlib.sha256()
        for name in ("config.yaml", "identity.md"):
            try:
                digest.update((self.config_dir / name).read_bytes())
            except FileNotFoundError:
                digest.update(b"missing")
        return digest.hexdigest()

    def status(self) -> dict:
        inbox = getattr(self.agent, "inbox", None)
        return {
            "pid": os.getpid(), "root": str(self.config_dir),
            "state": self.state, "runtime_ready": self.runtime_ready,
            "started_at": self.started_at, "channels": self.channel_status,
            "socket_path": str(self.paths.socket_path),
            "model": getattr(self.agent, "model", None),
            "tools": list(getattr(self.agent, "tools", {})),
            "queue": {"pending": getattr(inbox, "pending_count", 0),
                "active_turn_id": getattr(inbox, "current_turn_id", None),
                "max_pending": 32, "queue_timeout_seconds": 30, "run_timeout_seconds": 600},
            "recovered_turns": self.recovered_turns,
            "needs_restart": self._startup_fingerprint is not None and self._startup_fingerprint != self._configuration_fingerprint(),
        }

    async def detailed_status(self) -> dict:
        result = self.status()
        if self.agent is not None:
            result["turns"] = await self.agent.turn_store.get_status()
            get_maintenance = getattr(self.agent.memory_handler, "get_maintenance_status", None)
            if callable(get_maintenance):
                result["journal"] = await get_maintenance()
        get_tasks = getattr(self.scheduler, "get_status", None)
        if callable(get_tasks):
            result["tasks"] = await get_tasks()
        return result

    def _publish_status(self) -> None:
        write_runtime_status(self.paths, self.status())

    async def run(self) -> None:
        """Acquire ownership before any Agent, storage recovery, or channel starts."""
        self.ownership.acquire()
        self.state = "starting"
        self.started_at = time.time()
        handlers = []
        try:
            self._publish_status()
            from ...interfaces.base import BaseAgentRunner
            from ...interfaces.cli.channels import enabled_channels_from_config
            from ...interfaces.cli.processes import managed_paths, running_pid
            from ...interfaces.server.admin_service import AdminService
            from ...integrations.api import ApiChannelAdapter
            from . import AsyncTaskScheduler, create_runtime_heartbeat, resolve_contacts_path

            legacy = [channel for channel in ("api", "feishu", "weixin", "voice")
                      if running_pid(managed_paths(self.config_dir, channel).pid_path) is not None]
            if legacy:
                raise RuntimeError("Close the legacy channel processes before starting the Agent: " + ", ".join(legacy))
            self.runner = BaseAgentRunner(config_dir=str(self.config_dir))
            self._startup_fingerprint = self._configuration_fingerprint()
            self.agent = self.runner.agent
            self.admin = AdminService(config_dir=str(self.config_dir), agent=self.agent)
            self.admin.is_runtime_owner = True
            recovery = getattr(self.agent.memory_handler, "recover_pending_commit", None)
            if callable(recovery):
                await recovery()
            self.recovered_turns = await self.agent.turn_store.recover()
            await self.agent.attention.reset_to_present(self.agent.message_storage)
            self.api = ApiChannelAdapter(self.agent,
                contacts_file=resolve_contacts_path(self.config_dir), tasks_dir=self.runner.tasks_dir)
            self.adapters["api"] = self.api
            channels = self.requested_channels if self.requested_channels is not None else enabled_channels_from_config(self.runner.config)
            for channel in channels:
                if channel not in {"api", "feishu", "weixin", "voice"}:
                    raise ValueError(f"Unknown channel: {channel}")
                self.channel_status[channel] = {"state": "starting"}

            self.control_app = self._create_control_app()
            await self._start_control_server()
            for channel in channels:
                try:
                    await self._start_channel(channel)
                except Exception as exc:
                    self._channel_failed(channel, exc)
            self.scheduler = AsyncTaskScheduler(
                self.runner.tasks_dir, can_handle=self._can_handle_task,
                execute=self._execute_task, deliver=self._deliver_task,
                shutdown_timeout_seconds=10.0,
            )
            await self.scheduler.start()
            self.heartbeat = create_runtime_heartbeat(
                self.agent, self.runner.config.get("runtime"),
                subconscious_delivery_sink=self._deliver_subconscious,
                subconscious_deliverable_channels=set(self.adapters),
            )
            if self.heartbeat is not None:
                await self.heartbeat.start()
            self.runtime_ready = True
            self.state = "degraded" if any(row["state"] == "failed" for row in self.channel_status.values()) else "running"
            self.paths.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.paths.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            self._publish_status()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, self._stop_event.set)
                    handlers.append(signum)
                except (NotImplementedError, RuntimeError):
                    pass
            await self._stop_event.wait()
        finally:
            for signum in handlers:
                asyncio.get_running_loop().remove_signal_handler(signum)
            try:
                await self._shutdown()
            finally:
                self._release_ownership_when_idle()

    def _release_ownership_when_idle(self, _finished=None) -> None:
        if self._ownership_finalized:
            return
        if any(not task.done() for task in self._shutdown_pending):
            if _finished is None:
                for task in self._shutdown_pending:
                    task.add_done_callback(self._release_ownership_when_idle)
            return
        self._ownership_finalized = True
        try:
            self.paths.pid_path.unlink(missing_ok=True)
            self.state = "stopped"
            self._publish_status()
        except Exception:
            logger.exception("Could not publish final runtime status")
        finally:
            self.ownership.release()

    async def stop(self) -> None:
        self._stop_event.set()

    def _channel_failed(self, channel, exc) -> None:
        logger.error("%s channel failed: %s", channel, exc, exc_info=exc)
        self.channel_status[channel] = {"state": "failed", "error": f"{type(exc).__name__}: check the runtime log"}
        if self.runtime_ready:
            self.state = "degraded"
            self._publish_status()

    def _channel_connection(self, channel: str, state: str) -> None:
        self.channel_status[channel] = {"state": state}
        if self.runtime_ready:
            self.state = "degraded" if any(row["state"] in {"failed", "reconnecting"}
                for row in self.channel_status.values()) else "running"
            self._publish_status()

    async def _watch_channel(self, channel, awaitable) -> None:
        self.channel_status[channel] = {"state": "connecting"}
        try:
            await awaitable
            if not self._stop_event.is_set():
                self._channel_failed(channel, RuntimeError("Channel stopped unexpectedly"))
        except asyncio.CancelledError:
            raise
        except (Exception, SystemExit) as exc:
            self._channel_failed(channel, exc)

    async def _start_channel(self, channel) -> None:
        config = self.runner.config
        if channel == "api":
            from ...interfaces.server import AgentHTTPServer
            from ...interfaces.cli.channels import api_config

            cfg = api_config(config)
            service = AgentHTTPServer(config_dir=str(self.config_dir), agent=self.agent,
                runtime_managed=True, api_adapter=self.api)
            server = _HostedServer(uvicorn.Config(service.app,
                host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 8010)),
                lifespan="off", access_log=False, timeout_graceful_shutdown=10))
            self._servers["api"] = server
            run = server.serve()
        elif channel == "feishu":
            from ...integrations.feishu import FeishuAdapter, FeishuAdapterConfig
            from ...interfaces.cli.channels import feishu_config

            adapter = FeishuAdapter(agent=self.agent, config=FeishuAdapterConfig.from_dict(feishu_config(config)))
            self.adapters[channel] = adapter
            run = adapter.run()
        elif channel == "weixin":
            from ...integrations.weixin import WeixinAdapter, WeixinAdapterConfig
            from ...interfaces.cli.channels import weixin_config

            adapter = WeixinAdapter(agent=self.agent, config=WeixinAdapterConfig.from_dict(weixin_config(config)), runtime_dir=self.config_dir)
            self.adapters[channel] = adapter
            run = adapter.run()
        else:
            from ...interfaces.voice.config import VoiceChannelConfig
            from ...interfaces.voice.factory import create_local_voice_runtime
            from ...interfaces.voice.runtime import VoiceRuntimeOptions
            from ...interfaces.cli.channels import voice_config

            adapter = create_local_voice_runtime(agent=self.agent,
                config=VoiceChannelConfig.from_dict(voice_config(config)),
                options=VoiceRuntimeOptions(tasks_dir=self.runner.tasks_dir))
            self.adapters[channel] = adapter
            run = adapter.run_forever()
        if channel != "api":
            loop = asyncio.get_running_loop()
            adapter.runtime_status = lambda state: loop.call_soon_threadsafe(self._channel_connection, channel, state)
        self._tasks[channel] = asyncio.create_task(self._watch_channel(channel, run), name=f"xagent-channel-{channel}")
        await asyncio.sleep(0)
        if channel == "api":
            server = self._servers["api"]
            async def ready():
                while not server.started:
                    if self._tasks[channel].done():
                        return
                    await asyncio.sleep(0.01)
                self._channel_connection(channel, "connected")
            self._tasks["api-readiness"] = asyncio.create_task(ready())

    def _task_adapter(self, task):
        channel = task.delivery_channel
        if channel in {"", "local", "cli", "api"}:
            return self.api
        return self.adapters.get(channel)

    def _can_handle_task(self, task) -> bool:
        adapter = self._task_adapter(task)
        if adapter is None:
            return False
        check = getattr(adapter, "_can_handle_scheduled_task", None)
        if adapter is self.api:
            return self.api.tasks.can_handle(task)
        return bool(check(task)) if callable(check) else False

    async def _execute_task(self, task):
        adapter = self._task_adapter(task)
        if adapter is None:
            raise RuntimeError("Task channel is unavailable")
        if adapter is self.api:
            return await self.api.tasks.execute(task)
        return await adapter.execute_scheduled_task(task)

    async def _deliver_task(self, task, result, run_id):
        adapter = self._task_adapter(task)
        if adapter is None:
            raise RuntimeError("Task channel is unavailable")
        if adapter is self.api:
            return await self.api.tasks.deliver(task, result, run_id)
        return await adapter.deliver_scheduled_task(task, result, run_id)

    async def _deliver_subconscious(self, delivery):
        adapter = self.adapters.get(delivery.recipient.channel)
        if adapter is None:
            raise RuntimeError("Subconscious delivery channel is unavailable")
        await adapter.deliver_subconscious_message(delivery)

    async def _start_control_server(self):
        prepare_socket_directory(self.paths)
        self.paths.socket_path.unlink(missing_ok=True)
        server = _HostedServer(uvicorn.Config(self.control_app, uds=str(self.paths.socket_path),
            lifespan="off", access_log=False, timeout_graceful_shutdown=10))
        self._servers["control"] = server
        task = asyncio.create_task(server.serve(), name="xagent-control")
        self._tasks["control"] = task

        async def wait_ready():
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Runtime control server failed to start")
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_ready(), timeout=10.0)
        self.paths.socket_path.chmod(0o600)

    def _create_control_app(self):
        from ...interfaces.server.admin_routes import register_admin_routes
        from ...integrations.api.input_normalization import input_attachments, input_image_sources
        from . import ScheduledDeliveryContext, scheduled_delivery_context, upsert_contact

        app = FastAPI(title="xAgent private runtime")
        register_admin_routes(app, lambda: self.admin)

        @app.get("/runtime/status")
        async def status():
            return await self.detailed_status()

        @app.post("/runtime/stop")
        async def stop():
            self._stop_event.set()
            return {"stopping": True}

        @app.post("/runtime/turns/stop")
        async def abort(payload: TurnControl):
            if not payload.turn_id:
                raise HTTPException(422, "turn_id is required")
            return {"stopped": self.agent.abort(payload.turn_id, channel=payload.channel)}

        @app.post("/runtime/turns/steer")
        async def steer(payload: TurnControl):
            return {"steered": await self.agent.steer(payload.turn_id, payload.content, channel=payload.channel)}

        @app.post("/runtime/chat/events")
        @app.post("/runtime/chat")
        async def chat(payload: RuntimeChatInput):
            if not self.runtime_ready:
                raise HTTPException(503, "Agent runtime is not ready")
            attachments = input_attachments(payload)

            async def events():
                upsert_contact(self.api.contacts_file, channel="api", user_id=payload.user_id,
                    target={"user_id": payload.user_id})
                context = ScheduledDeliveryContext(channel="api", user_id=payload.user_id,
                    target={"user_id": payload.user_id}, metadata={"source": payload.channel})
                with scheduled_delivery_context(context):
                    async for event in self.agent.chat_events(
                        user_message=payload.user_message, user_id=payload.user_id,
                        image_source=input_image_sources(payload, attachments=attachments),
                        attachments=attachments, stream=payload.stream, channel=payload.channel,
                        room_name=payload.room_name, sender_name=payload.sender_name,
                        extra_message_metadata=payload.extra_message_metadata,
                        channel_instructions=payload.channel_instructions,
                        event_id=payload.event_id, turn_id=payload.turn_id, request_id=payload.request_id,
                        inbox_kind=payload.inbox_kind, max_agent_loops=payload.max_agent_loops,
                    ):
                        yield json.dumps(event, ensure_ascii=False) + "\n"
            return StreamingResponse(events(), media_type="application/x-ndjson")

        @app.post("/runtime/observe")
        async def observe(payload: RuntimeObserveInput):
            if not self.runtime_ready:
                raise HTTPException(503, "Agent runtime is not ready")
            result = await self.agent.observe(**payload.model_dump(exclude_none=True))
            return result.model_dump()

        @app.get("/runtime/events")
        async def subscribe(request: Request, user_id: str):
            queue = asyncio.Queue(maxsize=128)
            class Subscriber:
                async def send_json(self, payload):
                    queue.put_nowait(payload)
            subscriber = Subscriber()
            async def events():
                await self.api.delivery.register_subscriber(user_id, subscriber)
                try:
                    while not self._stop_event.is_set() and not await request.is_disconnected():
                        try:
                            payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                        except asyncio.TimeoutError:
                            payload = {"type": "keepalive"}
                        yield json.dumps(payload, ensure_ascii=False) + "\n"
                finally:
                    await self.api.delivery.unregister_subscriber(user_id, subscriber)
            return StreamingResponse(events(), media_type="application/x-ndjson")
        return app

    async def _shutdown(self):
        deadline = time.monotonic() + self.shutdown_timeout
        drain = asyncio.create_task(self._drain(), name="xagent-shutdown-drain")
        try:
            done, _ = await asyncio.wait({drain}, timeout=max(0.0, self.shutdown_timeout - 4.0))
            if drain not in done:
                logger.warning("Runtime drain reached its deadline; recording unfinished work")
                drain.cancel()
            elif not drain.cancelled():
                error = drain.exception()
                if error is not None:
                    logger.warning("Runtime drain failed: %s", error, exc_info=error)
        finally:
            if self.agent is not None:
                self.agent.inbox.stop_accepting()
                self.agent.inbox.cancel_pending()
                self.agent.abort()
            cleanup = set(self._tasks.values()) | {drain}
            for task in cleanup:
                if not task.done():
                    task.cancel()
            if cleanup:
                done, pending = await asyncio.wait(cleanup,
                    timeout=min(2.0, max(0.0, deadline-time.monotonic())))
                for task in done:
                    if not task.cancelled():
                        task.exception()  # consume transport failures
                if pending:
                    logger.error("Shutdown left %s unresponsive transport tasks", len(pending))
                    self._shutdown_pending = pending
            if self.agent is not None:
                remaining = max(0.001, deadline-time.monotonic())
                try:
                    await asyncio.wait_for(self.agent.turn_store.recover(), remaining)
                except Exception:
                    # Startup recovery also marks accepted/running ordinary
                    # turns interrupted; never replay them if this write fails.
                    logger.exception("Could not finalize interrupted turn receipts")
            self.paths.socket_path.unlink(missing_ok=True)
            self.runtime_ready = False
            # Keep the OS lock until unresponsive writes stop or the process
            # manager terminates us. A replacement core must never overlap.
            self.state = "stopping" if self._shutdown_pending else "stopped"
            self._publish_status()

    async def _drain(self):
        self._stop_event.set()
        self.runtime_ready = False
        self.state = "stopping"
        if self.agent is not None:
            self.agent.inbox.stop_accepting()
        if self.heartbeat is not None:
            await self.heartbeat.stop()
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.agent is not None:
            deadline = time.monotonic() + 15.0
            while (self.agent.inbox.busy or self.agent.inbox.pending_count) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            if self.agent.inbox.busy or self.agent.inbox.pending_count:
                self.agent.abort()
                self.agent.inbox.cancel_pending()
        if self.agent is not None:
            await self.agent.attention.stop()
        for adapter in self.adapters.values():
            stop = getattr(adapter, "stop", None)
            if callable(stop):
                try:
                    await asyncio.wait_for(stop(), timeout=3.0)
                except (Exception, asyncio.CancelledError):
                    logger.warning("Channel stop did not complete", exc_info=True)
            event = getattr(adapter, "stop_event", None)
            if event is not None:
                event.set()
        for server in self._servers.values():
            server.should_exit = True
        tasks = list(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.agent is not None:
            try:
                await asyncio.wait_for(self.agent.memory_handler.run_maintenance(force=True, trigger="shutdown"), timeout=10.0)
            except Exception:
                logger.warning("Final memory maintenance did not complete", exc_info=True)
            await self.agent.turn_store.recover()
            close = getattr(getattr(self.agent, "client", None), "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=3.0)
