"""Multi-agent capabilities for xAgent."""

from .swarm import Swarm
from .workflow import (
    Workflow,
    WorkflowPatternType,
    WorkflowResult,
    SequentialPipeline,
    ParallelPattern,
    BaseWorkflowPattern
)

__all__ = [
    "Swarm", 
    "Workflow",
    "WorkflowPatternType",
    "WorkflowResult", 
    "SequentialPipeline",
    "ParallelPattern",
    "BaseWorkflowPattern"
]
