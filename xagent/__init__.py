"""xAgent public package exports."""

from __future__ import annotations

import importlib

from .__version__ import __version__

__all__ = [
    "AgentHTTPServer",
    "AgentCLI",
    "AgentOrchestrator",
    "ConversationRecord",
    "ConversationStateStore",
    "InterruptController",
    "JobManager",
    "RealtimeClientEvent",
    "RealtimeGateway",
    "RealtimeServerEvent",
    "RealtimeSessionManager",
    "RealtimeTool",
    "ResponsesEngine",
    "ResponsesTool",
    "ResponsesWorkflowRunner",
    "TaskContext",
    "TaskPlan",
    "TaskResult",
    "ToolPolicy",
    "TurnInput",
    "Message",
    "function_tool",
    "web_search",
    "draw_image",
    "Swarm",
    "Workflow",
    "__version__",
]

_EXPORTS = {
    "AgentHTTPServer": (".interfaces.server", "AgentHTTPServer"),
    "AgentCLI": (".interfaces.cli", "AgentCLI"),
    "AgentOrchestrator": (".orchestrator", "AgentOrchestrator"),
    "ConversationRecord": (".state", "ConversationRecord"),
    "ConversationStateStore": (".state", "ConversationStateStore"),
    "InterruptController": (".realtime", "InterruptController"),
    "JobManager": (".orchestrator", "JobManager"),
    "RealtimeClientEvent": (".realtime", "RealtimeClientEvent"),
    "RealtimeGateway": (".realtime", "RealtimeGateway"),
    "RealtimeServerEvent": (".realtime", "RealtimeServerEvent"),
    "RealtimeSessionManager": (".realtime", "RealtimeSessionManager"),
    "RealtimeTool": (".responses", "RealtimeTool"),
    "ResponsesEngine": (".responses", "ResponsesEngine"),
    "ResponsesTool": (".responses", "ResponsesTool"),
    "ResponsesWorkflowRunner": (".responses", "ResponsesWorkflowRunner"),
    "TaskContext": (".responses", "TaskContext"),
    "TaskPlan": (".responses", "TaskPlan"),
    "TaskResult": (".responses", "TaskResult"),
    "ToolPolicy": (".orchestrator", "ToolPolicy"),
    "TurnInput": (".orchestrator", "TurnInput"),
    "Message": (".schemas", "Message"),
    "function_tool": (".utils", "function_tool"),
    "web_search": (".tools.openai_tool", "web_search"),
    "draw_image": (".tools.openai_tool", "draw_image"),
    "Swarm": (".multi.swarm", "Swarm"),
    "Workflow": (".multi.workflow", "Workflow"),
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)
