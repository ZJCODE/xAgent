"""Workflow management for multi-agent coordination."""

import asyncio
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import uuid
import time

from ..core.agent import Agent

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


class BaseWorkflow(ABC):
    """Abstract base class for workflow patterns."""
    
    def __init__(self, agents: List[Agent], name: Optional[str] = None):
        self.agents = agents
        self.name = name or f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
    
    @abstractmethod
    async def execute(self, user_id: str, task: str, image_source: Optional[str] = None, **kwargs) -> WorkflowResult:
        """Execute the workflow pattern."""
        pass
    
    def _validate_agents(self):
        """Validate that agents are properly configured."""
        if not self.agents:
            raise ValueError("At least one agent is required")
        
        for i, agent in enumerate(self.agents):
            if not isinstance(agent, Agent):
                raise TypeError(f"Agent at index {i} must be an instance of Agent")


class SequentialWorkflow(BaseWorkflow):
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
        user_id: str,
        task: str, 
        image_source: Optional[str] = None,
        intermediate_results: bool = False
    ) -> WorkflowResult:
        """
        Execute agents in sequence, with each agent's output feeding the next.
        
        The first agent receives the original task, and each subsequent agent 
        receives the previous agent's output as input. This is the fundamental
        nature of sequential processing.
        
        Args:
            task: Initial task string for the first agent
            image_source: Optional image source (URL, file path, or base64 string)
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
                if i == 0:
                    result = await agent.chat(
                        user_message=current_input, 
                        user_id=user_id, 
                        session_id=str(uuid.uuid4()),
                        image_source=image_source
                    )
                else:
                    result = await agent.chat(
                        user_message=current_input, 
                        user_id=user_id, 
                        session_id=str(uuid.uuid4())
                    )
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


class ParallelWorkflow(BaseWorkflow):
    """
    Parallel Pattern for consensus building, validation, and multi-perspective synthesis.
    
    Same input, same processing (redundancy for reliability and diverse perspectives)
    - Use case: Critical decisions, consensus building, error reduction, multi-perspective analysis
    - Example: Multiple agents independently solve same problem for validation or provide different expert perspectives
    
    Key capabilities:
    1. Consensus Building: When agents provide similar solutions, build consensus or select the best
    2. Multi-Perspective Synthesis: When agents provide different valid perspectives, integrate insights
    3. Quality Validation: Evaluate and validate the quality of all responses
    4. Comprehensive Analysis: Combine consensus building with synthesis for robust results
    """
    
    def __init__(
        self, 
        agents: List[Agent], 
        name: Optional[str] = None
    ):
        """
        Initialize parallel pattern for broadcast consensus building.
        
        Args:
            agents: Agents that perform the actual work
            name: Optional name for the pattern
        """
        # Create internal coordinator agent for consensus building
        validator_name = f"consensus_validator_{uuid.uuid4().hex[:8]}"
        
        self.consensus_validator = Agent(
            name=validator_name,
            description="Consensus validator and synthesizer agent for parallel processing"
        )
        
        all_agents = agents + [self.consensus_validator]
        super().__init__(all_agents, name)
        self.agents = agents
    
    async def execute(
        self,
        user_id: str,
        task: str,
        image_source: Optional[str] = None,
        max_concurrent: int = 10,
    ) -> WorkflowResult:
        """
        Execute broadcast pattern.
        
        Args:
            task: Task string to be processed by all agents
            image_source: Optional image source (URL, file path, or base64 string)
            max_concurrent: Maximum concurrent worker executions
            
        Returns:
            WorkflowResult with consensus or best validated output
        """
        start_time = time.time()
        self._validate_agents()
        
        # Prepare inputs - same task for all agents in parallel mode
        task_str = str(task)
        worker_inputs = [task_str] * len(self.agents)
        
        # Execute workers in parallel
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def execute_worker(agent: Agent, input_task: str) -> Tuple[str, str]:
            async with semaphore:
                try:
                    result = await agent.chat(
                        user_message=input_task,
                        user_id=user_id,
                        session_id=str(uuid.uuid4()),
                        image_source=image_source
                    )
                    return (agent.name, result)
                except Exception as e:
                    self.logger.error(f"Worker {agent.name} failed: {e}")
                    return (agent.name, f"Error: {e}")

        self.logger.info(f"Executing parallel pattern with {len(self.agents)} workers")

        worker_tasks = [
            execute_worker(agent, input_task) 
            for agent, input_task in zip(self.agents, worker_inputs)
        ]
        
        worker_results = await asyncio.gather(*worker_tasks)
        
        # Aggregate results 
        final_result = await self._aggregate_parallel_results(
            user_id=user_id,
            worker_results=worker_results,
            original_task=task
        )

        execution_time = time.time() - start_time
        
        metadata = {
            "agents": [agent.name for agent in self.agents],
            "consensus_validator": self.consensus_validator.name,
            "pattern_type": "parallel",
            "worker_results": dict(worker_results)
        }
        
        return WorkflowResult(
            result=final_result,
            execution_time=execution_time,
            pattern=WorkflowPatternType.PARALLEL,
            metadata=metadata
        )

    async def _aggregate_parallel_results(
        self, 
        user_id: str,
        worker_results: List[Tuple[str, str]], 
        original_task: str
    ) -> str:
        """Enhanced consensus validation and synthesis from parallel processing."""
        results = [result for _, result in worker_results]
        
        # Check for perfect consensus
        if len(set(results)) == 1:
            return results[0]
        
        # Enhanced aggregation with both consensus and synthesis capabilities
        perspective_results = "\n\n---\n\n".join([
            f"Agent {name}'s perspective:\n{result}" 
            for name, result in worker_results
        ])
        
        prompt = f"""
You are acting as both a validator and synthesizer. Multiple agents independently worked on the same task.
Your role is to analyze their results and provide the best possible response through either consensus building or synthesis.

Original task: {original_task}

Agent Results:
{chr(10).join([f"{i+1}. Agent {name}: {result}" for i, (name, result) in enumerate(worker_results)])}

---

Detailed Perspectives:
{perspective_results}

Your comprehensive approach:

CONSENSUS ANALYSIS:
1. If there's clear consensus among results, summarize the agreed-upon answer
2. If results differ significantly, evaluate quality and select the superior response
3. Explain your reasoning for the final choice
4. Highlight any important minority opinions that should be considered

SYNTHESIS CAPABILITIES:
When results represent different valid perspectives rather than competing answers:
1. Integrate insights from all perspectives
2. Resolve any conflicts between different viewpoints  
3. Deliver a well-rounded, multi-faceted final answer
4. Highlight complementary insights and trade-offs

Choose the most appropriate approach (consensus or synthesis) based on the nature of the responses, and provide a comprehensive final answer.
        """
        
        return await self.consensus_validator.chat(user_message=prompt, user_id=user_id, session_id=str(uuid.uuid4()))


class Workflow:
    """
    Main workflow orchestrator that supports multiple orchestration patterns.
    Provides a unified interface for executing different workflow patterns.
    """
    
    def __init__(self, name: Optional[str] = None):
        self.name = name or f"workflow_{uuid.uuid4().hex[:8]}"
        self.logger = logging.getLogger("Workflow")

    # Direct execution methods - simplified API
    async def run_sequential(
        self,
        agents: List[Agent],
        task: str,
        image_source: Optional[str] = None,
        intermediate_results: bool = False
    ) -> WorkflowResult:
        """
        Directly execute a sequential pipeline in one call.
        
        Args:
            agents: List of agents to execute in sequence
            task: Initial task string for the first agent
            image_source: Optional image source (URL, file path, or base64 string)
            intermediate_results: Whether to include intermediate results in metadata
            
        Returns:
            WorkflowResult with final output and execution metadata
        """
        pattern = SequentialWorkflow(agents, f"{self.name}_sequential")
        result = await pattern.execute(
            user_id="default_user",
            task=task, 
            image_source=image_source,
            intermediate_results=intermediate_results
        )
        
        self.logger.info(
            f"Workflow {pattern.name} completed in {result.execution_time:.2f}s "
            f"using {result.pattern.value} pattern"
        )
        
        return result
    
    async def run_parallel(
        self,
        agents: List[Agent],
        task: str,
        image_source: Optional[str] = None,
        max_concurrent: int = 10
    ) -> WorkflowResult:
        """
        Directly execute parallel processing in one call.
        
        Args:
            agents: Multiple agents for redundant processing
            task: Task string to be processed by all agents
            image_source: Optional image source (URL, file path, or base64 string)
            max_concurrent: Maximum concurrent worker executions
            
        Returns:
            WorkflowResult with consensus or best validated output
        """
        pattern = ParallelWorkflow(agents, f"{self.name}_parallel")
        result = await pattern.execute(
            user_id="default_user",
            task=task, 
            image_source=image_source,
            max_concurrent=max_concurrent
        )
        
        self.logger.info(
            f"Workflow {pattern.name} completed in {result.execution_time:.2f}s "
            f"using {result.pattern.value} pattern"
        )
        
        return result
    