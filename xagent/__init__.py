"""
xAgent - Multi-Modal AI Agent System

A powerful multi-modal AI Agent system with modern architecture.
"""

from .core import Session, Agent, HTTPAgentServer
from .schemas import Message, ToolCall
from .db import MessageDB
from .utils import function_tool, MCPTool, upload_image
from .tools import web_search, draw_image
from .multi import Swarm, Workflow
from .__version__ import __version__

__all__ = [
    # Core components
    "Session",
    "Agent", 
    "HTTPAgentServer",
    
    # Data models
    "Message",
    "ToolCall",
    "MessageDB",
    
    # Utilities
    "function_tool",
    "MCPTool", 
    "upload_image",
    
    # Built-in tools
    "web_search",
    "draw_image",

    # Multi-agent
    "Swarm",
    "Workflow",
    
    # Meta
    "__version__"
]
