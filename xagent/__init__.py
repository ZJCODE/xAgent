"""xAgent - A powerful multi-modal conversational AI system."""

from .__version__ import (
    __version__,
    __version_info__,
    __title__,
    __description__,
    __author__,
    __author_email__,
    __license__,
    __copyright__,
    __url__,
)

# Core imports
from .core.agent import Agent
from .core.session import Session
from .core.server import HTTPAgentServer
from .db.message import MessageDB
from .schemas.message import Message
from .utils.tool_decorator import function_tool

# Version info
version = __version__

__all__ = [
    # Version info
    "__version__",
    "__version_info__",
    "version",
    # Core classes
    "Agent",
    "Session", 
    "HTTPAgentServer",
    "MessageDB",
    "Message",
    # Utilities
    "function_tool",
]
