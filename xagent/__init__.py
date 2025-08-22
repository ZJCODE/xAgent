"""
xAgent - Multi-Modal AI Agent System

A powerful multi-modal AI Agent system with modern architecture.
"""

from .core import Agent
from .interfaces import AgentHTTPServer, AgentCLI
from .schemas import Message
from .components import MessageStorageBase, MessageStorageRedis, MessageStorageLocal
from .utils import function_tool
from .tools import web_search, draw_image
from .multi import Swarm, Workflow
from .__version__ import __version__

__all__ = [
    # Core components
    "Agent", 

    # interfaces
    "AgentHTTPServer",
    "AgentCLI",
    
    # Data models
    "Message",

    # Database
    "MessageStorageBase",
    "MessageStorageRedis",
    "MessageStorageLocal",
    
    # Utilities
    "function_tool",

    # Built-in tools
    "web_search",
    "draw_image",

    # Multi-agent
    "Swarm",
    "Workflow",
    
    # Meta
    "__version__"
]
