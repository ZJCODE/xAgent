import uvicorn
import argparse
import logging
import os
from pathlib import Path
from typing import Optional, Union, List
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
from dotenv import load_dotenv

from .base import BaseAgentRunner
from ..core.agent import Agent

# Path to bundled static frontend
_STATIC_DIR = Path(__file__).parent / "static"


class AgentInput(BaseModel):
    """Request body for chat endpoint."""
    user_id: str
    session_id: str
    user_message: str
    image_source: Optional[Union[str, List[str]]] = None
    # Enable server-side streaming when true
    stream: Optional[bool] = False
    # Number of previous messages to include in conversation history
    history_count: Optional[int] = 16
    # Maximum model call attempts
    max_iter: Optional[int] = 10
    # Maximum number of concurrent tool calls
    max_concurrent_tools: Optional[int] = 10
    # Whether to enable memory storage and retrieval
    enable_memory: Optional[bool] = False
    # Whether to enable the agent can share current chat with other user or agent
    shared: Optional[bool] = False


class ClearSessionInput(BaseModel):
    """Request body for clear session endpoint."""
    user_id: str
    session_id: str


class AgentHTTPServer(BaseAgentRunner):
    """
    HTTP Agent Server for xAgent.
    
    This server can be initialized in two ways:
    1. Using configuration files (traditional approach)
    2. Using a pre-configured Agent instance (new approach)
    
    Examples:
        Traditional approach with config:
        >>> server = AgentHTTPServer(config_path="config.yaml")
        >>> server.run()
        
        Direct agent approach:
        >>> agent = Agent(name="MyAgent", tools=[web_search])
        >>> server = AgentHTTPServer(agent=agent)
        >>> server.run()
    """
    
    def __init__(
        self, 
        config_path: Optional[str] = None, 
        toolkit_path: Optional[str] = None,
        agent: Optional[Agent] = None,
        enable_web: bool = True,
    ):
        """
        Initialize AgentHTTPServer.
        
        Args:
            config_path: Path to configuration file (if None, uses default configuration)
            toolkit_path: Path to toolkit directory (if None, no additional tools will be loaded)
            agent: Pre-configured Agent instance (if provided, config_path and toolkit_path are ignored)
            enable_web: Whether to serve the built-in web UI at / (default: True)
        """
        self._enable_web = enable_web

        if agent is not None:
            # Use the provided agent directly
            self.agent = agent
            self.config = {"server": {"host": "0.0.0.0", "port": 8010}}  # Minimal server config
            self.message_storage = self.agent.message_storage
        else:
            # Initialize the base agent runner using config
            super().__init__(config_path, toolkit_path)
        
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Initialize FastAPI app
        self.app = self._create_app()

        # Enable CORS for local development and external frontends
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
    def _create_app(self) -> FastAPI:
        """Create and configure FastAPI application."""
        app = FastAPI(
            title="xAgent HTTP Agent Server",
            description="HTTP API for xAgent conversational AI",
            version="1.0.0"
        )
        
        # Add API routes
        self._add_routes(app)

        # Serve bundled web UI (unless disabled)
        if self._enable_web:
            self._add_web_ui(app)
        
        return app

    def _add_web_ui(self, app: FastAPI) -> None:
        """Mount static files and root route for the built-in web UI."""
        if _STATIC_DIR.is_dir():
            # Root route serves index.html
            @app.get("/", include_in_schema=False)
            async def serve_index():
                index = _STATIC_DIR / "index.html"
                if index.exists():
                    return FileResponse(str(index), media_type="text/html")
                raise HTTPException(status_code=404, detail="Web UI not found")

            # Static assets (CSS, JS, images etc.)
            app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
            self.logger.info("Web UI available at /")
        else:
            self.logger.warning("Static directory not found at %s — web UI disabled", _STATIC_DIR)
    
    def _add_routes(self, app: FastAPI) -> None:
        """Add API routes to the FastAPI application."""
        

        @app.get("/i/health", tags=["Health"])
        async def health_check():
            """
            Health check endpoint to verify if the API is running.
            """
            return "ok"

        @app.get("/health")
        async def health_check():
            """Health check endpoint."""
            self.logger.debug("Health check requested")
            return {"status": "healthy", "service": "xAgent HTTP Server"}
        
        @app.post("/chat")
        async def chat(input_data: AgentInput):
            """
            Chat endpoint for agent interaction.
            
            Args:
                input_data: User input containing message and metadata
                    - user_id: Unique identifier for the user
                    - session_id: Unique identifier for the conversation session
                    - user_message: The user's message content
                    - image_source: Optional image(s) for analysis (URL, file path, base64, or list of these)
                    - stream: Whether to enable streaming response (default: False)
                    - history_count: Number of previous messages to include (default: 16)
                    - max_iter: Maximum model call attempts (default: 10)
                    - enable_memory: Whether to enable memory storage and retrieval (default: False)
                    - shared: Whether to enable the agent can share current chat with other user or agent (default: False)
                
            Returns:
                Agent response or streaming SSE when input_data.stream is True
            """
            self.logger.info(
                "Chat request from user %s, session %s, stream=%s, shared=%s", 
                input_data.user_id, input_data.session_id, input_data.stream, input_data.shared
            )
            try:
                # Streaming mode via Server-Sent Events
                if input_data.stream:
                    self.logger.debug("Enabling streaming response for user %s", input_data.user_id)
                    async def event_generator():
                        try:
                            response = await self.agent(
                                user_message=input_data.user_message,
                                user_id=input_data.user_id,
                                session_id=input_data.session_id,
                                history_count=input_data.history_count,
                                max_iter=input_data.max_iter,
                                max_concurrent_tools=input_data.max_concurrent_tools,
                                image_source=input_data.image_source,
                                stream=True,
                                enable_memory=input_data.enable_memory,
                                shared=input_data.shared
                            )
                            # If the agent returns an async generator, stream deltas
                            if hasattr(response, "__aiter__"):
                                async for delta in response:
                                    # Send as SSE data frames
                                    yield f"data: {json.dumps({'delta': delta})}\n\n"
                                # Signal completion
                                yield "data: [DONE]\n\n"
                            else:
                                # Fallback when no generator is returned
                                # Handle structured output properly
                                if hasattr(response, 'model_dump'):  # Pydantic BaseModel
                                    yield f"data: {json.dumps({'message': response.model_dump()})}\n\n"
                                else:  # String response
                                    yield f"data: {json.dumps({'message': str(response)})}\n\n"
                                yield "data: [DONE]\n\n"
                        except Exception as e:
                            self.logger.error("Streaming error for user %s: %s", input_data.user_id, e)
                            # Stream error as SSE, client can handle gracefully
                            yield f"data: {json.dumps({'error': str(e)})}\n\n"
                            yield "data: [DONE]\n\n"
                    return StreamingResponse(event_generator(), media_type="text/event-stream")
                
                # Non-streaming mode (default)
                response = await self.agent(
                    user_message=input_data.user_message,
                    user_id=input_data.user_id,
                    session_id=input_data.session_id,
                    history_count=input_data.history_count,
                    max_iter=input_data.max_iter,
                    max_concurrent_tools=input_data.max_concurrent_tools,
                    image_source=input_data.image_source,
                    enable_memory=input_data.enable_memory,
                    shared=input_data.shared
                )
                
                self.logger.debug("Chat response generated for user %s", input_data.user_id)
                
                # Handle different response types properly
                if hasattr(response, 'model_dump'):  # Pydantic BaseModel
                    return {"reply": response.model_dump()}
                else:  # String response
                    return {"reply": str(response)}
                
            except Exception as e:
                self.logger.error("Agent processing error for user %s: %s", input_data.user_id, e)
                raise HTTPException(status_code=500, detail=f"Agent processing error: {str(e)}")
        
        @app.get("/memory")
        async def get_memory(
            user_id: str = Query(..., description="User ID to retrieve memories for"),
            query: str = Query("recent conversations", description="Search query for memory retrieval"),
            limit: int = Query(10, ge=1, le=50, description="Maximum number of memories to return"),
        ):
            """
            Retrieve stored memories for a user.
            
            Returns:
                List of memory entries matching the query.
            """
            self.logger.info("Memory retrieval for user %s, query=%s, limit=%d", user_id, query, limit)
            memory_storage = getattr(self.agent, 'memory_storage', None)
            if memory_storage is None:
                return {"memories": [], "message": "Memory storage not configured"}
            try:
                results = await memory_storage.retrieve(
                    user_id=user_id,
                    query=query,
                    limit=limit,
                )
                # Normalise: results may be list of strings or list of dicts
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
            except Exception as e:
                self.logger.error("Memory retrieval error: %s", e)
                raise HTTPException(status_code=500, detail=f"Memory retrieval error: {str(e)}")

        @app.post("/clear_session")
        async def clear_session(input_data: ClearSessionInput):
            """
            Clear session data endpoint.
            
            Args:
                input_data: Contains user_id and session_id to clear
                
            Returns:
                Success confirmation
            """
            self.logger.info(
                "Clear session request for user %s, session %s", 
                input_data.user_id, input_data.session_id
            )
            try:
                await self.message_storage.clear_history(
                    user_id=input_data.user_id,
                    session_id=input_data.session_id
                )
                
                self.logger.debug(
                    "Session %s for user %s cleared successfully", 
                    input_data.session_id, input_data.user_id
                )
                
                return {"status": "success", "message": f"Session {input_data.session_id} for user {input_data.user_id} cleared"}
                
            except Exception as e:
                self.logger.error(
                    "Failed to clear session %s for user %s: %s", 
                    input_data.session_id, input_data.user_id, e
                )
                raise HTTPException(status_code=500, detail=f"Failed to clear session: {str(e)}")

    
    def run(self, host: str = None, port: int = None, open_browser: bool = False) -> None:
        """
        Run the HTTP server.
        
        Args:
            host: Host to bind to
            port: Port to bind to
            open_browser: Whether to auto-open the web UI in the default browser
        """
        server_cfg = self.config.get("server", {})
        
        # Use provided args or fall back to config defaults
        host = host or server_cfg.get("host", "0.0.0.0")
        port = port or server_cfg.get("port", 8010)
        
        self.logger.info("Starting xAgent HTTP Server on %s:%s", host, port)
        self.logger.info("Agent: %s", self.agent.name)
        self.logger.info("Model: %s", self.agent.model)
        self.logger.info("Tools: %d loaded", len(self.agent.tools))
        if self._enable_web:
            self.logger.info("Web UI: enabled at /")
        else:
            self.logger.info("Web UI: disabled (--no-web)")

        # Auto-open browser after a short delay so the server has time to start
        if open_browser and self._enable_web:
            import threading
            import webbrowser
            browse_host = "localhost" if host in ("0.0.0.0", "::") else host
            url = f"http://{browse_host}:{port}"
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        
        uvicorn.run(
            self.app,
            host=host,
            port=port,
        )


# Global server instance for uvicorn module loading
_server_instance = None


def get_app() -> FastAPI:
    """Get the FastAPI app instance for uvicorn."""
    global _server_instance
    if _server_instance is None:
        # Use default configuration when used as module
        _server_instance = AgentHTTPServer()
    return _server_instance.app


def get_app_lazy() -> FastAPI:
    """Lazy initialization for global app variable."""
    return get_app()


# For backward compatibility - use lazy initialization to avoid import-time errors
app = None  # Will be initialized when first accessed


def main():
    """Main entry point for xagent-server command."""
    # Initialize basic logging for main entry point
    logger = logging.getLogger("xAgent.HTTPServer.main")
    
    parser = argparse.ArgumentParser(description="xAgent HTTP Server")
    parser.add_argument("--config", default=None, help="Config file path (if not specified, uses default configuration)")
    parser.add_argument("--toolkit_path", default=None, help="Toolkit directory path (if not specified, no additional tools will be loaded)")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="Auto-open the web UI in the default browser")
    parser.add_argument("--no-web", action="store_true", dest="no_web",
                        help="Disable the built-in web UI (API-only mode)")
    
    args = parser.parse_args()
    
    # Load .env file (default: .env in current directory)
    if os.path.exists(args.env):
        load_dotenv(args.env, override=True)
        logger.info("Loaded .env file from: %s", args.env)
    else:
        logger.warning(".env file not found: %s", args.env)
    
    try:
        server = AgentHTTPServer(
            config_path=args.config, 
            toolkit_path=args.toolkit_path,
            agent=None,  # Command line interface does not support direct agent passing
            enable_web=not args.no_web,
        )
        server.run(host=args.host, port=args.port, open_browser=args.open_browser)
    except Exception as e:
        logger.error("Failed to start server: %s", e)
        raise


if __name__ == "__main__":
    main()