"""
xAgent - Multi-Modal AI Agent System

A powerful multi-modal AI Agent system with modern architecture.
"""

import importlib

from .core import Agent
from .interfaces import AgentHTTPServer, AgentCLI
from .schemas import Message
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
    "MessageStorageLocal",
    "MessageStorageCloud",
    "MemoryStorageCloud",
    
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

_EXPORTS = {
    "MessageStorageBase": (".components", "MessageStorageBase"),
    "MessageStorageLocal": (".components", "MessageStorageLocal"),
    "MessageStorageCloud": (".components", "MessageStorageCloud"),
    "MemoryStorageCloud": (".components", "MemoryStorageCloud"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
