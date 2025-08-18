"""Workflow management for multi-agent coordination."""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union, Tuple
from collections import Counter, defaultdict
import uuid
import time

from ..core.agent import Agent
from ..schemas import Message


class WorkflowPatternType(Enum):
    """Types of workflow orchestration patterns."""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class WorkflowResult:
    """Result container for workflow execution."""
    
    def __init__(
        self,
        result: Any,
        execution_time: float,
        pattern: WorkflowPatternType,
        metadata: Optional[Dict[str, Any]] = None
    ):
        self.result = result
        self.execution_time = execution_time
        self.pattern = pattern
        self.metadata = metadata or {}
        self.timestamp = time.time()
    
    def __str__(self):
        return f"WorkflowResult(pattern={self.pattern.value}, time={self.execution_time:.2f}s)"


class BaseWorkflowPattern(ABC):
    """Abstract base class for workflow patterns."""
    
    def __init__(self, agents: List[Agent], name: Optional[str] = None):
        self.agents = agents
        self.name = name or f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
    
    @abstractmethod
    async def execute(self, task: Union[str, Message], **kwargs) -> WorkflowResult:
        """Execute the workflow pattern."""
        pass
    
    def _validate_agents(self):
        """Validate that agents are properly configured."""
        if not self.agents:
            raise ValueError("At least one agent is required")
        
        for i, agent in enumerate(self.agents):
            if not isinstance(agent, Agent):
                raise TypeError(f"Agent at index {i} must be an instance of Agent")


class SequentialPipeline(BaseWorkflowPattern):
    """
    Sequential Pipeline Pattern: Agent A → Agent B → Agent C → Result
    
    Pure sequential processing where each agent's output becomes the next agent's input.
    This is the fundamental nature of pipeline processing - there's no meaningful scenario
    where you wouldn't want context passing in a sequential workflow.
    
    Use cases:
    - Multi-step task decomposition (research → analysis → summary)
    - Progressive refinement (draft → review → polish)
    - Chain of reasoning (premise → logic → conclusion)
    """
    
    async def execute(
        self, 
        task: Union[str, Message], 
        intermediate_results: bool = False
    ) -> WorkflowResult:
        """
        Execute agents in sequence, with each agent's output feeding the next.
        
        The first agent receives the original task, and each subsequent agent 
        receives the previous agent's output as input. This is the fundamental
        nature of sequential processing.
        
        Args:
            task: Initial task or message for the first agent
            intermediate_results: Whether to include intermediate results in metadata
            
        Returns:
            WorkflowResult with final output and execution metadata
        """
        start_time = time.time()
        self._validate_agents()
        
        current_input = str(task)
        results = []
        
        for i, agent in enumerate(self.agents):
            self.logger.info(f"Executing agent {i+1}/{len(self.agents)}: {agent.name}")
            
            try:
                # Each agent processes the current input (original task for first agent, 
                # previous agent's output for subsequent agents)
                result = await agent.chat(current_input)
                results.append(result)
                
                # The output becomes the input for the next agent
                current_input = str(result)
                
            except Exception as e:
                self.logger.error(f"Agent {agent.name} failed: {e}")
                raise RuntimeError(f"Sequential pipeline failed at agent {i+1}: {e}")
        
        execution_time = time.time() - start_time
        
        metadata = {
            "agents_used": [agent.name for agent in self.agents],
            "steps_completed": len(results)
        }
        
        if intermediate_results:
            metadata["intermediate_results"] = results[:-1]
        
        return WorkflowResult(
            result=results[-1],
            execution_time=execution_time,
            pattern=WorkflowPatternType.SEQUENTIAL,
            metadata=metadata
        )


class ParallelPattern(BaseWorkflowPattern):
    """
    Simplified Parallel Pattern: Input → [Agent1, Agent2, Agent3] → Aggregator → Result
    
    Supports two core modes:
    - Split Mode: Task is divided into focused sub-tasks for different agents (MapReduce style)
    - Consensus Mode: Same task is given to all agents for validation/consensus
    - Custom Mode: Custom inputs can be provided for each agent
    
    Results are intelligently aggregated using LLM-based synthesis.
    """
    
    def __init__(
        self, 
        worker_agents: List[Agent], 
        aggregator_agent: Optional[Agent] = None,
        name: Optional[str] = None
    ):
        all_agents = worker_agents + ([aggregator_agent] if aggregator_agent else [])
        super().__init__(all_agents, name)
        self.worker_agents = worker_agents
        self.aggregator_agent = aggregator_agent or worker_agents[0]  # Use first worker as default aggregator
    
    async def execute(
        self,
        task: Union[str, Message],
        split_task: bool = True,
        custom_inputs: Optional[List[str]] = None,
        max_concurrent: int = 10
    ) -> WorkflowResult:
        """
        Execute parallel pattern with simplified interface.
        
        Args:
            task: Task to be processed
            split_task: If True, split task into focused sub-tasks; If False, all agents get same task
            custom_inputs: Optional list of custom inputs for each worker (overrides split_task)
            max_concurrent: Maximum concurrent worker executions
            
        Returns:
            WorkflowResult with aggregated output
        """
        start_time = time.time()
        self._validate_agents()
        
        # Determine worker inputs based on parameters
        if custom_inputs:
            # Custom inputs provided - use directly
            worker_inputs = custom_inputs
            mode = "custom"
        elif split_task:
            # Split task into focused sub-tasks (MapReduce style)
            worker_inputs = self._split_task_intelligently(str(task))
            mode = "split"
        else:
            # Same task for all workers (Consensus style)
            worker_inputs = [str(task)] * len(self.worker_agents)
            mode = "consensus"
        
        if len(worker_inputs) != len(self.worker_agents):
            raise ValueError(f"Number of inputs ({len(worker_inputs)}) must match number of workers ({len(self.worker_agents)})")
        
        # Execute workers in parallel
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def execute_worker(agent: Agent, input_task: str) -> Tuple[str, str]:
            async with semaphore:
                try:
                    result = await agent.chat(input_task)
                    return (agent.name, result)
                except Exception as e:
                    self.logger.error(f"Worker {agent.name} failed: {e}")
                    return (agent.name, f"Error: {e}")
        
        self.logger.info(f"Executing {mode} pattern with {len(self.worker_agents)} workers")
        
        worker_tasks = [
            execute_worker(agent, input_task) 
            for agent, input_task in zip(self.worker_agents, worker_inputs)
        ]
        
        worker_results = await asyncio.gather(*worker_tasks)
        
        # Aggregate results using intelligent merging
        final_result = await self._aggregate_results_intelligently(
            worker_results, 
            str(task)
        )
        
        execution_time = time.time() - start_time
        
        metadata = {
            "worker_agents": [agent.name for agent in self.worker_agents],
            "aggregator_agent": self.aggregator_agent.name,
            "execution_mode": mode,
            "worker_results": dict(worker_results)
        }
        
        return WorkflowResult(
            result=final_result,
            execution_time=execution_time,
            pattern=WorkflowPatternType.PARALLEL,
            metadata=metadata
        )
    
    def _split_task_intelligently(self, task: str) -> List[str]:
        """
        Intelligently split task for parallel processing.
        Uses a more flexible approach that adapts to the number of agents.
        """
        base_prompt = f"Focus on this specific aspect of the task: {task}\n\nYour specific focus area:"
        
        # Dynamic focus areas based on common analysis dimensions
        focus_areas = [
            "Analyze the data quality and accuracy aspects",
            "Focus on trends and patterns identification", 
            "Examine edge cases and outliers",
            "Evaluate overall sentiment and themes",
            "Look for actionable insights and recommendations",
            "Consider technical implementation details",
            "Assess potential risks and challenges",
            "Explore creative and innovative approaches"
        ]
        
        # Assign focus areas to agents
        assigned_areas = []
        for i in range(len(self.worker_agents)):
            area = focus_areas[i % len(focus_areas)]
            assigned_areas.append(f"{base_prompt} {area}")
        
        return assigned_areas
    
    async def _aggregate_results_intelligently(
        self, 
        worker_results: List[Tuple[str, str]], 
        original_task: str
    ) -> str:
        """
        Intelligently aggregate worker results using LLM-based synthesis.
        Simplified from multiple strategies to focus on what works best.
        """
        # Check if all results are identical (consensus case)
        results = [result for _, result in worker_results]
        if len(set(results)) == 1:
            return results[0]  # Perfect consensus
        
        # LLM-based intelligent merging for different results
        combined_results = "\n\n".join([
            f"Agent {name} result:\n{result}" 
            for name, result in worker_results
        ])
        
        merge_prompt = f"""
        Analyze and synthesize the following results from multiple agents working on the same task:
        
        Original task: {original_task}
        
        Agent results:
        {combined_results}
        
        Provide a comprehensive response that combines the best insights from all agents, 
        resolves any conflicts, and delivers a unified final answer.
        """
        
        return await self.aggregator_agent.chat(merge_prompt)


class Workflow:
    """
    Main workflow orchestrator that supports multiple orchestration patterns.
    Provides a unified interface for executing different workflow patterns.
    """
    
    def __init__(self, name: Optional[str] = None):
        self.name = name or f"workflow_{uuid.uuid4().hex[:8]}"
        self.logger = logging.getLogger("Workflow")
        self.execution_history: List[WorkflowResult] = []
    
    def sequential(self, agents: List[Agent]) -> SequentialPipeline:
        """Create a sequential pipeline workflow."""
        return SequentialPipeline(agents, f"{self.name}_sequential")
    
    def parallel(
        self, 
        worker_agents: List[Agent], 
        aggregator_agent: Optional[Agent] = None
    ) -> ParallelPattern:
        """Create a parallel workflow (replaces both MapReduce and Consensus patterns)."""
        return ParallelPattern(worker_agents, aggregator_agent, f"{self.name}_parallel")
    
    async def execute_pattern(
        self,
        pattern: BaseWorkflowPattern,
        task: Union[str, Message],
        **kwargs
    ) -> WorkflowResult:
        """Execute a workflow pattern and track results."""
        try:
            result = await pattern.execute(task, **kwargs)
            self.execution_history.append(result)
            
            self.logger.info(
                f"Workflow {pattern.name} completed in {result.execution_time:.2f}s "
                f"using {result.pattern.value} pattern"
            )
            
            return result
            
        except Exception as e:
            self.logger.error(f"Workflow execution failed: {e}")
            raise
    
    def get_execution_stats(self) -> Dict[str, Any]:
        """Get statistics about workflow executions."""
        if not self.execution_history:
            return {"total_executions": 0}
        
        pattern_counts = Counter(result.pattern for result in self.execution_history)
        avg_execution_time = sum(result.execution_time for result in self.execution_history) / len(self.execution_history)
        
        return {
            "total_executions": len(self.execution_history),
            "pattern_usage": {pattern.value: count for pattern, count in pattern_counts.items()},
            "average_execution_time": avg_execution_time,
            "fastest_execution": min(result.execution_time for result in self.execution_history),
            "slowest_execution": max(result.execution_time for result in self.execution_history)
        }