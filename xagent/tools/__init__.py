from .openai_tool import web_search, draw_image
from .shell_tool import run_command

__all__ = ["web_search", "draw_image", "run_command"]

TOOL_REGISTRY = {
    "web_search": web_search,
    "draw_image": draw_image,
    "run_command": run_command,
}