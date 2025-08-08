import os
import yaml
import uvicorn
import argparse
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

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


class HTTPAgentServer:
    """HTTP Agent Server for xAgent."""
    
    def __init__(self, config_path: str = "config/agent.yaml"):
        """
        Initialize HTTPAgentServer.
        
        Args:
            config_path: Path to configuration file
        """
        # Load environment variables
        load_dotenv(override=True)
        
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
            # Support relative path lookup
            base = os.path.dirname(os.path.abspath(__file__))
            abs_path = os.path.join(base, cfg_path)
            if not os.path.isfile(abs_path):
                raise FileNotFoundError(f"Cannot find config file at {cfg_path} or {abs_path}")
            cfg_path = abs_path
            
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    
    def _initialize_agent(self) -> Agent:
        """Initialize the agent with tools and configuration."""
        agent_cfg = self.config.get("agent", {})
        
        # Load tools
        tool_names = agent_cfg.get("tools", [])
        tools = [TOOL_REGISTRY[name] for name in tool_names if name in TOOL_REGISTRY]
        
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
                Agent response
            """
            try:
                session = Session(
                    user_id=input_data.user_id,
                    session_id=input_data.session_id,
                    message_db=self.message_db
                )
                
                response = await self.agent(
                    user_message=input_data.user_message,
                    session=session,
                    image_source=input_data.image_source
                )
                
                return {"reply": str(response)}
                
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Agent processing error: {str(e)}")
    
    def run(self, host: str = None, port: int = None, reload: bool = None) -> None:
        """
        Run the HTTP server.
        
        Args:
            host: Host to bind to
            port: Port to bind to
            reload: Enable auto-reload for development
        """
        server_cfg = self.config.get("server", {})
        
        # Use provided args or fall back to config defaults
        host = host or server_cfg.get("host", "0.0.0.0")
        port = port or server_cfg.get("port", 8010)
        reload = reload if reload is not None else server_cfg.get("debug", False)
        
        print(f"Starting xAgent HTTP Server on {host}:{port}")
        print(f"Agent: {self.agent.name}")
        print(f"Model: {self.agent.model}")
        print(f"Tools: {len(self.agent.tools)} loaded")
        
        uvicorn.run(
            self.app,
            host=host,
            port=port,
            reload=reload,
        )


# Global server instance for uvicorn module loading
_server_instance = None


def get_app() -> FastAPI:
    """Get the FastAPI app instance for uvicorn."""
    global _server_instance
    if _server_instance is None:
        _server_instance = HTTPAgentServer()
    return _server_instance.app


# For backward compatibility
app = get_app()


def main():
    """Main entry point for xagent-server command."""
    parser = argparse.ArgumentParser(description="xAgent HTTP Server")
    parser.add_argument("--config", default="config/agent.yaml", help="Config file path")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    
    args = parser.parse_args()
    
    try:
        server = HTTPAgentServer(config_path=args.config)
        server.run(host=args.host, port=args.port, reload=args.reload)
    except Exception as e:
        print(f"Failed to start server: {e}")
        raise


if __name__ == "__main__":
    main()