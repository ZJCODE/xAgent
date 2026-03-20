import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .base import BaseAgentRunner
from ..core.agent import Agent

_STATIC_DIR = Path(__file__).parent / "static"


class AgentInput(BaseModel):
    """Request body for chat endpoint."""

    user_id: str
    user_message: str
    image_source: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    history_count: Optional[int] = 100
    max_iter: Optional[int] = 10
    max_concurrent_tools: Optional[int] = 10
    enable_memory: Optional[bool] = True


class AgentHTTPServer(BaseAgentRunner):
    """HTTP server for xAgent."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        toolkit_path: Optional[str] = None,
        agent: Optional[Agent] = None,
        enable_web: bool = True,
    ):
        self._enable_web = enable_web

        if agent is not None:
            self.agent = agent
            self.config = {"server": {"host": "0.0.0.0", "port": 8010}}
            self.message_storage = self.agent.message_storage
        else:
            super().__init__(config_path, toolkit_path)

        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.app = self._create_app()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def _get_memory_root(self) -> Path:
        memory = self.agent.markdown_memory
        memory_root = getattr(memory, "root", None)
        if memory_root is None:
            raise HTTPException(status_code=500, detail="Memory storage path is unavailable")
        return Path(memory_root).expanduser().resolve()

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="xAgent HTTP Agent Server",
            description="HTTP API for xAgent continuous-stream AI",
            version="1.0.0",
        )
        self._add_routes(app)
        if self._enable_web:
            self._add_web_ui(app)
        return app

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
        async def chat(input_data: AgentInput):
            self.logger.info(
                "Chat request from %s, stream=%s",
                input_data.user_id,
                input_data.stream,
            )
            try:
                if input_data.stream:
                    async def event_generator():
                        try:
                            response = await self.agent(
                                user_message=input_data.user_message,
                                user_id=input_data.user_id,
                                history_count=input_data.history_count,
                                max_iter=input_data.max_iter,
                                max_concurrent_tools=input_data.max_concurrent_tools,
                                image_source=input_data.image_source,
                                stream=True,
                                enable_memory=input_data.enable_memory,
                            )
                            if hasattr(response, "__aiter__"):
                                async for delta in response:
                                    yield f"data: {json.dumps({'delta': delta})}\n\n"
                                yield "data: [DONE]\n\n"
                            else:
                                if hasattr(response, "model_dump"):
                                    yield f"data: {json.dumps({'message': response.model_dump()})}\n\n"
                                else:
                                    yield f"data: {json.dumps({'message': str(response)})}\n\n"
                                yield "data: [DONE]\n\n"
                        except Exception as exc:
                            self.logger.error("Streaming error for %s: %s", input_data.user_id, exc)
                            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
                            yield "data: [DONE]\n\n"

                    return StreamingResponse(event_generator(), media_type="text/event-stream")

                response = await self.agent(
                    user_message=input_data.user_message,
                    user_id=input_data.user_id,
                    history_count=input_data.history_count,
                    max_iter=input_data.max_iter,
                    max_concurrent_tools=input_data.max_concurrent_tools,
                    image_source=input_data.image_source,
                    enable_memory=input_data.enable_memory,
                )

                if hasattr(response, "model_dump"):
                    return {"reply": response.model_dump()}
                return {"reply": str(response)}
            except Exception as exc:
                self.logger.error("Agent processing error for %s: %s", input_data.user_id, exc)
                raise HTTPException(status_code=500, detail=f"Agent processing error: {str(exc)}")

        @app.post("/clear_messages")
        async def clear_messages():
            self.logger.info("Clear messages request for agent %s", self.agent.name)
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
            memory_dir = str(self._get_memory_root())
            storage_info = self.message_storage.get_stream_info() if hasattr(self.message_storage, "get_stream_info") else {}
            return {
                "name": self.agent.name,
                "model": self.agent.model,
                "workspace": str(getattr(self, "workspace", "")),
                "memory_dir": memory_dir,
                "message_storage": storage_info,
            }

        @app.get("/api/memory/tree", tags=["Monitoring"])
        async def memory_tree():
            """Return the memory directory tree as JSON."""
            memory_dir = self._get_memory_root()
            if not memory_dir.is_dir():
                return {"tree": []}

            def _scan(directory: Path, rel_root: Path) -> List[Dict[str, Any]]:
                entries: List[Dict[str, Any]] = []
                try:
                    children = sorted(directory.iterdir(), key=lambda p: p.name)
                except PermissionError:
                    return entries
                for child in children:
                    rel = child.relative_to(rel_root)
                    if child.is_dir():
                        entries.append({
                            "name": child.name,
                            "path": str(rel),
                            "type": "dir",
                            "children": _scan(child, rel_root),
                        })
                    elif child.suffix == ".md":
                        entries.append({
                            "name": child.name,
                            "path": str(rel),
                            "type": "file",
                            "modified": child.stat().st_mtime,
                        })
                return entries

            return {"tree": _scan(memory_dir, memory_dir)}

        @app.get("/api/memory/read", tags=["Monitoring"])
        async def memory_read(path: str = Query(..., description="Relative path inside memory directory")):
            """Read a specific memory markdown file."""
            memory_dir = self._get_memory_root()
            requested = (memory_dir / path).resolve()

            # Path traversal protection
            if not requested.is_relative_to(memory_dir):
                raise HTTPException(status_code=403, detail="Access denied")
            if not requested.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            if requested.suffix != ".md":
                raise HTTPException(status_code=403, detail="Only markdown files can be read")

            content = requested.read_text(encoding="utf-8")
            return {
                "path": path,
                "content": content,
                "modified": requested.stat().st_mtime,
            }

        @app.get("/api/memory/search", tags=["Monitoring"])
        async def memory_search(
            query: str = Query(..., min_length=1, description="Search text for memory file names or file content"),
            limit: int = Query(50, ge=1, le=200, description="Maximum number of results to return"),
        ):
            """Search memory files by file name and content."""
            memory_dir = self._get_memory_root()
            needle = query.strip().lower()
            results: List[Dict[str, Any]] = []

            for file_path in sorted(memory_dir.rglob("*.md")):
                if len(results) >= limit:
                    break

                relative_path = str(file_path.relative_to(memory_dir))
                file_name = file_path.name
                match_kind: List[str] = []
                snippet = ""

                if needle in file_name.lower() or needle in relative_path.lower():
                    match_kind.append("filename")

                try:
                    content = file_path.read_text(encoding="utf-8")
                except OSError:
                    continue

                lower_content = content.lower()
                content_index = lower_content.find(needle)
                if content_index != -1:
                    match_kind.append("content")
                    start = max(0, content_index - 80)
                    end = min(len(content), content_index + len(query) + 120)
                    snippet = content[start:end].replace("\n", " ").strip()

                if match_kind:
                    results.append({
                        "path": relative_path,
                        "name": file_name,
                        "matched_in": match_kind,
                        "snippet": snippet,
                        "modified": file_path.stat().st_mtime,
                    })

            return {
                "query": query,
                "results": results,
            }

        @app.get("/api/messages", tags=["Monitoring"])
        async def get_messages(
            count: int = Query(50, ge=1, le=500, description="Number of messages to retrieve"),
            offset: int = Query(0, ge=0, description="Number of recent messages to skip"),
        ):
            """Paginated message retrieval for the monitoring page.

            Returns messages in newest-first order so the UI can append older
            pages at the end without reordering previously rendered items.
            """
            total = await self.message_storage.get_message_count()
            messages = await self.message_storage.get_messages(count=count, offset=offset)
            items = []
            for msg in messages:
                item = {
                    "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                    "type": msg.type.value if hasattr(msg.type, "value") else str(msg.type),
                    "content": msg.content,
                    "sender_id": msg.sender_id,
                    "timestamp": msg.timestamp,
                }
                if msg.tool_call:
                    item["tool_call"] = {
                        "name": msg.tool_call.name,
                        "arguments": msg.tool_call.arguments,
                        "output": msg.tool_call.output,
                    }
                items.append(item)
            items.reverse()
            return {
                "messages": items,
                "total": total,
                "count": count,
                "offset": offset,
                "has_more": offset + count < total,
            }

        @app.get("/api/messages/stats", tags=["Monitoring"])
        async def messages_stats():
            """Return message storage statistics."""
            total = await self.message_storage.get_message_count()
            storage_info = self.message_storage.get_stream_info() if hasattr(self.message_storage, "get_stream_info") else {}
            result: Dict[str, Any] = {"total": total, "storage": storage_info}
            if total > 0:
                oldest = await self.message_storage.get_messages(count=1, offset=total - 1)
                newest = await self.message_storage.get_messages(count=1, offset=0)
                if oldest:
                    result["earliest_timestamp"] = oldest[0].timestamp
                if newest:
                    result["latest_timestamp"] = newest[0].timestamp
            return result

    def run(self, host: str = None, port: int = None, open_browser: bool = False) -> None:
        server_cfg = self.config.get("server", {})
        host = host or server_cfg.get("host", "0.0.0.0")
        port = port or server_cfg.get("port", 8010)

        self.logger.info("Starting xAgent HTTP Server on %s:%s", host, port)
        self.logger.info("Agent: %s", self.agent.name)
        self.logger.info("Model: %s", self.agent.model)
        self.logger.info("Tools: %d loaded", len(self.agent.tools))
        self.logger.info("Web UI: %s", "enabled at /" if self._enable_web else "disabled (--no-web)")

        if open_browser and self._enable_web:
            import threading
            import webbrowser

            browse_host = "localhost" if host in ("0.0.0.0", "::") else host
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


def main():
    logger = logging.getLogger("xAgent.HTTPServer.main")

    parser = argparse.ArgumentParser(description="xAgent HTTP Server")
    parser.add_argument("--config", default=None, help="Config file path (if not specified, uses default configuration)")
    parser.add_argument("--toolkit_path", default=None, help="Toolkit directory path (if not specified, no additional tools will be loaded)")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--open", action="store_true", dest="open_browser", help="Auto-open the web UI in the default browser")
    parser.add_argument("--no-web", action="store_true", dest="no_web", help="Disable the built-in web UI (API-only mode)")

    args = parser.parse_args()

    if os.path.exists(args.env):
        load_dotenv(args.env, override=True)
        logger.info("Loaded environment from %s", args.env)

    server = AgentHTTPServer(
        config_path=args.config,
        toolkit_path=args.toolkit_path,
        enable_web=not args.no_web,
    )
    server.run(host=args.host, port=args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    main()
