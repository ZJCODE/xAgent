import argparse
import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Union

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
    conversation_id: str
    user_message: str
    image_source: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    history_count: Optional[int] = 16
    max_iter: Optional[int] = 10
    max_concurrent_tools: Optional[int] = 10
    enable_memory: Optional[bool] = True


class ClearConversationInput(BaseModel):
    """Request body for clear conversation endpoint."""

    conversation_id: str


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

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="xAgent HTTP Agent Server",
            description="HTTP API for xAgent conversational AI",
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
                "Chat request from %s, conversation %s, stream=%s",
                input_data.user_id,
                input_data.conversation_id,
                input_data.stream,
            )
            try:
                if input_data.stream:
                    async def event_generator():
                        try:
                            response = await self.agent(
                                user_message=input_data.user_message,
                                user_id=input_data.user_id,
                                conversation_id=input_data.conversation_id,
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
                    conversation_id=input_data.conversation_id,
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

        @app.get("/memory")
        async def get_memory(
            query: str = Query("recent conversations", description="Search query for memory retrieval"),
            limit: int = Query(10, ge=1, le=50, description="Maximum number of memories to return"),
        ):
            self.logger.info("Memory retrieval for agent key %s, query=%s, limit=%d", self.agent.memory_key, query, limit)
            memory_storage = getattr(self.agent, "memory_storage", None)
            if memory_storage is None:
                return {"memories": [], "message": "Memory storage not configured"}
            try:
                results = await memory_storage.retrieve(
                    memory_key=self.agent.memory_key,
                    query=query,
                    limit=limit,
                )
                memories = []
                if results:
                    for item in results:
                        if isinstance(item, str):
                            memories.append({"content": item})
                        elif isinstance(item, dict):
                            memories.append(item)
                        else:
                            memories.append({"content": str(item)})
                return {"memories": memories}
            except Exception as exc:
                self.logger.error("Memory retrieval error: %s", exc)
                raise HTTPException(status_code=500, detail=f"Memory retrieval error: {str(exc)}")

        @app.post("/clear_conversation")
        async def clear_conversation(input_data: ClearConversationInput):
            self.logger.info("Clear conversation request for %s", input_data.conversation_id)
            try:
                await self.message_storage.clear_conversation(
                    conversation_id=self.agent.normalize_conversation_id(input_data.conversation_id)
                )
                return {
                    "status": "success",
                    "message": f"Conversation {input_data.conversation_id} cleared",
                }
            except Exception as exc:
                self.logger.error("Failed to clear conversation %s: %s", input_data.conversation_id, exc)
                raise HTTPException(status_code=500, detail=f"Failed to clear conversation: {str(exc)}")

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
        logger.info("Loaded .env file from: %s", args.env)
    else:
        logger.warning(".env file not found: %s", args.env)

    try:
        server = AgentHTTPServer(
            config_path=args.config,
            toolkit_path=args.toolkit_path,
            agent=None,
            enable_web=not args.no_web,
        )
        server.run(host=args.host, port=args.port, open_browser=args.open_browser)
    except Exception as exc:
        logger.error("Failed to start server: %s", exc)
        raise


if __name__ == "__main__":
    main()
