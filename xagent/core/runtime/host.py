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

from ..config import AgentConfig
from .channel_activity import ChannelActivity
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
    user_id: str | None = None
    room_name: str | None = None


class RuntimeStopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restart: bool = False


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
        self.device_service = None
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
        self.channel_drain_timeout = 10.0
        self.channel_cleanup_timeout = 5.0
        self._channel_locks = {name: asyncio.Lock() for name in ("api", "weixin", "voice")}
        self._channel_generation = {name: 0 for name in self._channel_locks}
        self._channel_activity: dict[str, ChannelActivity] = {}
        self._channel_operations: dict[str, asyncio.Task] = {}
        self._api_app = None
        self._restarting = False
        self._restart_channels: list[str] | None = None
        self.lifecycle_timings: dict[str, float] = {}

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
            "started_at": self.started_at, "channels": {name: self._channel_view(name) for name in self.channel_status},
            "socket_path": str(self.paths.socket_path),
            "model": getattr(self.agent, "model", None),
            "tools": list(getattr(self.agent, "tools", {})),
            "queue": {"pending": getattr(inbox, "pending_count", 0),
                "active_turn_id": getattr(inbox, "current_turn_id", None),
                "max_pending": getattr(inbox, "max_pending", AgentConfig.DEFAULT_MAX_PENDING_TURNS),
                "queue_timeout_seconds": getattr(inbox, "queue_timeout", AgentConfig.DEFAULT_TURN_QUEUE_TIMEOUT),
                "run_timeout_seconds": getattr(inbox, "run_timeout", AgentConfig.DEFAULT_TURN_RUN_TIMEOUT)},
            "recovered_turns": self.recovered_turns,
            "lifecycle_timings": dict(self.lifecycle_timings),
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
        startup_started = time.monotonic()
        self.state = "starting"
        self.started_at = time.time()
        handlers = []
        try:
            self._publish_status()
            from ...interfaces.base import BaseAgentRunner
            from ...interfaces.cli.channels import default_runtime_channels
            from ...interfaces.cli.processes import managed_paths, running_pid
            from ...interfaces.server.admin_service import AdminService
            from ...integrations.api import ApiChannelAdapter
            from . import AsyncTaskScheduler, create_runtime_heartbeat, resolve_contacts_path

            legacy = [channel for channel in ("api", "weixin", "voice")
                      if running_pid(managed_paths(self.config_dir, channel).pid_path) is not None]
            if legacy:
                raise RuntimeError("Close the legacy channel processes before starting the Agent: " + ", ".join(legacy))
            self.runner = BaseAgentRunner(config_dir=str(self.config_dir))
            self._startup_fingerprint = self._configuration_fingerprint()
            self.agent = self.runner.agent
            self.device_service = getattr(self.runner, "device_service", None)
            if self.device_service is not None:
                self.device_service.on_voice_sessions_changed = self._sync_delivery_channels
            self.admin = AdminService(config_dir=str(self.config_dir), agent=self.agent,
                device_service=self.device_service)
            self.admin.is_runtime_owner = True
            recovery = getattr(self.agent.memory_handler, "recover_pending_commit", None)
            if callable(recovery):
                await recovery()
            self.recovered_turns = await self.agent.turn_store.recover()
            from ...interfaces.cli.channels import voice_config
            self.api = ApiChannelAdapter(self.agent,
                contacts_file=resolve_contacts_path(self.config_dir), tasks_dir=self.runner.tasks_dir,
                device_service=self.device_service, voice_config=voice_config(self.runner.config))
            self.adapters["api"] = self.api
            channels = self.requested_channels if self.requested_channels is not None else default_runtime_channels(self.runner.config)
            self.channel_status = {name: {"state": "disconnected"} for name in self._channel_locks}
            for channel in channels:
                if channel not in {"api", "weixin", "voice"}:
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
                subconscious_deliverable_channels=self._deliverable_channels(),
                subconscious_contact_deliverable=self._is_subconscious_contact_deliverable,
            )
            if self.heartbeat is not None:
                await self.heartbeat.start()
            self.runtime_ready = True
            self.lifecycle_timings["startup_seconds"] = round(time.monotonic() - startup_started, 3)
            self.state = "degraded" if any(row["state"] in {"failed", "reconnecting"} for row in self.channel_status.values()) else "running"
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

    def _channel_readiness(self, channel: str) -> tuple[bool, str]:
        if self.runner is None:
            return False, "Agent runtime is not ready"
        from ...interfaces.cli.channels import channel_readiness
        return channel_readiness(self.runner.config, channel, self.config_dir)

    def _channel_view(self, channel: str) -> dict:
        row = dict(self.channel_status.get(channel, {"state": "disconnected"}))
        ready, detail = self._channel_readiness(channel)
        resources = channel in self._tasks or channel in self._servers or (channel != "api" and channel in self.adapters)
        operation = self._channel_operations.get(channel)
        busy = operation is not None and not operation.done()
        available = self.runtime_ready and not self._stop_event.is_set()
        row.update(ready=ready, config_error=detail, can_connect=available and ready and not resources and not busy,
                   can_disconnect=available and resources and not busy and row["state"] != "disconnecting")
        return row

    def _update_channel_state(self, channel: str, state: str, **extra) -> None:
        self.channel_status[channel] = {"state": state, **extra}
        if self.runtime_ready and not self._stop_event.is_set():
            self.state = "degraded" if any(row["state"] in {"failed", "reconnecting"}
                for row in self.channel_status.values()) else "running"
            self._publish_status()
        self._sync_delivery_channels()

    def _deliverable_channels(self) -> set[str]:
        # The internal API delivery sink remains available without a public listener.
        channels = {"api"} | {name for name, row in self.channel_status.items() if row["state"] == "connected"}
        if self.device_service is not None and self.device_service.voice_sessions:
            channels.add("voice")
        return channels

    def _is_subconscious_contact_deliverable(self, contact: Any) -> bool:
        channel = str(getattr(contact, "channel", "") or "").strip().lower()
        user_id = str(getattr(contact, "user_id", "") or "").strip()
        target = getattr(contact, "target", None) or {}
        if channel not in self._deliverable_channels():
            return False
        if channel == "weixin":
            adapter = self.adapters.get("weixin")
            if adapter is None or self.channel_status.get("weixin", {}).get("state") != "connected":
                return False
            uid = str(target.get("user_id") or user_id).strip()
            tokens = getattr(adapter, "_context_tokens", None)
            if isinstance(tokens, dict):
                return bool(tokens.get(uid))
            getter = getattr(getattr(adapter, "client", None), "get_context_token", None)
            if callable(getter):
                return bool(getter(uid))
            return True
        if channel == "voice":
            adapter = self._resolve_voice_adapter(target, user_id=user_id)
            if adapter is None:
                return False
            allowed = getattr(adapter, "_proactive_speech_allowed", None)
            return bool(allowed()) if callable(allowed) else True
        return True

    def _sync_delivery_channels(self) -> None:
        loop = getattr(self.heartbeat, "subconscious_loop", None)
        if loop is not None:
            loop.set_deliverable_channels(self._deliverable_channels())
        if self.scheduler is not None:
            self.scheduler.wake()

    def _channel_failed(self, channel, exc) -> None:
        logger.error("%s channel failed: %s", channel, exc, exc_info=exc)
        self._update_channel_state(channel, "failed", error=f"{type(exc).__name__}: check the runtime log")

    def _channel_connection(self, channel: str, state: str, generation: int | None = None) -> None:
        if generation is not None and generation != self._channel_generation[channel]:
            return
        if self._stop_event.is_set() or self.channel_status.get(channel, {}).get("state") in {"disconnecting", "disconnected", "failed"}:
            return
        self._update_channel_state(channel, state)

    async def _watch_channel(self, channel, awaitable, generation: int | None = None) -> None:
        activity = self._channel_activity.get(channel)
        try:
            await awaitable
            if (not self._stop_event.is_set()
                    and self.channel_status[channel]["state"] != "disconnecting"
                    and (activity is None or activity.accepting)):
                raise RuntimeError("Channel stopped unexpectedly")
        except asyncio.CancelledError:
            raise
        except (Exception, SystemExit) as exc:
            if ((generation is None or generation == self._channel_generation[channel])
                    and not self._stop_event.is_set()
                    and self.channel_status[channel]["state"] != "disconnecting"
                    and (activity is None or activity.accepting)):
                self._channel_failed(channel, exc)
                self._schedule_channel_disconnect(channel, failed=True)
        finally:
            # A cleanup deadline changes state to failed, but the disconnect
            # intent remains in the activity. A late exit is still expected;
            # finish releasing it once the timed-out operation has returned.
            if (not self._stop_event.is_set()
                    and (generation is None or generation == self._channel_generation[channel])
                    and activity is not None and not activity.accepting
                    and self.channel_status[channel]["state"] != "disconnecting"):
                self._schedule_channel_disconnect(channel)

    def _schedule_channel_disconnect(self, channel: str, *, failed: bool = False) -> asyncio.Task:
        operation = self._channel_operations.get(channel)
        if operation is None or operation.done():
            operation = asyncio.create_task(self._disconnect_locked(channel, failed=failed))
            self._channel_operations[channel] = operation

            def finished(task):
                if not task.cancelled():
                    task.exception()
                if self.runtime_ready and not self._stop_event.is_set():
                    self._publish_status()

            operation.add_done_callback(finished)
        return operation

    def _require_channel_control(self, channel: str) -> None:
        if channel not in self._channel_locks:
            raise HTTPException(404, "Unknown channel")
        if not self.runtime_ready or self._stop_event.is_set():
            raise HTTPException(409, "Start the Agent before managing connections")

    async def connect_channel(self, channel: str) -> dict:
        self._require_channel_control(channel)
        async with self._channel_locks[channel]:
            self._require_channel_control(channel)
            operation = self._channel_operations.get(channel)
            if operation is not None and not operation.done():
                raise HTTPException(409, "Channel cleanup is still in progress")
            state = self.channel_status[channel]["state"]
            if state in {"connecting", "connected", "reconnecting"}:
                return self._channel_response(channel)
            if not self._channel_view(channel)["can_connect"]:
                ready, detail = self._channel_readiness(channel)
                if not ready:
                    raise HTTPException(422, detail)
                raise HTTPException(409, "Disconnect the previous connection before reconnecting")
            try:
                await self._start_channel(channel)
            except Exception as exc:
                self._channel_failed(channel, exc)
                await self._cleanup_channel(channel)
                raise HTTPException(503, "Channel could not start; check the runtime log") from exc
            return self._channel_response(channel)

    async def disconnect_channel(self, channel: str) -> dict:
        self._require_channel_control(channel)
        # A disconnected HTTP caller must not leave half-cleaned resources.
        operation = self._schedule_channel_disconnect(channel)
        await asyncio.shield(operation)
        return self._channel_response(channel)

    def _channel_response(self, channel: str) -> dict:
        return {"channel": {"id": channel, **self._channel_view(channel)}, "runtime": self.status()}

    def _channel_work(self, channel: str) -> set[asyncio.Task]:
        activity = self._channel_activity.get(channel)
        tasks = set(activity.tasks) if activity else set()
        if channel != "api" and self.scheduler is not None:
            tasks.update(self.scheduler.channel_tasks(channel))
        if channel == "api":
            device_stop = self._tasks.get("api-devices-stop")
            if device_stop is not None:
                tasks.add(device_stop)
        return {task for task in tasks if not task.done()}

    def _stop_devices(self, drain_timeout: float = 0) -> asyncio.Task | None:
        if self.device_service is None:
            return None
        self.device_service.quiesce()
        existing = self._tasks.get("api-devices-stop")
        if existing is not None and not existing.done():
            return existing
        if self.device_service.resources_closed:
            return None
        task = asyncio.create_task(self.device_service.stop(drain_timeout), name="xagent-devices-stop")
        self._tasks["api-devices-stop"] = task
        return task

    async def _disconnect_locked(self, channel: str, *, failed: bool = False) -> None:
        async with self._channel_locks[channel]:
            previous_error = self.channel_status[channel].get("error")
            self._update_channel_state(channel, "disconnecting")
            activity = self._channel_activity.get(channel)
            if activity is not None:
                activity.quiesce()
            adapter = self.adapters.get(channel) if channel != "api" else None
            if adapter is not None:
                adapter.quiesce()
            # Closing the public listening sockets blocks new requests without
            # touching local API turns or the private control server.
            if channel == "api":
                self._stop_devices(0.0 if failed else self.channel_drain_timeout)
                server = self._servers.get(channel)
                if server is not None:
                    for listener in getattr(server, "servers", []):
                        listener.close()
            deadline = time.monotonic() + (0.0 if failed else self.channel_drain_timeout)
            while time.monotonic() < deadline:
                work = self._channel_work(channel)
                if channel == "api":
                    server = self._servers.get(channel)
                    work |= set(server.server_state.tasks) if server else set()
                if not work:
                    break
                await asyncio.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            if channel != "api":
                self.agent.inbox.cancel_pending(channel=channel)
            for task in self._channel_work(channel):
                if task is not self._tasks.get("api-devices-stop"):
                    task.cancel()
            if not await self._cleanup_channel(channel):
                self._update_channel_state(channel, "failed", error="Connection cleanup did not finish; retry Disconnect")
                if failed:
                    return
                raise HTTPException(503, "Connection cleanup did not finish; retry Disconnect")
            if failed:
                self._update_channel_state(channel, "failed", error=previous_error or "Channel failed; check the runtime log")
            else:
                self._update_channel_state(channel, "disconnected")

    async def _cleanup_channel(self, channel: str) -> bool:
        deadline = time.monotonic() + self.channel_cleanup_timeout
        if channel == "api":
            self._stop_devices()
        adapter = self.adapters.get(channel) if channel != "api" else None
        server = self._servers.get(channel)
        if server is not None:
            server.should_exit = True
            # Cancel only requests owned by the public uvicorn server.
            for task in list(server.server_state.tasks):
                task.cancel()
        readiness = self._tasks.get(f"{channel}-readiness")
        if readiness is not None:
            readiness.cancel()
        stop_task = None
        if adapter is not None:
            stop_task = self._tasks.get(f"{channel}-stop")
            if stop_task is None or stop_task.done():
                stop_task = asyncio.create_task(adapter.stop())
                self._tasks[f"{channel}-stop"] = stop_task
        transport = self._tasks.get(channel)
        work = self._channel_work(channel)
        pending = work | {task for task in (transport, readiness, stop_task) if task is not None and not task.done()}
        if server is not None:
            pending |= {task for task in server.server_state.tasks if not task.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()))
        if not pending and server is not None and any(listener.is_serving() for listener in getattr(server, "servers", [])):
            # Uvicorn can return immediately after startup when should_exit was
            # set before bind completed. That path skips its normal shutdown.
            stop_task = self._tasks.get(f"{channel}-stop")
            if stop_task is None or stop_task.done():
                stop_task = asyncio.create_task(server.shutdown())
                self._tasks[f"{channel}-stop"] = stop_task
            _, pending = await asyncio.wait({stop_task}, timeout=max(0.0, deadline - time.monotonic()))
        # Voice / SDK threads can outlive cancelled asyncio wrappers.
        while not pending and adapter is not None and not adapter.resources_closed and time.monotonic() < deadline:
            await asyncio.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        clean = not pending and (adapter is None or adapter.resources_closed)
        if channel == "api" and self.device_service is not None:
            clean = clean and self.device_service.resources_closed
        if not clean:
            logger.warning("%s channel cleanup is waiting: tasks=%s adapter_resources_closed=%s",
                channel, sorted(task.get_name() for task in pending),
                adapter.resources_closed if adapter is not None else None)
            return False
        for task in (transport, readiness, stop_task):
            if task is not None and not task.cancelled():
                task.exception()
        if channel != "api":
            self.adapters.pop(channel, None)
        for key in (channel, f"{channel}-readiness", f"{channel}-stop"):
            self._tasks.pop(key, None)
        if channel == "api":
            device_stop = self._tasks.pop("api-devices-stop", None)
            if device_stop is not None and not device_stop.cancelled():
                device_stop.exception()
        self._servers.pop(channel, None)
        self._channel_activity.pop(channel, None)
        self._channel_generation[channel] += 1
        return True

    async def _start_channel(self, channel) -> None:
        config = self.runner.config
        self._channel_generation[channel] += 1
        generation = self._channel_generation[channel]
        activity = ChannelActivity()
        self._channel_activity[channel] = activity
        self._update_channel_state(channel, "connecting")
        if channel == "api":
            from ...interfaces.server import AgentHTTPServer
            from ...interfaces.cli.channels import api_config

            cfg = api_config(config)
            service = AgentHTTPServer(config_dir=str(self.config_dir), agent=self.agent,
                runtime_managed=True, api_adapter=self.api)
            self._api_app = service.app
            class PublicAdmission:
                def __init__(self, app):
                    self.app = app
                async def __call__(self, scope, receive, send):
                    if not activity.accepting:
                        if scope["type"] == "http":
                            from starlette.responses import JSONResponse
                            await JSONResponse({"detail": "API is disconnecting"}, status_code=503)(scope, receive, send)
                            return
                        if scope["type"] == "websocket":
                            if scope.get("path", "").startswith("/ws/devices/"):
                                # Accept then close with 1013, so a temporary
                                # drain isn't mistaken for HTTP 403 bad auth.
                                await send({"type": "websocket.accept"})
                            await send({"type": "websocket.close", "code": 1013})
                            return
                    await self.app(scope, receive, send)
            server = _HostedServer(uvicorn.Config(PublicAdmission(service.app),
                host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 8010)),
                lifespan="off", access_log=False, timeout_graceful_shutdown=10))
            self._servers["api"] = server
            run = server.serve()
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
            adapter.runtime_activity = activity
            loop = asyncio.get_running_loop()
            adapter.runtime_status = lambda state: loop.call_soon_threadsafe(self._channel_connection, channel, state, generation)
        self._tasks[channel] = asyncio.create_task(self._watch_channel(channel, run, generation), name=f"xagent-channel-{channel}")
        await asyncio.sleep(0)
        if channel == "api":
            server = self._servers["api"]
            async def ready():
                while not server.started:
                    if generation != self._channel_generation[channel] or self._tasks[channel].done():
                        return
                    await asyncio.sleep(0.01)
                self._channel_connection(channel, "connected", generation)
                if self.channel_status[channel]["state"] == "connected" and self.device_service is not None:
                    self.device_service.start()
            self._tasks["api-readiness"] = asyncio.create_task(ready())

    def _resolve_voice_adapter(self, target: Any, *, user_id: str = ""):
        target_dict = target if isinstance(target, dict) else {}
        if self.device_service is not None and self.device_service.voice_sessions:
            if target_dict.get("device_id") or target_dict.get("room_name") or str(user_id or "").startswith("device:"):
                session = self.device_service.resolve_voice_session(target_dict, user_id=user_id)
                if session is not None and getattr(session, "runtime", None) is not None:
                    return session.runtime
        local_voice = self.adapters.get("voice")
        if local_voice is not None and self.channel_status.get("voice", {}).get("state") == "connected":
            return local_voice
        if self.device_service is not None and self.device_service.voice_sessions:
            session = self.device_service.resolve_voice_session(target_dict, user_id=user_id)
            if session is not None and getattr(session, "runtime", None) is not None:
                return session.runtime
        return local_voice

    def _task_adapter(self, task):
        channel = task.delivery_channel
        if channel in {"", "local", "cli", "api"}:
            return self.api
        if channel == "voice":
            target = task.delivery.get("target") if isinstance(getattr(task, "delivery", None), dict) else {}
            return self._resolve_voice_adapter(target, user_id=getattr(task, "delivery_user_id", "") or "")
        return self.adapters.get(channel)

    def _can_handle_task(self, task) -> bool:
        adapter = self._task_adapter(task)
        if adapter is None:
            return False
        if task.delivery_channel not in {"", "local", "cli", "api"}:
            if adapter is self.adapters.get(task.delivery_channel) and self.channel_status.get(task.delivery_channel, {}).get("state") != "connected":
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
        channel = delivery.recipient.channel
        if channel == "voice":
            adapter = self._resolve_voice_adapter(
                delivery.recipient.target,
                user_id=delivery.recipient.user_id,
            )
        else:
            adapter = self.adapters.get(channel)
        if adapter is None:
            raise RuntimeError("Subconscious delivery channel is unavailable")
        if channel == "api":
            await adapter.deliver_subconscious_message(delivery)
            return
        if adapter is not self.adapters.get(channel):
            await adapter.deliver_subconscious_message(delivery)
            return
        if self.channel_status.get(channel, {}).get("state") != "connected":
            raise RuntimeError("Subconscious delivery channel is disconnected")
        task = self._channel_activity[channel].create_task(adapter.deliver_subconscious_message(delivery))
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise RuntimeError("Channel disconnected during delivery") from None

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

        @app.get("/runtime/ready")
        async def ready():
            return {"pid": os.getpid(), "state": self.state,
                    "runtime_ready": self.runtime_ready and not self._stop_event.is_set()}

        @app.post("/runtime/channels/{channel}/connect")
        async def connect(channel: str):
            return await self.connect_channel(channel)

        @app.post("/runtime/channels/{channel}/disconnect")
        async def disconnect(channel: str):
            return await self.disconnect_channel(channel)

        @app.post("/runtime/stop")
        async def stop(payload: RuntimeStopInput | None = None):
            if not self._stop_event.is_set():
                self._restarting = bool(payload and payload.restart)
                if self._restarting:
                    from ...interfaces.cli.channels import restart_channels_from_status
                    # Capture intent and close admission without yielding. A
                    # queued Disconnect must also win over automatic restoration.
                    self._restart_channels = [
                        channel for channel in restart_channels_from_status({"channels": self.channel_status}) or []
                        if not (operation := self._channel_operations.get(channel)) or operation.done()
                    ]
                self._stop_event.set()
            response = {"stopping": True}
            if self._restart_channels is not None:
                response["restart_channels"] = list(self._restart_channels)
            return response

        @app.post("/runtime/turns/stop")
        async def abort(payload: TurnControl):
            if not payload.turn_id:
                raise HTTPException(422, "turn_id is required")
            return {"stopped": self.agent.abort(payload.turn_id, channel=payload.channel)}

        @app.post("/runtime/turns/steer")
        async def steer(payload: TurnControl):
            return {"steered": await self.agent.offer_steer(
                payload.turn_id,
                payload.content,
                channel=payload.channel,
                user_id=payload.user_id,
                room_name=payload.room_name,
            )}

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
                stopping = asyncio.create_task(self._stop_event.wait())
                incoming = None
                try:
                    while not self._stop_event.is_set() and not await request.is_disconnected():
                        incoming = asyncio.create_task(queue.get())
                        done, _ = await asyncio.wait({incoming, stopping}, timeout=15.0,
                            return_when=asyncio.FIRST_COMPLETED)
                        if stopping in done:
                            break
                        if incoming in done:
                            payload = incoming.result()
                        else:
                            incoming.cancel()
                            await asyncio.gather(incoming, return_exceptions=True)
                            payload = {"type": "keepalive"}
                        yield json.dumps(payload, ensure_ascii=False) + "\n"
                finally:
                    stopping.cancel()
                    if incoming is not None:
                        incoming.cancel()
                    await asyncio.gather(stopping, *([incoming] if incoming is not None else []), return_exceptions=True)
                    await self.api.delivery.unregister_subscriber(user_id, subscriber)
            return StreamingResponse(events(), media_type="application/x-ndjson")
        return app

    async def _shutdown(self):
        shutdown_started = time.monotonic()
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
            device_stop = self._stop_devices()
            if self.agent is not None:
                self.agent.inbox.stop_accepting()
                self.agent.inbox.cancel_pending()
                self.agent.abort()
            cleanup = set(self._tasks.values()) | set(self._channel_operations.values()) | {drain}
            for channel in self._channel_activity:
                cleanup.update(self._channel_work(channel))
            for task in cleanup:
                if not task.done() and task is not device_stop:
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
            self.lifecycle_timings["shutdown_seconds"] = round(time.monotonic() - shutdown_started, 3)
            logger.info("Runtime %s completed in %.3fs", "restart shutdown" if self._restarting else "shutdown",
                        self.lifecycle_timings["shutdown_seconds"])
            self._publish_status()

    async def _drain(self):
        self._stop_event.set()
        self.runtime_ready = False
        self.state = "stopping"
        self._publish_status()
        if self.agent is not None:
            self.agent.inbox.stop_accepting()
        for activity in self._channel_activity.values():
            activity.quiesce()
        for name, adapter in list(self.adapters.items()):
            if name != "api":
                adapter.quiesce()
        operations = {task for task in self._channel_operations.values() if not task.done()}
        if operations:
            await asyncio.wait(operations, timeout=1.0)
            for task in operations:
                if not task.done():
                    task.cancel()
        # These owners are independent. Start their cleanup together with the
        # accepted-turn drain, instead of adding each timeout to the next one.
        for name, owner in (("heartbeat", self.heartbeat), ("scheduler", self.scheduler)):
            if owner is not None:
                self._tasks[f"{name}-stop"] = asyncio.create_task(owner.stop())
        if self.agent is not None:
            deadline = time.monotonic() + 15.0
            while (self.agent.inbox.busy or self.agent.inbox.pending_count) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            if self.agent.inbox.busy or self.agent.inbox.pending_count:
                self.agent.abort()
                self.agent.inbox.cancel_pending()
        self._stop_devices()
        for name, adapter in list(self.adapters.items()):
            stop = getattr(adapter, "stop", None)
            if callable(stop):
                existing = self._tasks.get(f"{name}-stop")
                if existing is None or existing.done():
                    self._tasks[f"{name}-stop"] = asyncio.create_task(stop())
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
            if not self._restarting:
                try:
                    await asyncio.wait_for(self.agent.memory_handler.run_maintenance(force=True, trigger="shutdown"), timeout=10.0)
                except Exception:
                    logger.warning("Final memory maintenance did not complete", exc_info=True)
            # Turn receipts are finalized once by _shutdown; startup recovers
            # prepared diary writes. Restart never starts a new model turn.
            close = getattr(getattr(self.agent, "client", None), "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=3.0)
