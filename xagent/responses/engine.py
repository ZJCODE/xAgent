"""High-level entrypoint for Responses tasks."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from ..core.agent import Agent
from .executor import ResponsesExecutor
from .memory import ResponsesMemoryManager
from .models import TaskContext, TaskResult
from .planner import TaskPlanner
from .tools import split_tools
from .workflow import ResponsesWorkflowRunner


class ResponsesEngine:
    """Complex reasoning, search, MCP, and workflow execution runtime."""

    def __init__(
        self,
        agent: Agent,
        workflow_cls: type[ResponsesWorkflowRunner] = ResponsesWorkflowRunner,
    ):
        self.agent = agent
        self.executor = ResponsesExecutor(agent=agent)
        self.planner = TaskPlanner()
        self.memory = ResponsesMemoryManager(getattr(agent, "memory_storage", None))
        self.workflow_cls = workflow_cls
        self.realtime_tools, self.responses_tools = split_tools(agent.tools.values())

    async def run(
        self,
        task: str,
        context: TaskContext,
        stream_callback: Optional[Callable[[str], Awaitable[None] | None]] = None,
    ) -> TaskResult:
        plan = self.planner.create_plan(task)
        result = await self.executor.execute(
            user_message=task,
            context=context,
            stream_callback=stream_callback,
        )
        result.plan = plan
        result.metadata.update(
            {
                "realtime_tools": [getattr(tool, "__name__", "tool") for tool in self.realtime_tools],
                "responses_tools": [getattr(tool, "__name__", "tool") for tool in self.responses_tools],
            }
        )
        return result

    def create_workflow(self, name: Optional[str] = None) -> ResponsesWorkflowRunner:
        return self.workflow_cls(name=name)
