import os
import yaml
import uvicorn
import argparse
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import json
from fastapi.responses import StreamingResponse

from ..core.base import BaseAgentRunner
from ..core.session import Session


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


class HTTPAgentServer(BaseAgentRunner):
    """HTTP Agent Server for xAgent."""
    
    def __init__(self, config_path: Optional[str] = None, toolkit_path: Optional[str] = None):
        """
        Initialize HTTPAgentServer.
        
        Args:
            config_path: Path to configuration file (if None, uses default configuration)
            toolkit_path: Path to toolkit directory (if None, no additional tools will be loaded)
        """
        # Initialize the base agent runner
        super().__init__(config_path, toolkit_path)
        
        # Initialize FastAPI app
        self.app = self._create_app()
        
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
        # Use default configuration when used as module
        _server_instance = HTTPAgentServer()
    return _server_instance.app


def get_app_lazy() -> FastAPI:
    """Lazy initialization for global app variable."""
    return get_app()


# For backward compatibility - use lazy initialization to avoid import-time errors
app = None  # Will be initialized when first accessed


def main():
    """Main entry point for xagent-server command."""
    parser = argparse.ArgumentParser(description="xAgent HTTP Server")
    parser.add_argument("--config", default=None, help="Config file path (if not specified, uses default configuration)")
    parser.add_argument("--toolkit_path", default=None, help="Toolkit directory path (if not specified, no additional tools will be loaded)")
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