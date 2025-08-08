from .openai_tool import web_search,draw_image

__all__ = ["web_search", "draw_image"]

TOOL_REGISTRY = {
    "web_search": web_search,
    "draw_image": draw_image,
}