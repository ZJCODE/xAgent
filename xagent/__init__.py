"""
xAgent - Multi-Modal AI Agent System

A powerful multi-modal AI Agent system with modern architecture.
"""

import importlib
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
    "Workflow",
    
    # Meta
    "__version__"
]

_EXPORTS = {
    "Agent": (".core", "Agent"),
    "AgentHTTPServer": (".interfaces", "AgentHTTPServer"),
    "AgentCLI": (".interfaces", "AgentCLI"),
    "Message": (".schemas", "Message"),
    "function_tool": (".utils", "function_tool"),
    "web_search": (".tools", "web_search"),
    "draw_image": (".tools", "draw_image"),
    "Workflow": (".multi", "Workflow"),
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
