import os
import yaml
import uvicorn
import argparse
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import importlib.util
import sys
import json
from fastapi.responses import StreamingResponse

from xagent.core.agent import Agent
from xagent.core.session import Session
from xagent.db.message import MessageDB
from xagent.tools import TOOL_REGISTRY


class AgentInput(BaseModel):
    """Request body for chat endpoint."""
    user_id: str
    session_id: str
    user_message: str
    image_source: Optional[str] = None
    # Enable server-side streaming when true
    stream: Optional[bool] = False


class ClearSessionInput(BaseModel):
    """Request body for clear session endpoint."""
    user_id: str
    session_id: str


class HTTPAgentServer:
    """HTTP Agent Server for xAgent."""
    
    def __init__(self, config_path: str = "config/agent.yaml",toolkit_path: str = "toolkit"):
        """
        Initialize HTTPAgentServer.
        
        Args:
            config_path: Path to configuration file
        """
        # Load environment variables
        load_dotenv(override=True)
        
        # Persist toolkit path for dynamic loading
        self.toolkit_path = toolkit_path
        
        # Load configuration
        self.config = self._load_config(config_path)
        
        # Initialize components
        self.agent = self._initialize_agent()
        self.message_db = self._initialize_message_db()
        self.app = self._create_app()
        
    def _load_config(self, cfg_path: str) -> Dict[str, Any]:
        """
        Load YAML configuration file.
        
        Args:
            cfg_path: Path to config file
            
        Returns:
            Configuration dictionary
            
        Raises:
            FileNotFoundError: If config file not found
        """
        if not os.path.isfile(cfg_path):
            # Support relative path lookup from project root first
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            root_path = os.path.join(project_root, cfg_path)
            if os.path.isfile(root_path):
                cfg_path = root_path
            else:
                # Fallback to relative path from this file
                base = os.path.dirname(os.path.abspath(__file__))
                abs_path = os.path.join(base, cfg_path)
                if os.path.isfile(abs_path):
                    cfg_path = abs_path
                else:
                    raise FileNotFoundError(f"Cannot find config file at {cfg_path}, {root_path}, or {abs_path}")
            
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    
    def _load_toolkit_registry(self, toolkit_path: Optional[str]) -> Dict[str, Any]:
        """Dynamically load TOOLKIT_REGISTRY from a toolkit directory.
        Only a directory path is supported; do not pass __init__.py.
        Returns empty dict if unavailable or on error.
        """
        if not toolkit_path:
            return {}
        try:
            # Resolve relative paths against this file's directory
            def resolve_path(p: str) -> str:
                if os.path.isabs(p):
                    return p
                if os.path.exists(p):
                    return p
                base = os.path.dirname(os.path.abspath(__file__))
                candidate = os.path.join(base, p)
                return candidate

            tp = resolve_path(toolkit_path)

            # Require a directory
            if not os.path.isdir(tp):
                return {}

            init_path = os.path.join(tp, "__init__.py")
            if not os.path.isfile(init_path):
                return {}

            # Mark as a package so relative imports inside __init__.py work
            spec = importlib.util.spec_from_file_location(
                "xagent_dynamic_toolkit",
                init_path,
                submodule_search_locations=[tp],
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules["xagent_dynamic_toolkit"] = module
                spec.loader.exec_module(module)  # type: ignore[attr-defined]
                registry = getattr(module, "TOOLKIT_REGISTRY", {})
                if isinstance(registry, dict):
                    return registry
        except Exception as e:
            print(f"Warning: failed to load TOOLKIT_REGISTRY from {toolkit_path}: {e}")
        return {}
    
    def _initialize_agent(self) -> Agent:
        """Initialize the agent with tools and configuration."""
        agent_cfg = self.config.get("agent", {})
        
        # Load tools from built-in registry and optional toolkit registry
        tool_names = agent_cfg.get("tools", [])
        toolkit_registry = self._load_toolkit_registry(self.toolkit_path)
        combined_registry: Dict[str, Any] = {**TOOL_REGISTRY, **toolkit_registry}
        tools = [combined_registry[name] for name in tool_names if name in combined_registry]
        
        return Agent(
            name=agent_cfg.get("name"),
            system_prompt=agent_cfg.get("system_prompt"),
            model=agent_cfg.get("model"),
            tools=tools,
            mcp_servers=agent_cfg.get("mcp_servers"),
        )
    
    def _initialize_message_db(self) -> Optional[MessageDB]:
        """Initialize message database based on configuration."""
        agent_cfg = self.config.get("agent", {})
        use_local_session = agent_cfg.get("use_local_session", True)
        return None if use_local_session else MessageDB()
    
    def _create_app(self) -> FastAPI:
        """Create and configure FastAPI application."""
        app = FastAPI(
            title="xAgent HTTP Agent Server",
            description="HTTP API for xAgent conversational AI",
            version="1.0.0"
        )
        
        # Add routes
        self._add_routes(app)
        
        return app
    
    def _add_routes(self, app: FastAPI) -> None:
        """Add API routes to the FastAPI application."""
        
        @app.get("/health")
        async def health_check():
            """Health check endpoint."""
            return {"status": "healthy", "service": "xAgent HTTP Server"}
        
        @app.post("/chat")
        async def chat(input_data: AgentInput):
            """
            Chat endpoint for agent interaction.
            
            Args:
                input_data: User input containing message and metadata
                
            Returns:
                Agent response or streaming SSE when input_data.stream is True
            """
            try:
                session = Session(
                    user_id=input_data.user_id,
                    session_id=input_data.session_id,
                    message_db=self.message_db
                )
                
                # Streaming mode via Server-Sent Events
                if input_data.stream:
                    async def event_generator():
                        try:
                            response = await self.agent(
                                user_message=input_data.user_message,
                                session=session,
                                image_source=input_data.image_source,
                                stream=True
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
                                yield f"data: {json.dumps({'message': str(response)})}\n\n"
                                yield "data: [DONE]\n\n"
                        except Exception as e:
                            # Stream error as SSE, client can handle gracefully
                            yield f"data: {json.dumps({'error': str(e)})}\n\n"
                            yield "data: [DONE]\n\n"
                    return StreamingResponse(event_generator(), media_type="text/event-stream")
                
                # Non-streaming mode (default)
                response = await self.agent(
                    user_message=input_data.user_message,
                    session=session,
                    image_source=input_data.image_source
                )
                
                return {"reply": str(response)}
                
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Agent processing error: {str(e)}")
        
        @app.post("/clear_session")
        async def clear_session(input_data: ClearSessionInput):
            """
            Clear session data endpoint.
            
            Args:
                input_data: Contains user_id and session_id to clear
                
            Returns:
                Success confirmation
            """
            try:
                session = Session(
                    user_id=input_data.user_id,
                    session_id=input_data.session_id,
                    message_db=self.message_db
                )
                
                await session.clear_session()
                
                return {"status": "success", "message": f"Session {input_data.session_id} for user {input_data.user_id} cleared"}
                
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to clear session: {str(e)}")

    
    def run(self, host: str = None, port: int = None) -> None:
        """
        Run the HTTP server.
        
        Args:
            host: Host to bind to
            port: Port to bind to
        """
        server_cfg = self.config.get("server", {})
        
        # Use provided args or fall back to config defaults
        host = host or server_cfg.get("host", "0.0.0.0")
        port = port or server_cfg.get("port", 8010)
        
        print(f"Starting xAgent HTTP Server on {host}:{port}")
        print(f"Agent: {self.agent.name}")
        print(f"Model: {self.agent.model}")
        print(f"Tools: {len(self.agent.tools)} loaded")
        
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
        # Default config path when used as module
        _server_instance = HTTPAgentServer("config/agent.yaml")
    return _server_instance.app


def get_app_lazy() -> FastAPI:
    """Lazy initialization for global app variable."""
    return get_app()


# For backward compatibility - use lazy initialization to avoid import-time errors
app = None  # Will be initialized when first accessed


def main():
    """Main entry point for xagent-server command."""
    parser = argparse.ArgumentParser(description="xAgent HTTP Server")
    parser.add_argument("--config", default="config/agent.yaml", help="Config file path")
    parser.add_argument("--toolkit_path", default="toolkit", help="Toolkit directory path")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    
    args = parser.parse_args()
    
    try:
        server = HTTPAgentServer(config_path=args.config, toolkit_path=args.toolkit_path)
        server.run(host=args.host, port=args.port)
    except Exception as e:
        print(f"Failed to start server: {e}")
        raise


if __name__ == "__main__":
    main()