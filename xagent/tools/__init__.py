from .memory_tool import (
    create_correct_memory_tool,
    create_forget_memory_tool,
    create_recall_memory_tool,
    create_remember_tool,
    create_search_history_tool,
)
from .search_tool import create_web_search_tool
from .shell_tool import run_command

__all__ = [
    "run_command",
    "create_correct_memory_tool",
    "create_forget_memory_tool",
    "create_recall_memory_tool",
    "create_remember_tool",
    "create_search_history_tool",
    "create_web_search_tool",
]

TOOL_REGISTRY = {
    "run_command": run_command,
}
