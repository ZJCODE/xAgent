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


class ParallelPatternType(Enum):
    """Types of parallel execution patterns based on first principles."""
    DATA_PARALLEL = "data_parallel"      # Same operation, different data subsets
    TASK_PARALLEL = "task_parallel"      # Different operations, same data  
    BROADCAST = "broadcast"              # Same operation, same data (redundancy)


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


class ParallelPatternType(Enum):
    """Types of parallel execution patterns based on first principles."""
    DATA_PARALLEL = "data_parallel"      # Same operation, different data subsets
    TASK_PARALLEL = "task_parallel"      # Different operations, same data  
    BROADCAST = "broadcast"              # Same operation, same data (redundancy)


class ParallelPattern(BaseWorkflowPattern):
    """
    Redesigned Parallel Pattern based on fundamental parallelism principles.
    
    Three core patterns aligned with computational theory:
    
    1. DATA_PARALLEL: Divide data/content, same processing logic
       - Use case: Process multiple documents, analyze large datasets
       - Example: 10 agents each analyze 100 news articles
    
    2. TASK_PARALLEL: Same input, different specialized processing
       - Use case: Multi-perspective analysis, expert specialization
       - Example: Legal, financial, technical review of same contract
    
    3. BROADCAST: Same input, same processing (redundancy for reliability)
       - Use case: Critical decisions, consensus building, error reduction
       - Example: Multiple agents independently solve same problem for validation
    
    Key insight: The pattern type determines the aggregation strategy automatically.
    """
    
    def __init__(
        self, 
        worker_agents: List[Agent], 
        pattern_type: ParallelPatternType,
        name: Optional[str] = None
    ):
        """
        Initialize parallel pattern.
        
        Args:
            worker_agents: Agents that perform the actual work
            pattern_type: Type of parallel execution pattern
            name: Optional name for the pattern
        """
        # Create internal coordinator agent based on pattern type
        coordinator_name = f"coordinator_{pattern_type.value}_{uuid.uuid4().hex[:8]}"
        coordinator_prompt = self._get_coordinator_system_prompt(pattern_type)
        
        self.coordinator_agent = Agent(
            name=coordinator_name,
            system_prompt=coordinator_prompt,
            description=f"Coordinator agent for {pattern_type.value} pattern"
        )
        
        all_agents = worker_agents + [self.coordinator_agent]
        super().__init__(all_agents, name)
        self.worker_agents = worker_agents
        self.pattern_type = pattern_type
    
    def _get_coordinator_system_prompt(self, pattern_type: ParallelPatternType) -> str:
        """
        Generate specialized system prompts for coordinator agents based on pattern type.
        
        Args:
            pattern_type: The type of parallel pattern
            
        Returns:
            str: Tailored system prompt for the coordinator agent
        """
        if pattern_type == ParallelPatternType.DATA_PARALLEL:
            return """
You are a Data Summarizer and Merger. Your role is to:
1. Combine results from multiple data processing workers
2. Identify patterns and insights across all data chunks
3. Create comprehensive summaries that capture key findings
4. Merge information without losing important details
5. Present unified conclusions from distributed data analysis

Focus on synthesis and pattern recognition across data subsets.
            """
        
        elif pattern_type == ParallelPatternType.TASK_PARALLEL:
            return """
You are a Multi-Perspective Synthesizer. Your role is to:
1. Integrate different expert perspectives on the same topic
2. Resolve conflicts between different viewpoints
3. Create well-rounded, comprehensive responses
4. Highlight complementary insights and trade-offs
5. Balance different approaches into cohesive conclusions

Focus on synthesis and integration of diverse expert opinions.
            """
        
        elif pattern_type == ParallelPatternType.BROADCAST:
            return """
You are a Consensus Builder and Validator. Your role is to:
1. Analyze multiple independent solutions to the same problem
2. Identify consensus among different responses
3. Validate the quality and accuracy of solutions
4. Select the best response when consensus isn't reached
5. Explain reasoning for final choices

Focus on validation, consensus building, and quality assessment.
            """
        
        else:
            return "You are a coordinator agent responsible for aggregating and synthesizing results from multiple worker agents."
    
    async def execute(
        self,
        task: Union[str, Message],
        data_chunks: Optional[List[str]] = None,  # For DATA_PARALLEL
        task_specifications: Optional[List[str]] = None,  # For TASK_PARALLEL  
        max_concurrent: int = 10
    ) -> WorkflowResult:
        """
        Execute parallel pattern based on the specified type.
        
        Args:
            task: Main task or data to be processed
            data_chunks: For DATA_PARALLEL - list of data subsets for each agent
            task_specifications: For TASK_PARALLEL - list of specialized tasks for each agent
            max_concurrent: Maximum concurrent worker executions
            
        Returns:
            WorkflowResult with appropriately aggregated output
        """
        start_time = time.time()
        self._validate_agents()
        
        # Determine worker inputs based on pattern type
        worker_inputs = await self._prepare_inputs(task, data_chunks, task_specifications)
        
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
        
        self.logger.info(f"Executing {self.pattern_type.value} pattern with {len(self.worker_agents)} workers")
        
        worker_tasks = [
            execute_worker(agent, input_task) 
            for agent, input_task in zip(self.worker_agents, worker_inputs)
        ]
        
        worker_results = await asyncio.gather(*worker_tasks)
        
        # Aggregate results using pattern-specific strategy
        final_result = await self._aggregate_by_pattern(worker_results, str(task))
        
        execution_time = time.time() - start_time
        
        metadata = {
            "worker_agents": [agent.name for agent in self.worker_agents],
            "coordinator_agent": self.coordinator_agent.name,
            "pattern_type": self.pattern_type.value,
            "worker_results": dict(worker_results)
        }
        
        return WorkflowResult(
            result=final_result,
            execution_time=execution_time,
            pattern=WorkflowPatternType.PARALLEL,
            metadata=metadata
        )
    
    async def _prepare_inputs(
        self, 
        task: Union[str, Message], 
        data_chunks: Optional[List[str]] = None,
        task_specifications: Optional[List[str]] = None
    ) -> List[str]:
        """
        Prepare inputs for workers based on pattern type.
        """
        task_str = str(task)
        
        if self.pattern_type == ParallelPatternType.DATA_PARALLEL:
            if not data_chunks:
                raise ValueError("DATA_PARALLEL requires data_chunks parameter")
            if len(data_chunks) != len(self.worker_agents):
                raise ValueError(f"Number of data chunks ({len(data_chunks)}) must match number of workers ({len(self.worker_agents)})")
            return [f"{task_str}\n\nData to process:\n{chunk}" for chunk in data_chunks]
        
        elif self.pattern_type == ParallelPatternType.TASK_PARALLEL:
            if not task_specifications:
                # Auto-generate task specifications based on agent names/roles
                task_specifications = await self._generate_task_specifications(task_str)
            if len(task_specifications) != len(self.worker_agents):
                raise ValueError(f"Number of task specifications ({len(task_specifications)}) must match number of workers ({len(self.worker_agents)})")
            return task_specifications
        
        elif self.pattern_type == ParallelPatternType.BROADCAST:
            # Same task for all workers
            return [task_str] * len(self.worker_agents)
        
        else:
            raise ValueError(f"Unknown pattern type: {self.pattern_type}")
    
    async def _generate_task_specifications(self, task: str) -> List[str]:
        """
        Auto-generate specialized task specifications for TASK_PARALLEL mode.
        """
        agent_names = [agent.name for agent in self.worker_agents]
        
        specification_prompt = f"""
        You need to create {len(self.worker_agents)} specialized task specifications for different agents to work on the same input in parallel.
        
        Original task: {task}
        
        Available agents: {', '.join(agent_names)}
        
        Create {len(self.worker_agents)} different specialized approaches/perspectives that:
        1. Each focus on a different aspect or angle of the task
        2. Are complementary and together provide comprehensive coverage
        3. Are specific enough that different agents can work independently
        
        Format as JSON array: ["specification 1", "specification 2", ...]
        
        Each specification should be a complete task description that includes the original context plus the specific focus area.
        """
        
        response = await self.coordinator_agent.chat(specification_prompt)
        
        try:
            specifications = json.loads(response)
            if isinstance(specifications, list) and len(specifications) == len(self.worker_agents):
                return specifications
        except json.JSONDecodeError:
            pass
        
        # Fallback: simple role-based specifications
        return [f"{task}\n\nPlease approach this from the perspective of: {agent.name}" 
                for agent in self.worker_agents]
    
    async def _aggregate_by_pattern(
        self, 
        worker_results: List[Tuple[str, str]], 
        original_task: str
    ) -> str:
        """
        Aggregate results using pattern-specific strategies.
        """
        results = [result for _, result in worker_results]
        
        if self.pattern_type == ParallelPatternType.DATA_PARALLEL:
            # Concatenate or merge data processing results
            return await self._aggregate_data_parallel_results(worker_results, original_task)
        
        elif self.pattern_type == ParallelPatternType.TASK_PARALLEL:
            # Synthesize different perspectives/aspects
            return await self._aggregate_task_parallel_results(worker_results, original_task)
        
        elif self.pattern_type == ParallelPatternType.BROADCAST:
            # Check for consensus or select best result
            return await self._aggregate_broadcast_results(worker_results, original_task)
        
        else:
            raise ValueError(f"Unknown pattern type: {self.pattern_type}")
    
    async def _aggregate_data_parallel_results(
        self, 
        worker_results: List[Tuple[str, str]], 
        original_task: str
    ) -> str:
        """Merge and summarize results from data parallel processing."""
        combined_results = "\n\n---\n\n".join([
            f"Results from {name}:\n{result}" 
            for name, result in worker_results
        ])
        
        merge_prompt = f"""
        You are acting as a summarizer. Combine the following data processing results into a comprehensive summary:
        
        Original task: {original_task}
        
        {combined_results}
        
        Provide a unified summary that incorporates insights from all data chunks.
        Focus on merging information and identifying patterns across all chunks.
        """
        
        return await self.coordinator_agent.chat(merge_prompt)
    
    async def _aggregate_task_parallel_results(
        self, 
        worker_results: List[Tuple[str, str]], 
        original_task: str
    ) -> str:
        """Synthesize and integrate results from task parallel processing (different perspectives)."""
        perspective_results = "\n\n---\n\n".join([
            f"Perspective from {name}:\n{result}" 
            for name, result in worker_results
        ])
        
        synthesis_prompt = f"""
        You are acting as a synthesizer. Integrate the following different perspectives on the same task:
        
        Original task: {original_task}
        
        {perspective_results}
        
        Provide a comprehensive response that:
        1. Integrates insights from all perspectives
        2. Resolves any conflicts between different viewpoints  
        3. Delivers a well-rounded, multi-faceted final answer
        4. Highlights complementary insights and trade-offs
        """
        
        return await self.coordinator_agent.chat(synthesis_prompt)
    
    async def _aggregate_broadcast_results(
        self, 
        worker_results: List[Tuple[str, str]], 
        original_task: str
    ) -> str:
        """Validate consensus and select best result from broadcast processing."""
        results = [result for _, result in worker_results]
        
        # Check for perfect consensus
        if len(set(results)) == 1:
            return results[0]
        
        # Build consensus or validate best result
        consensus_prompt = f"""
        You are acting as a validator. Multiple agents independently worked on the same task. 
        Analyze their results for consensus or select the best response:
        
        Original task: {original_task}
        
        Results:
        {chr(10).join([f"{i+1}. Agent {name}: {result}" for i, (name, result) in enumerate(worker_results)])}
        
        Your validation approach:
        1. If there's clear consensus among results, summarize the agreed-upon answer
        2. If results differ significantly, evaluate quality and select the superior response
        3. Explain your reasoning for the final choice
        4. Highlight any important minority opinions that should be considered
        """
        
        return await self.coordinator_agent.chat(consensus_prompt)


class Workflow:
    """
    Main workflow orchestrator that supports multiple orchestration patterns.
    Provides a unified interface for executing different workflow patterns.
    """
    
    def __init__(self, name: Optional[str] = None):
        self.name = name or f"workflow_{uuid.uuid4().hex[:8]}"
        self.logger = logging.getLogger("Workflow")
        self.execution_history: List[WorkflowResult] = []
    
    # Direct execution methods - simplified API
    async def run_sequential(
        self,
        agents: List[Agent],
        task: Union[str, Message],
        intermediate_results: bool = False
    ) -> WorkflowResult:
        """
        Directly execute a sequential pipeline in one call.
        
        Args:
            agents: List of agents to execute in sequence
            task: Initial task for the first agent
            intermediate_results: Whether to include intermediate results in metadata
            
        Returns:
            WorkflowResult with final output and execution metadata
        """
        pattern = SequentialPipeline(agents, f"{self.name}_sequential")
        result = await pattern.execute(task, intermediate_results=intermediate_results)
        self.execution_history.append(result)
        
        self.logger.info(
            f"Workflow {pattern.name} completed in {result.execution_time:.2f}s "
            f"using {result.pattern.value} pattern"
        )
        
        return result
    
    async def run_data_parallel(
        self,
        worker_agents: List[Agent],
        task: Union[str, Message],
        data_chunks: List[str],
        max_concurrent: int = 10
    ) -> WorkflowResult:
        """
        Directly execute data parallel processing in one call.
        
        Args:
            worker_agents: Agents that process different data chunks
            task: Main task description
            data_chunks: List of data subsets for each agent
            max_concurrent: Maximum concurrent worker executions
            
        Returns:
            WorkflowResult with aggregated output
        """
        pattern = ParallelPattern(worker_agents, ParallelPatternType.DATA_PARALLEL, f"{self.name}_data_parallel")
        result = await pattern.execute(task, data_chunks=data_chunks, max_concurrent=max_concurrent)
        self.execution_history.append(result)
        
        self.logger.info(
            f"Workflow {pattern.name} completed in {result.execution_time:.2f}s "
            f"using {result.pattern.value} pattern"
        )
        
        return result
    
    async def run_task_parallel(
        self,
        worker_agents: List[Agent],
        task: Union[str, Message],
        task_specifications: Optional[List[str]] = None,
        max_concurrent: int = 10
    ) -> WorkflowResult:
        """
        Directly execute task parallel processing in one call.
        
        Args:
            worker_agents: Specialized agents for different aspects
            task: Main task to be analyzed from different perspectives
            task_specifications: Optional specific tasks for each agent
            max_concurrent: Maximum concurrent worker executions
            
        Returns:
            WorkflowResult with synthesized output
        """
        pattern = ParallelPattern(worker_agents, ParallelPatternType.TASK_PARALLEL, f"{self.name}_task_parallel")
        result = await pattern.execute(task, task_specifications=task_specifications, max_concurrent=max_concurrent)
        self.execution_history.append(result)
        
        self.logger.info(
            f"Workflow {pattern.name} completed in {result.execution_time:.2f}s "
            f"using {result.pattern.value} pattern"
        )
        
        return result
    
    async def run_broadcast(
        self,
        worker_agents: List[Agent],
        task: Union[str, Message],
        max_concurrent: int = 10
    ) -> WorkflowResult:
        """
        Directly execute broadcast processing in one call.
        
        Args:
            worker_agents: Multiple agents for redundant processing
            task: Task to be processed by all agents
            max_concurrent: Maximum concurrent worker executions
            
        Returns:
            WorkflowResult with consensus or best validated output
        """
        pattern = ParallelPattern(worker_agents, ParallelPatternType.BROADCAST, f"{self.name}_broadcast")
        result = await pattern.execute(task, max_concurrent=max_concurrent)
        self.execution_history.append(result)
        
        self.logger.info(
            f"Workflow {pattern.name} completed in {result.execution_time:.2f}s "
            f"using {result.pattern.value} pattern"
        )
        
        return result
    
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