"""Authenticated loopback control plane for one AgentRuntime."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from .channels import ChannelManager
from .engine import AgentRuntime
from .scheduling import RuntimeTaskStore
from .state import RuntimeStateStore
from .types import (
    DELIVERY_CHANNELS,
    LOCAL_OWNER_PERSON_ID,
    MAX_EVENT_CONTENT_BYTES,
    AgentEvent,
)


_MEMORY_SCOPES = {"all", "daily", "weekly", "monthly", "yearly"}
_MAX_MEMORY_FILE_BYTES = 2 * 1024 * 1024
_MAX_MEMORY_SCAN_BYTES = 8 * 1024 * 1024
_MAX_MEMORY_FILES = 1_000


class ControlEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str | None = Field(default=None, max_length=256)
    kind: str = Field(default="chat", min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)
    conversation_id: str = Field(default="", max_length=512)
    speaker_id: str = Field(min_length=1, max_length=512)
    audience_ids: list[str] = Field(default_factory=list, max_length=64)
    content: str = Field(max_length=MAX_EVENT_CONTENT_BYTES)
    timestamp: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    wait: bool = True

    def to_event(self) -> AgentEvent:
        return AgentEvent.create(
            event_id=self.event_id,
            kind=self.kind,
            source=self.source,
            conversation_id=self.conversation_id,
            speaker_id=self.speaker_id,
            audience_ids=tuple(self.audience_ids),
            content=self.content,
            timestamp=self.timestamp,
            metadata=self.metadata,
        )


class AccountLinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=512)


class TaskDestinationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Literal["api", "feishu", "weixin", "voice"]
    target: dict[str, Any] = Field(default_factory=dict)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=MAX_EVENT_CONTENT_BYTES)
    schedule: dict[str, Any]
    destination: TaskDestinationInput | None = None
    created_source: str = Field(default="control", min_length=1, max_length=64)
    created_by: str = Field(
        default=LOCAL_OWNER_PERSON_ID,
        min_length=1,
        max_length=512,
    )


class TaskUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_EVENT_CONTENT_BYTES,
    )
    schedule: dict[str, Any] | None = None
    destination: TaskDestinationInput | None = None


class RuntimeLease:
    """Non-blocking process lease preventing two active brains for one agent."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError("another xAgent runtime already owns this agent") from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


class RuntimeControlServer:
    """Serve local lifecycle, event, and delivery operations."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        channels: ChannelManager,
        state: RuntimeStateStore,
        tasks: RuntimeTaskStore,
        runtime_dir: str | Path,
        shutdown: Callable[[], Awaitable[None]] | None = None,
        delivery_wakeup: Callable[[], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.channels = channels
        self.state = state
        self.tasks = tasks
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.run_dir = self.runtime_dir / "run"
        self.info_path = self.run_dir / "runtime.json"
        self.lease = RuntimeLease(self.run_dir / "runtime.lock")
        self.token = secrets.token_urlsafe(32)
        self.instance_id = uuid.uuid4().hex
        self.started_at = time.time()
        self.shutdown_callback = shutdown
        self.delivery_wakeup = delivery_wakeup
        self.app = FastAPI(title="xAgent Local Runtime Control", docs_url=None, redoc_url=None)
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._register_routes()

    async def start(self) -> None:
        if self._server_task is not None:
            return
        if self.run_dir.is_symlink():
            raise RuntimeError(
                f"Runtime directory must not be a symbolic link: {self.run_dir}"
            )
        if self.run_dir.exists() and not self.run_dir.is_dir():
            raise RuntimeError(f"Runtime path is not a directory: {self.run_dir}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.run_dir.chmod(0o700)
        self.lease.acquire()
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        self._server = server
        self._server_task = asyncio.create_task(server.serve(), name="xagent-control-plane")
        try:
            for _ in range(500):
                if server.started and server.servers:
                    break
                if self._server_task.done():
                    await self._server_task
                    raise RuntimeError("runtime control server exited during startup")
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("runtime control server did not start")
            socket = server.servers[0].sockets[0]
            port = int(socket.getsockname()[1])
            self._write_info(port)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        server = self._server
        task = self._server_task
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._server = None
        self._server_task = None
        self._remove_own_info()
        self.lease.release()

    def _write_info(self, port: int) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "instance_id": self.instance_id,
            "control_url": f"http://127.0.0.1:{port}",
            "token": self.token,
            "started_at": self.started_at,
        }
        temporary = self.info_path.with_name(f".{self.info_path.name}.{self.instance_id}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.info_path)

    def _remove_own_info(self) -> None:
        try:
            payload = json.loads(self.info_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return
        if payload.get("instance_id") != self.instance_id:
            return
        try:
            self.info_path.unlink()
        except FileNotFoundError:
            pass

    def _register_routes(self) -> None:
        async def authorize(authorization: str | None = Header(default=None)) -> None:
            prefix = "Bearer "
            supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
            if not hmac.compare_digest(supplied, self.token):
                raise HTTPException(status_code=401, detail="invalid runtime token")

        auth = Depends(authorize)

        @self.app.get("/v1/runtime", dependencies=[auth])
        async def runtime_status():
            return {
                "pid": os.getpid(),
                "instance_id": self.instance_id,
                "started_at": self.started_at,
                "running": self.runtime.running,
                "channels": self.channels.snapshot(),
            }

        @self.app.get("/v1/overview", dependencies=[auth])
        async def overview():
            payload = await self.state.overview()
            memory = await asyncio.to_thread(
                self._memory_index_sync,
                "all",
                "",
                6,
            )
            payload["runtime"] = {
                "pid": os.getpid(),
                "instance_id": self.instance_id,
                "started_at": self.started_at,
                "uptime_seconds": max(0.0, time.time() - self.started_at),
                "running": self.runtime.running,
                "channels": self.channels.snapshot(),
            }
            payload["counts"]["memory_files"] = memory["total"]
            payload["recent_memory"] = memory["entries"]
            return payload

        @self.app.post("/v1/runtime/stop", dependencies=[auth])
        async def stop_runtime():
            if self.shutdown_callback is None:
                raise HTTPException(status_code=503, detail="runtime shutdown is unavailable")
            asyncio.create_task(self.shutdown_callback())
            return {"status": "stopping"}

        @self.app.get("/v1/channels", dependencies=[auth])
        async def list_channels():
            return {"channels": self.channels.snapshot()}

        @self.app.post("/v1/channels/{name}/{action}", dependencies=[auth])
        async def change_channel(name: str, action: str):
            try:
                if action == "start":
                    value = await self.channels.start(name)
                elif action == "stop":
                    value = await self.channels.stop(name)
                elif action == "restart":
                    value = await self.channels.restart(name)
                else:
                    raise HTTPException(status_code=404, detail="unknown channel action")
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"channel": value}

        @self.app.post("/v1/events", dependencies=[auth])
        async def submit_event(input_data: ControlEventInput):
            event = await self._canonical_event(input_data.to_event())
            if input_data.wait:
                return {
                    "event_id": event.event_id,
                    "result": await self.runtime.submit_and_wait(event),
                }
            return {"event_id": await self.runtime.submit(event)}

        @self.app.websocket("/v1/events/stream")
        async def stream_event(websocket: WebSocket):
            supplied = websocket.headers.get("authorization", "")
            query_token = websocket.query_params.get("token", "")
            header_token = supplied[7:] if supplied.startswith("Bearer ") else ""
            if not (
                hmac.compare_digest(header_token, self.token)
                or hmac.compare_digest(query_token, self.token)
            ):
                await websocket.close(code=4401)
                return
            await websocket.accept()
            try:
                input_data = ControlEventInput.model_validate(await websocket.receive_json())
                event = await self._canonical_event(input_data.to_event())
                async for item in self.runtime.stream(event):
                    await websocket.send_json(item)
            finally:
                await websocket.close()

        @self.app.get("/v1/deliveries", dependencies=[auth])
        async def list_deliveries(status: str | None = Query(default=None)):
            try:
                deliveries = await self.state.list_deliveries(status=status)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"deliveries": [asdict(delivery) for delivery in deliveries]}

        @self.app.get("/v1/messages", dependencies=[auth])
        async def list_messages(
            limit: int = Query(default=50, ge=1, le=100),
            offset: int = Query(default=0, ge=0, le=100_000),
            q: str = Query(default="", max_length=256),
            role: str = Query(default="", max_length=32),
            source: str = Query(default="", max_length=64),
        ):
            try:
                return await self.state.list_messages(
                    limit=limit,
                    offset=offset,
                    query=q,
                    role=role,
                    source=source,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @self.app.get("/v1/memory", dependencies=[auth])
        async def list_memory(
            scope: str = Query(default="all", max_length=16),
            q: str = Query(default="", max_length=256),
            limit: int = Query(default=200, ge=1, le=500),
        ):
            try:
                return await asyncio.to_thread(
                    self._memory_index_sync,
                    scope,
                    q,
                    limit,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @self.app.get("/v1/memory/file", dependencies=[auth])
        async def read_memory_file(
            path: str = Query(min_length=1, max_length=512),
        ):
            try:
                return await asyncio.to_thread(self._read_memory_file_sync, path)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @self.app.post("/v1/deliveries/{delivery_id}/retry", dependencies=[auth])
        async def retry_delivery(delivery_id: str):
            try:
                delivery = await self.state.retry_blocked_delivery(delivery_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if self.delivery_wakeup is not None:
                self.delivery_wakeup()
            return {"delivery": asdict(delivery)}

        @self.app.get("/v1/tasks", dependencies=[auth])
        async def list_tasks(status: str | None = Query(default=None)):
            try:
                tasks = await self.tasks.list(status=status)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"tasks": [asdict(task) for task in tasks]}

        @self.app.post("/v1/tasks", dependencies=[auth], status_code=201)
        async def create_task(input_data: TaskInput):
            try:
                task = await self.tasks.create(**input_data.model_dump())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"task": asdict(task)}

        @self.app.patch("/v1/tasks/{task_id}", dependencies=[auth])
        async def update_task(task_id: str, input_data: TaskUpdateInput):
            try:
                task = await self.tasks.update(
                    task_id,
                    **input_data.model_dump(exclude_unset=True),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"task": asdict(task)}

        @self.app.post("/v1/tasks/{task_id}/{action}", dependencies=[auth])
        async def change_task_status(task_id: str, action: str):
            if action not in {"pause", "resume"}:
                raise HTTPException(status_code=404, detail="unknown task action")
            try:
                task = await self.tasks.set_status(
                    task_id,
                    "paused" if action == "pause" else "active",
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"task": asdict(task)}

        @self.app.delete("/v1/tasks/{task_id}", dependencies=[auth])
        async def delete_task(task_id: str):
            try:
                task = await self.tasks.delete(task_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return {"task": asdict(task)}

        @self.app.get("/v1/people", dependencies=[auth])
        async def list_people():
            return {"people": await self.state.list_people()}

        @self.app.post("/v1/people/{person_id}/accounts", dependencies=[auth])
        async def link_account(person_id: str, input_data: AccountLinkInput):
            try:
                account = await self.state.link_account(
                    person_id=person_id,
                    **input_data.model_dump(),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"account": account}

    def _memory_root(self) -> Path:
        return (self.runtime_dir / "memory").resolve()

    def _memory_index_sync(
        self,
        scope: str,
        query: str,
        limit: int,
    ) -> dict[str, Any]:
        normalized_scope = str(scope or "all").strip().lower()
        normalized_query = str(query or "").strip()
        if normalized_scope not in _MEMORY_SCOPES:
            raise ValueError(f"unknown memory scope: {normalized_scope}")
        if len(normalized_query) > 256:
            raise ValueError("memory search query must not exceed 256 characters")
        normalized_limit = max(1, min(int(limit), 500))
        root = self._memory_root()
        search_root = root if normalized_scope == "all" else root / normalized_scope
        if not search_root.is_dir():
            return {
                "entries": [],
                "total": 0,
                "scope": normalized_scope,
                "query": normalized_query,
            }

        candidates = sorted(
            (
                path
                for path in search_root.rglob("*.md")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:_MAX_MEMORY_FILES]
        needle = normalized_query.casefold()
        scanned_bytes = 0
        entries: list[dict[str, Any]] = []
        total = 0
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > _MAX_MEMORY_FILE_BYTES:
                continue
            if scanned_bytes + stat.st_size > _MAX_MEMORY_SCAN_BYTES:
                break
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned_bytes += stat.st_size
            relative = path.relative_to(root).as_posix()
            if needle and needle not in relative.casefold() and needle not in content.casefold():
                continue
            total += 1
            if len(entries) >= normalized_limit:
                continue
            entries.append(
                {
                    "path": relative,
                    "scope": relative.split("/", 1)[0],
                    "title": _memory_title(path, content),
                    "excerpt": _memory_excerpt(content, normalized_query),
                    "modified_at": stat.st_mtime,
                    "size_bytes": stat.st_size,
                }
            )
        return {
            "entries": entries,
            "total": total,
            "scope": normalized_scope,
            "query": normalized_query,
        }

    def _read_memory_file_sync(self, relative_path: str) -> dict[str, Any]:
        root = self._memory_root()
        candidate = Path(str(relative_path or ""))
        if candidate.is_absolute():
            raise ValueError("memory path must be relative")
        path = (root / candidate).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != ".md":
            raise ValueError("memory path must reference a Markdown diary file")
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("memory file does not exist")
        stat = path.stat()
        if stat.st_size > _MAX_MEMORY_FILE_BYTES:
            raise ValueError("memory file exceeds the 2 MiB read limit")
        content = path.read_text(encoding="utf-8", errors="replace")
        return {
            "path": path.relative_to(root).as_posix(),
            "title": _memory_title(path, content),
            "content": content,
            "modified_at": stat.st_mtime,
            "size_bytes": stat.st_size,
        }

    async def _canonical_event(self, event: AgentEvent) -> AgentEvent:
        if event.kind != "chat":
            return event
        if event.source in {"web", "cli"}:
            return replace(
                event,
                speaker_id=LOCAL_OWNER_PERSON_ID,
                conversation_id=event.conversation_id or f"{event.source}:main",
                audience_ids=(LOCAL_OWNER_PERSON_ID,),
            )
        if event.source not in DELIVERY_CHANNELS:
            return event
        account_id = event.speaker_id
        person_id = await self.state.resolve_person(event.source, account_id)
        audience: list[str] = []
        for account in event.audience_ids:
            audience.append(await self.state.resolve_person(event.source, account))
        audience_ids = tuple(audience) or (person_id,)
        metadata = dict(event.metadata)
        metadata["channel_account_id"] = account_id
        return replace(
            event,
            speaker_id=person_id,
            conversation_id=event.conversation_id or f"{event.source}:{account_id}",
            audience_ids=audience_ids,
            metadata=metadata,
        )


def _memory_title(path: Path, content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:120]
    return path.stem.replace("_", " ")


def _memory_excerpt(content: str, query: str) -> str:
    compact = " ".join(line.strip() for line in content.splitlines() if line.strip())
    if not compact:
        return ""
    needle = query.strip().casefold()
    if needle:
        index = compact.casefold().find(needle)
        if index >= 0:
            start = max(0, index - 80)
            end = min(len(compact), index + len(needle) + 140)
            prefix = "…" if start else ""
            suffix = "…" if end < len(compact) else ""
            return f"{prefix}{compact[start:end]}{suffix}"
    return compact[:220] + ("…" if len(compact) > 220 else "")
