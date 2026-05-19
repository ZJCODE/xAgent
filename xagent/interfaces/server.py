import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from posthog import host
import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError

from .base import BaseAgentConfig, BaseAgentRunner
from ..core.agent import Agent
from ..core.config import AgentConfig
from ..core.runtime import create_runtime_heartbeat
from ..core.time import (
    format_in_timezone,
    format_utc,
    resolve_timezone,
    timezone_name,
    utc_offset_text,
)

_STATIC_DIR = Path(__file__).parent / "static"


class ChatInput(BaseModel):
    """Final-only request body for the HTTP chat endpoint."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    user_message: str
    image_source: Optional[Union[str, List[str]]] = None
    history_count: Optional[int] = AgentConfig.DEFAULT_HISTORY_COUNT
    max_iter: Optional[int] = AgentConfig.DEFAULT_MAX_ITER
    max_concurrent_tools: Optional[int] = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
    enable_memory: Optional[bool] = True
    private: Optional[bool] = False
    timezone: Optional[str] = None


class AgentInput(ChatInput):
    """Event request body for WebSocket chat."""

    stream: Optional[bool] = False


class ObserveInput(BaseModel):
    """Request body for observation endpoint."""

    context: str
    source: Optional[str] = "environment"
    event_type: Optional[str] = "observation"
    metadata: Optional[Dict[str, Any]] = None


class IdentityInput(BaseModel):
    """Request body for updating identity.md."""

    identity: str


class MemoryCorrectInput(BaseModel):
    correction: str
    reason: str = "manual correction"
    query: Optional[str] = None


class MemoryForgetInput(BaseModel):
    mode: str = "archive"
    reason: str = "forget requested"


class MemoryExportInput(BaseModel):
    output_dir: Optional[str] = None


class MemoryQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str
    max_rows: int = 50


class AgentHTTPServer(BaseAgentRunner):
    """HTTP server for xAgent."""

    def __init__(
        self,
        config_dir: Optional[str] = None,
        agent: Optional[Agent] = None,
        enable_web: bool = True,
        max_concurrent_chats: int = AgentConfig.DEFAULT_HTTP_MAX_CONCURRENT_CHATS,
        chat_queue_timeout: float = AgentConfig.DEFAULT_HTTP_QUEUE_TIMEOUT,
        chat_timeout: float = AgentConfig.DEFAULT_HTTP_CHAT_TIMEOUT,
    ):
        self._enable_web = enable_web
        self._chat_semaphore = asyncio.Semaphore(max(1, int(max_concurrent_chats)))
        self._chat_queue_timeout = max(0.001, float(chat_queue_timeout))
        self._chat_timeout = max(0.001, float(chat_timeout))

        if agent is not None:
            self.agent = agent
            self.config = {}
            self.message_storage = self.agent.message_storage
        else:
            super().__init__(config_dir=config_dir)

        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.app = self._create_app()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    async def _acquire_chat_slot(self) -> None:
        try:
            await asyncio.wait_for(
                self._chat_semaphore.acquire(),
                timeout=self._chat_queue_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=429,
                detail="Too many concurrent chat requests; try again later.",
            ) from exc

    async def _call_agent(self, input_data: ChatInput):
        return await self.agent(
            user_message=input_data.user_message,
            user_id=input_data.user_id,
            history_count=input_data.history_count,
            max_iter=input_data.max_iter,
            max_concurrent_tools=input_data.max_concurrent_tools,
            image_source=input_data.image_source,
            enable_memory=input_data.enable_memory,
            private=input_data.private,
            timezone=input_data.timezone,
        )

    async def _call_observe(self, input_data: ObserveInput):
        return await self.agent.observe(
            context=input_data.context,
            source=input_data.source or "environment",
            event_type=input_data.event_type or "observation",
            metadata=input_data.metadata,
        )

    async def _run_chat_with_limits(self, input_data: ChatInput):
        await self._acquire_chat_slot()
        try:
            deadline = time.monotonic() + self._chat_timeout
            return await self._await_before_deadline(
                self._call_agent(input_data),
                deadline,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent chat timed out.") from exc
        finally:
            self._chat_semaphore.release()

    async def _run_observe_with_limits(self, input_data: ObserveInput):
        await self._acquire_chat_slot()
        try:
            deadline = time.monotonic() + self._chat_timeout
            return await self._await_before_deadline(
                self._call_observe(input_data),
                deadline,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent observe timed out.") from exc
        finally:
            self._chat_semaphore.release()

    async def _chat_event_stream(self, input_data: AgentInput):
        acquired = False
        done_sent = False
        try:
            await self._acquire_chat_slot()
            acquired = True
            deadline = time.monotonic() + self._chat_timeout

            chat_events = getattr(self.agent, "chat_events", None)
            if not callable(chat_events):
                raise RuntimeError("Agent does not support chat_events().")

            response = chat_events(
                user_message=input_data.user_message,
                user_id=input_data.user_id,
                history_count=input_data.history_count,
                max_iter=input_data.max_iter,
                max_concurrent_tools=input_data.max_concurrent_tools,
                image_source=input_data.image_source,
                stream=bool(input_data.stream),
                enable_memory=input_data.enable_memory,
                private=input_data.private,
                timezone=input_data.timezone,
            )
            async for event in self._iterate_before_deadline(response, deadline):
                if event.get("type") == "done":
                    done_sent = True
                yield event
        except HTTPException as exc:
            self.logger.warning("WebSocket chat rejected for %s: %s", input_data.user_id, exc.detail)
            yield {"type": "error", "error": exc.detail, "status_code": exc.status_code}
        except asyncio.TimeoutError:
            self.logger.error("WebSocket chat timed out for %s", input_data.user_id)
            yield {"type": "error", "error": "Agent chat timed out.", "status_code": 504}
        except Exception as exc:
            self.logger.error("WebSocket chat event error for %s: %s", input_data.user_id, exc)
            yield {"type": "error", "error": str(exc)}
        finally:
            if acquired:
                self._chat_semaphore.release()
        if not done_sent:
            yield {"type": "done"}

    async def _send_websocket_chat_events(self, websocket: WebSocket, input_data: AgentInput) -> None:
        async for event in self._chat_event_stream(input_data):
            await websocket.send_json(event)

    async def _send_websocket_observe_events(self, websocket: WebSocket, input_data: ObserveInput) -> None:
        try:
            response = await self._run_observe_with_limits(input_data)
            await websocket.send_json({
                "type": "result",
                "result": self._response_payload(response),
            })
        except HTTPException as exc:
            self.logger.warning(
                "WebSocket observe rejected: source=%s type=%s detail=%s",
                input_data.source,
                input_data.event_type,
                exc.detail,
            )
            await websocket.send_json({
                "type": "error",
                "error": exc.detail,
                "status_code": exc.status_code,
            })
        except Exception as exc:
            self.logger.error(
                "WebSocket observe error: source=%s type=%s error=%s",
                input_data.source,
                input_data.event_type,
                exc,
            )
            await websocket.send_json({
                "type": "error",
                "error": f"Agent observe error: {str(exc)}",
            })
        finally:
            await websocket.send_json({"type": "done"})

    async def _send_websocket_error(
        self,
        websocket: WebSocket,
        error: str,
        *,
        status_code: Optional[int] = None,
        details: Optional[Any] = None,
    ) -> None:
        payload: Dict[str, Any] = {"type": "error", "error": error}
        if status_code is not None:
            payload["status_code"] = status_code
        if details is not None:
            payload["details"] = details
        await websocket.send_json(payload)
        await websocket.send_json({"type": "done"})

    async def _await_before_deadline(self, awaitable, deadline: float):
        return await asyncio.wait_for(awaitable, timeout=self._remaining_time(deadline))

    async def _iterate_before_deadline(self, response, deadline: float):
        iterator = response.__aiter__()
        while True:
            try:
                yield await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=self._remaining_time(deadline),
                )
            except StopAsyncIteration:
                break

    @staticmethod
    def _response_payload(response):
        if hasattr(response, "model_dump"):
            return response.model_dump()
        return str(response)

    @staticmethod
    def _remaining_time(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    def _get_memory_store(self):
        memory = getattr(self.agent, "memory_store", None)
        if memory is None:
            raise HTTPException(status_code=500, detail="Memory storage is unavailable")
        return memory

    def _get_memory_db_path(self) -> Path:
        memory = self._get_memory_store()
        memory_path = getattr(memory, "path", None)
        if memory_path is None:
            raise HTTPException(status_code=500, detail="Memory database path is unavailable")
        return Path(memory_path).expanduser().resolve()

    def _get_identity_path(self) -> Path:
        identity_path = getattr(self, "identity_path", None)
        if identity_path is None:
            raise HTTPException(status_code=500, detail="Identity file path is unavailable")
        return Path(identity_path).expanduser().resolve()

    def _get_agent_identity(self) -> str:
        identity = getattr(self.agent, "identity", None)
        if identity is None:
            identity = getattr(self.agent, "system_prompt", "")
        return identity or ""

    def _set_agent_identity(self, identity: str) -> None:
        if hasattr(self.agent, "set_identity"):
            self.agent.set_identity(identity)
        else:
            self.agent.system_prompt = identity
            message_handler = getattr(self.agent, "message_handler", None)
            if message_handler is not None:
                message_handler.system_prompt = identity
        self.identity = identity

    def _resolve_request_timezone(self, request_timezone: Optional[str] = None):
        return resolve_timezone(self.config if isinstance(self.config, dict) else None, request_timezone)

    def _message_payload(self, msg, request_timezone: Optional[str] = None) -> Dict[str, Any]:
        active_timezone = self._resolve_request_timezone(request_timezone)
        timestamp = float(msg.timestamp)
        item = {
            "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
            "type": msg.type.value if hasattr(msg.type, "value") else str(msg.type),
            "content": msg.content,
            "sender_id": msg.sender_id,
            "timestamp": timestamp,
            "timestamp_utc": format_utc(timestamp),
            "timestamp_local": format_in_timezone(timestamp, active_timezone),
            "timezone": timezone_name(active_timezone),
            "utc_offset": utc_offset_text(timestamp, active_timezone),
            "metadata": msg.metadata,
        }
        if msg.tool_call:
            item["tool_call"] = {
                "name": msg.tool_call.name,
                "arguments": msg.tool_call.arguments,
                "output": msg.tool_call.output,
            }
        return item

    @staticmethod
    def _decode_memory_json(value: Any, default: Any):
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default

    async def _build_memory_dashboard(self, preview_limit: int = 60) -> Dict[str, Any]:
        store = self._get_memory_store()
        normalized_limit = max(8, min(int(preview_limit or 0), 200))

        (
            stats,
            memory_statuses,
            memory_kinds,
            subject_types,
            memory_sensitivities,
            event_roles,
            event_types,
            policy_count,
            memory_result,
            event_result,
            people_result,
            summary_result,
            revision_result,
            evidence_result,
            policy_result,
        ) = await asyncio.gather(
            store.get_stats(),
            store.query_sql(
                "SELECT status AS label, COUNT(*) AS count FROM memory_items "
                "GROUP BY status ORDER BY count DESC, status ASC",
                max_rows=20,
            ),
            store.query_sql(
                "SELECT kind AS label, COUNT(*) AS count FROM memory_items "
                "GROUP BY kind ORDER BY count DESC, kind ASC",
                max_rows=20,
            ),
            store.query_sql(
                "SELECT subject_type AS label, COUNT(*) AS count FROM memory_items "
                "GROUP BY subject_type ORDER BY count DESC, subject_type ASC",
                max_rows=20,
            ),
            store.query_sql(
                "SELECT sensitivity AS label, COUNT(*) AS count FROM memory_items "
                "GROUP BY sensitivity ORDER BY count DESC, sensitivity ASC",
                max_rows=20,
            ),
            store.query_sql(
                "SELECT role AS label, COUNT(*) AS count FROM events "
                "GROUP BY role ORDER BY count DESC, role ASC",
                max_rows=20,
            ),
            store.query_sql(
                "SELECT event_type AS label, COUNT(*) AS count FROM events "
                "GROUP BY event_type ORDER BY count DESC, event_type ASC",
                max_rows=20,
            ),
            store.query_sql("SELECT COUNT(*) AS count FROM retention_policies", max_rows=1),
            store.list_memory_items(limit=normalized_limit),
            store.get_events(limit=normalized_limit),
            store.query_sql(
                "SELECT person_key, display_name, aliases_json, relationship, notes, created_at, updated_at "
                f"FROM people ORDER BY updated_at DESC, person_key ASC LIMIT {normalized_limit}",
                max_rows=normalized_limit,
            ),
            store.query_sql(
                "SELECT id, summary_type, scope_type, scope_key, period_start, period_end, "
                "content, source_memory_ids_json, created_at "
                f"FROM memory_summaries ORDER BY created_at DESC, id DESC LIMIT {normalized_limit}",
                max_rows=normalized_limit,
            ),
            store.query_sql(
                "SELECT id, memory_id, revision_type, old_content, new_content, reason, actor, created_at "
                f"FROM memory_revisions ORDER BY created_at DESC, id DESC LIMIT {normalized_limit}",
                max_rows=normalized_limit,
            ),
            store.query_sql(
                "SELECT id, memory_id, event_id, quote, relation, confidence, extractor_model, created_at "
                f"FROM memory_evidence ORDER BY created_at DESC, id DESC LIMIT {normalized_limit}",
                max_rows=normalized_limit,
            ),
            store.query_sql(
                "SELECT id, scope_type, scope_key, policy, ttl_days, created_at "
                f"FROM retention_policies ORDER BY created_at DESC, id DESC LIMIT {normalized_limit}",
                max_rows=normalized_limit,
            ),
        )

        policy_total_rows = policy_count.get("rows", []) if isinstance(policy_count, dict) else []
        retention_policy_total = int(policy_total_rows[0].get("count", 0)) if policy_total_rows else 0
        stats = dict(stats)
        stats["retention_policies"] = retention_policy_total

        people_items = []
        for row in people_result.get("rows", []):
            people_items.append(
                {
                    "person_key": row.get("person_key"),
                    "display_name": row.get("display_name"),
                    "aliases": self._decode_memory_json(row.get("aliases_json"), []),
                    "relationship": row.get("relationship"),
                    "notes": row.get("notes"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )

        summary_items = []
        for row in summary_result.get("rows", []):
            summary_items.append(
                {
                    "summary_id": row.get("id"),
                    "summary_type": row.get("summary_type"),
                    "scope_type": row.get("scope_type"),
                    "scope_key": row.get("scope_key"),
                    "period_start": row.get("period_start"),
                    "period_end": row.get("period_end"),
                    "content": row.get("content"),
                    "source_memory_ids": self._decode_memory_json(row.get("source_memory_ids_json"), []),
                    "created_at": row.get("created_at"),
                }
            )

        memory_items = memory_result.get("items", []) if isinstance(memory_result, dict) else []
        event_items = event_result.get("events", []) if isinstance(event_result, dict) else []
        revision_items = revision_result.get("rows", []) if isinstance(revision_result, dict) else []
        evidence_items = evidence_result.get("rows", []) if isinstance(evidence_result, dict) else []
        policy_items = policy_result.get("rows", []) if isinstance(policy_result, dict) else []

        return {
            "status": "ok",
            "generated_at": time.time(),
            "preview_limit": normalized_limit,
            "stats": stats,
            "breakdowns": {
                "memory_status": memory_statuses.get("rows", []),
                "memory_kind": memory_kinds.get("rows", []),
                "subject_type": subject_types.get("rows", []),
                "memory_sensitivity": memory_sensitivities.get("rows", []),
                "event_role": event_roles.get("rows", []),
                "event_type": event_types.get("rows", []),
            },
            "collections": {
                "memories": {
                    "items": memory_items,
                    "count": len(memory_items),
                    "total": int(stats.get("memory_items", 0)),
                    "truncated": int(stats.get("memory_items", 0)) > len(memory_items),
                },
                "events": {
                    "items": event_items,
                    "count": len(event_items),
                    "total": int(stats.get("events", 0)),
                    "truncated": int(stats.get("events", 0)) > len(event_items),
                },
                "people": {
                    "items": people_items,
                    "count": len(people_items),
                    "total": int(stats.get("people", 0)),
                    "truncated": int(stats.get("people", 0)) > len(people_items),
                },
                "summaries": {
                    "items": summary_items,
                    "count": len(summary_items),
                    "total": int(stats.get("summaries", 0)),
                    "truncated": int(stats.get("summaries", 0)) > len(summary_items),
                },
                "revisions": {
                    "items": revision_items,
                    "count": len(revision_items),
                    "total": int(stats.get("revisions", 0)),
                    "truncated": int(stats.get("revisions", 0)) > len(revision_items),
                },
                "evidence": {
                    "items": evidence_items,
                    "count": len(evidence_items),
                    "total": int(stats.get("evidence", 0)),
                    "truncated": int(stats.get("evidence", 0)) > len(evidence_items),
                },
                "policies": {
                    "items": policy_items,
                    "count": len(policy_items),
                    "total": retention_policy_total,
                    "truncated": retention_policy_total > len(policy_items),
                },
            },
        }

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="xAgent HTTP Agent Server",
            description="HTTP and WebSocket API for xAgent",
            version="1.0.0",
            lifespan=self._lifespan,
        )
        self._add_routes(app)
        if self._enable_web:
            self._add_web_ui(app)
        return app

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        heartbeat = create_runtime_heartbeat(
            self.agent,
            self.config.get("runtime") if isinstance(self.config, dict) else None,
            logger_=self.logger,
        )
        try:
            if heartbeat is not None:
                await heartbeat.start()
                self.logger.info(
                    "Runtime heartbeat started (interval=%ss)",
                    heartbeat.interval_seconds,
                )
            yield
        finally:
            if heartbeat is not None:
                await heartbeat.stop()
                self.logger.info("Runtime heartbeat stopped")
            await self._flush_agent_memory()

    async def _flush_agent_memory(self) -> None:
        flusher = getattr(self.agent, "flush_memory", None)
        if flusher is not None:
            await flusher()

    def _add_web_ui(self, app: FastAPI) -> None:
        if _STATIC_DIR.is_dir():
            @app.get("/", include_in_schema=False)
            async def serve_index():
                index = _STATIC_DIR / "index.html"
                if index.exists():
                    return FileResponse(str(index), media_type="text/html")
                raise HTTPException(status_code=404, detail="Web UI not found")

            @app.get("/memory", include_in_schema=False)
            async def serve_memory():
                page = _STATIC_DIR / "memory.html"
                if page.exists():
                    return FileResponse(str(page), media_type="text/html")
                raise HTTPException(status_code=404, detail="Memory page not found")

            @app.get("/message", include_in_schema=False)
            async def serve_message():
                page = _STATIC_DIR / "message.html"
                if page.exists():
                    return FileResponse(str(page), media_type="text/html")
                raise HTTPException(status_code=404, detail="Message page not found")

            @app.get("/group", include_in_schema=False)
            async def serve_group():
                page = _STATIC_DIR / "group.html"
                if page.exists():
                    return FileResponse(str(page), media_type="text/html")
                raise HTTPException(status_code=404, detail="Group page not found")

            @app.get("/agent", include_in_schema=False)
            async def serve_agent():
                page = _STATIC_DIR / "agent.html"
                if page.exists():
                    return FileResponse(str(page), media_type="text/html")
                raise HTTPException(status_code=404, detail="Agent page not found")

            app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
            self.logger.info("Web UI available at /")
        else:
            self.logger.warning("Static directory not found at %s — web UI disabled", _STATIC_DIR)

    def _add_routes(self, app: FastAPI) -> None:
        @app.get("/i/health", tags=["Health"])
        async def health_check():
            return "ok"

        @app.get("/health")
        async def health():
            return {"status": "healthy", "service": "xAgent HTTP Server"}

        @app.post("/chat")
        async def chat(input_data: ChatInput):
            self.logger.info(
                "Chat request from %s",
                input_data.user_id,
            )
            try:
                response = await self._run_chat_with_limits(input_data)
                return {"reply": self._response_payload(response)}
            except HTTPException:
                raise
            except Exception as exc:
                self.logger.error("Agent processing error for %s: %s", input_data.user_id, exc)
                raise HTTPException(status_code=500, detail=f"Agent processing error: {str(exc)}")

        @app.websocket("/ws/chat")
        async def websocket_chat(websocket: WebSocket):
            await websocket.accept()
            self.logger.info("WebSocket chat connected")

            while True:
                try:
                    raw_payload = await websocket.receive_json()
                    input_data = AgentInput.model_validate(raw_payload)
                    self.logger.info(
                        "WebSocket chat request from %s, stream=%s",
                        input_data.user_id,
                        input_data.stream,
                    )
                    await self._send_websocket_chat_events(websocket, input_data)
                except WebSocketDisconnect:
                    self.logger.info("WebSocket chat disconnected")
                    break
                except json.JSONDecodeError as exc:
                    self.logger.warning("Invalid WebSocket chat JSON: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        "Invalid JSON payload.",
                        status_code=400,
                        details=str(exc),
                    )
                except ValidationError as exc:
                    self.logger.warning("Invalid WebSocket chat payload: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        "Invalid chat payload.",
                        status_code=422,
                        details=exc.errors(),
                    )
                except Exception as exc:
                    self.logger.error("Unexpected WebSocket chat error: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        f"Agent processing error: {str(exc)}",
                    )

        @app.websocket("/ws/observe")
        async def websocket_observe(websocket: WebSocket):
            await websocket.accept()
            self.logger.info("WebSocket observe connected")

            while True:
                try:
                    raw_payload = await websocket.receive_json()
                    input_data = ObserveInput.model_validate(raw_payload)
                    self.logger.info(
                        "WebSocket observe request: source=%s, type=%s",
                        input_data.source,
                        input_data.event_type,
                    )
                    await self._send_websocket_observe_events(websocket, input_data)
                except WebSocketDisconnect:
                    self.logger.info("WebSocket observe disconnected")
                    break
                except json.JSONDecodeError as exc:
                    self.logger.warning("Invalid WebSocket observe JSON: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        "Invalid JSON payload.",
                        status_code=400,
                        details=str(exc),
                    )
                except ValidationError as exc:
                    self.logger.warning("Invalid WebSocket observe payload: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        "Invalid observe payload.",
                        status_code=422,
                        details=exc.errors(),
                    )
                except Exception as exc:
                    self.logger.error("Unexpected WebSocket observe error: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        f"Agent observe error: {str(exc)}",
                    )

        @app.post("/observe")
        async def observe(input_data: ObserveInput):
            self.logger.info(
                "Observation request: source=%s, type=%s",
                input_data.source,
                input_data.event_type,
            )
            try:
                response = await self._run_observe_with_limits(input_data)
                return self._response_payload(response)
            except HTTPException:
                raise
            except Exception as exc:
                self.logger.error(
                    "Agent observe error: source=%s type=%s error=%s",
                    input_data.source,
                    input_data.event_type,
                    exc,
                )
                raise HTTPException(status_code=500, detail=f"Agent observe error: {str(exc)}")

        @app.post("/clear_messages")
        async def clear_messages():
            self.logger.info("Clear messages request")
            try:
                await self.message_storage.clear_messages()
                return {
                    "status": "success",
                    "message": "Message stream cleared",
                }
            except Exception as exc:
                self.logger.error("Failed to clear messages: %s", exc)
                raise HTTPException(status_code=500, detail=f"Failed to clear messages: {str(exc)}")

        # ── Monitoring API endpoints ─────────────────────────────────

        @app.get("/api/agent/info", tags=["Monitoring"])
        async def agent_info():
            """Return agent metadata for monitoring pages."""
            memory_db = str(self._get_memory_db_path())
            storage_info = self.message_storage.get_stream_info() if hasattr(self.message_storage, "get_stream_info") else {}
            identity = self._get_agent_identity()
            try:
                identity_path = self._get_identity_path()
                identity_path_value = str(identity_path)
                identity_editable = True
            except HTTPException:
                identity_path_value = ""
                identity_editable = False
            return {
                "model": self.agent.model,
                "workspace": str(getattr(self, "workspace", "")),
                "memory_db": memory_db,
                "message_storage": storage_info,
                "tools": list(self.agent.tools.keys()),
                "identity": identity,
                "identity_file": BaseAgentConfig.IDENTITY_FILENAME,
                "identity_path": identity_path_value,
                "identity_editable": identity_editable,
                "system_prompt": identity,
            }

        @app.get("/api/agent/identity", tags=["Monitoring"])
        async def agent_identity():
            """Return identity.md content."""
            identity_path = self._get_identity_path()
            if not identity_path.is_file():
                raise HTTPException(status_code=404, detail="identity.md not found")
            content = identity_path.read_text(encoding="utf-8")
            return {
                "identity": content,
                "path": str(identity_path),
                "filename": identity_path.name,
                "modified": identity_path.stat().st_mtime,
            }

        @app.put("/api/agent/identity", tags=["Monitoring"])
        async def update_agent_identity(input_data: IdentityInput):
            """Persist identity.md and update the running agent."""
            identity = input_data.identity.strip()
            if not identity:
                raise HTTPException(status_code=400, detail="Identity cannot be empty")

            identity_path = self._get_identity_path()
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            file_content = f"{identity}\n"
            identity_path.write_text(file_content, encoding="utf-8")
            self._set_agent_identity(identity)

            return {
                "status": "ok",
                "identity": file_content,
                "path": str(identity_path),
                "filename": identity_path.name,
                "modified": identity_path.stat().st_mtime,
            }

        @app.get("/api/memory/stats", tags=["Monitoring"])
        async def memory_stats():
            """Return SQLite memory statistics."""
            return await self._get_memory_store().get_stats()

        @app.get("/api/memory/dashboard", tags=["Monitoring"])
        async def memory_dashboard(
            preview_limit: int = Query(72, ge=8, le=200, description="Rows to preview per collection"),
        ):
            """Return a wide read-only snapshot of the unified memory schema."""
            return await self._build_memory_dashboard(preview_limit=preview_limit)

        @app.get("/api/memory/recall", tags=["Monitoring"])
        async def memory_recall(
            query: str = Query("", description="Recall cue"),
            subject_type: Optional[str] = Query(None, description="Subject type filter"),
            subject_key: Optional[str] = Query(None, description="Subject key filter"),
            time_range: Optional[str] = Query(None, description="Optional date/time range"),
            kinds: Optional[str] = Query(None, description="Comma-separated memory kinds"),
            include_evidence: bool = Query(False, description="Include evidence rows"),
            max_items: int = Query(8, ge=1, le=100, description="Maximum memory items"),
        ):
            """Recall active structured memory items."""
            return await self._get_memory_store().recall_memory(
                query=query,
                subject_type=subject_type,
                subject_key=subject_key,
                time_range=time_range,
                kinds=kinds,
                include_evidence=include_evidence,
                max_items=max_items,
            )

        @app.get("/api/memory/search", tags=["Monitoring"])
        async def memory_search_alias(
            query: str = Query(..., min_length=1, description="Recall cue"),
            limit: int = Query(50, ge=1, le=200, description="Maximum number of results to return"),
        ):
            """Compatibility alias for memory recall."""
            return await self._get_memory_store().recall_memory(query=query, max_items=limit)

        @app.get("/api/memory/items", tags=["Monitoring"])
        async def memory_items(
            status: Optional[str] = Query(None, description="Memory status"),
            kind: Optional[str] = Query(None, description="Memory kind"),
            subject_type: Optional[str] = Query(None, description="Subject type"),
            limit: int = Query(50, ge=1, le=200, description="Maximum rows"),
            offset: int = Query(0, ge=0, description="Rows to skip"),
        ):
            """List structured memory items."""
            return await self._get_memory_store().list_memory_items(
                status=status,
                kind=kind,
                subject_type=subject_type,
                limit=limit,
                offset=offset,
            )

        @app.get("/api/memory/items/{memory_id}", tags=["Monitoring"])
        async def memory_item(memory_id: int):
            """Read one structured memory item with evidence."""
            item = await self._get_memory_store().get_memory_item(memory_id, include_evidence=True)
            if item is None:
                raise HTTPException(status_code=404, detail="Memory item not found")
            return item

        @app.post("/api/memory/items/{memory_id}/correct", tags=["Monitoring"])
        async def memory_item_correct(memory_id: int, input_data: MemoryCorrectInput):
            """Correct one memory item and record a revision."""
            return await self._get_memory_store().correct_memory(
                memory_id=memory_id,
                query=input_data.query,
                correction=input_data.correction,
                reason=input_data.reason,
                actor="http",
            )

        @app.post("/api/memory/items/{memory_id}/forget", tags=["Monitoring"])
        async def memory_item_forget(memory_id: int, input_data: MemoryForgetInput):
            """Archive or delete one memory item."""
            return await self._get_memory_store().forget_memory(
                memory_id=memory_id,
                mode=input_data.mode,
                reason=input_data.reason,
                actor="http",
            )

        @app.get("/api/memory/events", tags=["Monitoring"])
        async def memory_events(
            limit: int = Query(50, ge=1, le=500, description="Maximum events"),
            offset: int = Query(0, ge=0, description="Events to skip"),
            speaker_id: Optional[str] = Query(None, description="Speaker filter"),
            conversation_id: Optional[str] = Query(None, description="Conversation filter"),
        ):
            """List raw experience events."""
            return await self._get_memory_store().get_events(
                limit=limit,
                offset=offset,
                speaker_id=speaker_id,
                conversation_id=conversation_id,
            )

        @app.get("/api/memory/events/{event_id}", tags=["Monitoring"])
        async def memory_event(event_id: int):
            """Read one raw experience event."""
            event = await self._get_memory_store().get_event(event_id)
            if event is None:
                raise HTTPException(status_code=404, detail="Memory event not found")
            return event

        @app.post("/api/memory/export", tags=["Monitoring"])
        async def memory_export(input_data: MemoryExportInput):
            """Export memory to Markdown and JSONL."""
            workspace = getattr(self, "workspace", Path.cwd())
            output_dir = input_data.output_dir or str(workspace / "exports" / "memory")
            return await self._get_memory_store().export_memory(output_dir)

        @app.post("/api/memory/query", tags=["Monitoring"])
        async def memory_query(input_data: MemoryQueryInput):
            """Execute one safe read-only SQL query against the memory database."""
            try:
                return await self._get_memory_store().query_sql(
                    input_data.sql,
                    max_rows=input_data.max_rows,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.get("/api/messages", tags=["Monitoring"])
        async def get_messages(
            count: int = Query(50, ge=1, le=500, description="Number of messages to retrieve"),
            offset: int = Query(0, ge=0, description="Number of recent messages to skip"),
            timezone: Optional[str] = Query(None, description="IANA timezone for formatted timestamps"),
        ):
            """Paginated message retrieval for the monitoring page.

            Returns messages in newest-first order so the UI can append older
            pages at the end without reordering previously rendered items.
            """
            total = await self.message_storage.get_message_count()
            messages = await self.message_storage.get_messages(count=count, offset=offset)
            items = [self._message_payload(msg, request_timezone=timezone) for msg in messages]
            items.reverse()
            return {
                "messages": items,
                "total": total,
                "count": count,
                "offset": offset,
                "has_more": offset + count < total,
            }

        @app.post("/api/memory/clear", tags=["Monitoring"])
        async def memory_clear():
            """Clear all SQLite memory rows."""
            await self._get_memory_store().clear()
            return {"status": "ok"}

        @app.get("/api/messages/stats", tags=["Monitoring"])
        async def messages_stats(
            timezone: Optional[str] = Query(None, description="IANA timezone for formatted timestamps"),
        ):
            """Return message storage statistics."""
            active_timezone = self._resolve_request_timezone(timezone)
            total = await self.message_storage.get_message_count()
            storage_info = self.message_storage.get_stream_info() if hasattr(self.message_storage, "get_stream_info") else {}
            result: Dict[str, Any] = {
                "total": total,
                "storage": storage_info,
                "timezone": timezone_name(active_timezone),
            }
            if total > 0:
                oldest = await self.message_storage.get_messages(count=1, offset=total - 1)
                newest = await self.message_storage.get_messages(count=1, offset=0)
                if oldest:
                    timestamp = float(oldest[0].timestamp)
                    result["earliest_timestamp"] = timestamp
                    result["earliest_timestamp_utc"] = format_utc(timestamp)
                    result["earliest_timestamp_local"] = format_in_timezone(timestamp, active_timezone)
                if newest:
                    timestamp = float(newest[0].timestamp)
                    result["latest_timestamp"] = timestamp
                    result["latest_timestamp_utc"] = format_utc(timestamp)
                    result["latest_timestamp_local"] = format_in_timezone(timestamp, active_timezone)
            return result

    def run(self, host: str = None, port: int = None, open_browser: bool = False) -> None:
        host = host if host is not None else BaseAgentConfig.DEFAULT_HOST
        port = port if port is not None else BaseAgentConfig.DEFAULT_PORT

        self.logger.info("Starting xAgent HTTP Server on %s:%s", host, port)
        self.logger.info("Model: %s", self.agent.model)
        self.logger.info("Tools: %d loaded", len(self.agent.tools))
        self.logger.info("Web UI: %s", "enabled at /" if self._enable_web else "disabled (--no-web)")

        if open_browser and self._enable_web:
            import threading
            import webbrowser

            browse_host = host

            # 0.0.0.0 不能直接用于浏览器访问
            if browse_host == "0.0.0.0":
                browse_host = "127.0.0.1"

            # IPv6 浏览器 URL 需要 []
            if ":" in browse_host and not browse_host.startswith("["):
                browse_host = f"[{browse_host}]"

            url = f"http://{browse_host}:{port}"

            threading.Timer(1.5, lambda: webbrowser.open(url)).start()

        uvicorn.run(self.app, host=host, port=port)


_server_instance = None


def get_app() -> FastAPI:
    global _server_instance
    if _server_instance is None:
        _server_instance = AgentHTTPServer()
    return _server_instance.app


def get_app_lazy() -> FastAPI:
    return get_app()


app = None
