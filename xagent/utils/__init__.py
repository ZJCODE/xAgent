"""Utilities for xAgent package."""

import importlib

__all__ = ["function_tool", "MCPTool", "file_to_data_uri"]

_EXPORTS = {
    "function_tool": (".tool_decorator", "function_tool"),
    "MCPTool": (".mcp_convertor", "MCPTool"),
    "file_to_data_uri": (".image_utils", "file_to_data_uri"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    try:
        module = importlib.import_module(module_name, __name__)
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] == "fastmcp":
            raise ImportError(
                "MCP support requires the optional fastmcp dependency."
            ) from exc
        raise
    except Exception as exc:
        if name == "MCPTool":
            raise ImportError(
                "MCP support is unavailable because the fastmcp dependency failed to initialize."
            ) from exc
        raise
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)
