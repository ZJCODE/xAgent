"""Workflow management for multi-agent coordination."""

import asyncio
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid
import time
from pydantic import BaseModel, Field

from ..core.agent import Agent
from ..utils.workflow_dsl import parse_dependencies_dsl, validate_dsl_syntax


# Auto mode structured output models
class AgentSpec(BaseModel):
    """Specification for an agent in auto mode."""
    name: str = Field(description="Agent name (must be unique)")
    system_prompt: str = Field(description="System prompt for the agent")


class AgentsList(BaseModel):
    """List of agent specifications for auto mode."""
    agents: List[AgentSpec] = Field(description="List of agents optimized for the task (number determined by task complexity)")
    reasoning: str = Field(description="Explanation of why this number of agents was chosen")


class AgentDependency(BaseModel):
    """Single agent dependency specification."""
    agent_name: str = Field(description="Name of the agent")
    depends_on: List[str] = Field(description="List of agent names this agent depends on")


class DependenciesSpec(BaseModel):
    """Dependencies specification for auto mode with nested BaseModel structure."""
    agent_dependencies: List[AgentDependency] = Field(
        description="List of agent dependencies. Each entry specifies an agent and what it depends on."
    )
    explanation: str = Field(description="Brief explanation of the workflow structure and dependency reasoning")
    
    def to_dict(self) -> Dict[str, List[str]]:
        """Convert to dictionary format for workflow execution."""
        return {dep.agent_name: dep.depends_on for dep in self.agent_dependencies if dep.depends_on}


class WorkflowPatternType(Enum):
    """Types of workflow orchestration patterns."""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    GRAPH = "graph"


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
    async def execute(self, user_id: str, task: str, image_source: Optional[Union[str, List[str]]] = None, **kwargs) -> WorkflowResult:
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
    Sequential Pipeline Pattern: Agent A -> Agent B -> Agent C -> Result
    
    Pure sequential processing where each agent's output becomes the next agent's input.
    This is the fundamental nature of pipeline processing - there's no meaningful scenario
    where you wouldn't want context passing in a sequential workflow.
    
    Use cases:
    - Multi-step task decomposition (research -> analysis -> summary)
    - Progressive refinement (draft -> review -> polish)
    - Chain of reasoning (premise -> logic -> conclusion)
    """
    
    async def execute(
        self, 
        user_id: str,
        task: str, 
        image_source: Optional[Union[str, List[str]]] = None,
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
        name: Optional[str] = None,
        output_type: type[BaseModel] | None = None,
    ):
        """
        Initialize parallel pattern for broadcast consensus building.
        
        Args:
            agents: Agents that perform the actual work
            name: Optional name for the pattern
            output_type: Optional output type for the consensus validator (default is None, which uses Agent's default output type)
        """
        
        self.consensus_validator = Agent(
            name="consensus_validator",
            system_prompt="Consensus validator and synthesizer agent for parallel processing",
            output_type=output_type
        )
        
        all_agents = agents + [self.consensus_validator]
        super().__init__(all_agents, name)
        self.agents = agents
    
    async def execute(
        self,
        user_id: str,
        task: str,
        image_source: Optional[Union[str, List[str]]] = None,
        max_concurrent: int = 10,
    ) -> WorkflowResult:
        """
        Execute broadcast pattern.
        
        Args:
            task: Task string to be processed by all agents
            image_source: Optional image source (URL, file path, base64 string, or list of these)
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
        
        # Check for perfect consensus - handle both string and structured results
        try:
            # For string results, we can use set comparison
            if all(isinstance(result, str) for result in results):
                if len(set(results)) == 1:
                    return results[0]
            else:
                # For structured results (Pydantic models), compare by converting to dict
                result_dicts = []
                for result in results:
                    if hasattr(result, 'model_dump'):
                        result_dicts.append(result.model_dump())
                    elif hasattr(result, 'dict'):
                        result_dicts.append(result.dict())
                    else:
                        result_dicts.append(str(result))
                
                # Check if all structured results are identical
                if len(result_dicts) > 1 and all(rd == result_dicts[0] for rd in result_dicts[1:]):
                    return results[0]
        except (TypeError, AttributeError):
            # Fallback: if comparison fails, proceed to aggregation
            pass
        
        # Enhanced aggregation with both consensus and synthesis capabilities
        perspective_results = "\n\n----------\n\n".join([
            f"Agent {name}'s perspective:\n\n{self._format_result_for_display(result)}" 
            for name, result in worker_results
        ])
        
        prompt = f"""
You are acting as both a validator and synthesizer. Multiple agents independently worked on the same task.
Your role is to analyze their results and provide the best possible response through either consensus building or synthesis.

Original task: {original_task}

----------

Agent Results (Detailed Perspectives) :

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

    def _format_result_for_display(self, result) -> str:
        """Format result for display, handling both string and structured outputs."""
        if isinstance(result, str):
            return result
        elif hasattr(result, 'model_dump_json'):
            # Pydantic v2 models
            return result.model_dump_json(indent=2)
        elif hasattr(result, 'json'):
            # Pydantic v1 models
            return result.json(indent=2)
        elif hasattr(result, 'model_dump'):
            # Pydantic v2 models without json serialization
            import json
            return json.dumps(result.model_dump(), indent=2)
        elif hasattr(result, 'dict'):
            # Pydantic v1 models without json serialization
            import json
            return json.dumps(result.dict(), indent=2)
        else:
            # Fallback to string representation
            return str(result)


class GraphWorkflow(BaseWorkflow):
    """
    Graph-based workflow that supports complex dependency patterns with parallel execution.
    
    Features:
    - Automatic detection of parallelizable nodes
    - Topological sorting for execution order
    - Parallel execution of independent nodes
    - Support for complex patterns like A->B, A&B->C, fan-out/fan-in, etc.
    """
    
    def __init__(
        self, 
        agents: List[Agent], 
        dependencies: Union[Dict[str, List[str]], str], 
        name: Optional[str] = None,
        max_concurrent: int = 10
    ):
        """
        Initialize graph workflow.
        
        Args:
            agents: List of agents with their names as identifiers
            dependencies: Either:
                - Dict mapping agent names to their input dependencies
                  Example: {"B": ["A"], "C": ["A", "B"], "D": ["A"]}
                - DSL string with arrow notation
                  Example: "A->B, A->C, B&C->D"
            max_concurrent: Maximum concurrent executions
        """
        super().__init__(agents, name)
        self.agent_map = {agent.name: agent for agent in agents}
        
        # Parse dependencies based on type
        if isinstance(dependencies, str):
            # Validate DSL syntax first
            is_valid, error_msg = validate_dsl_syntax(dependencies)
            if not is_valid:
                raise ValueError(f"Invalid DSL syntax: {error_msg}")
            
            self.dependencies = parse_dependencies_dsl(dependencies)
            self.dsl_string = dependencies
        else:
            self.dependencies = dependencies
            self.dsl_string = None
            
        self.max_concurrent = max_concurrent
        
        # Validate dependencies
        self._validate_dependencies()
    
    def _validate_dependencies(self):
        """Validate that all dependencies refer to existing agents."""
        for agent_name, deps in self.dependencies.items():
            if agent_name not in self.agent_map:
                raise ValueError(f"Agent '{agent_name}' in dependencies not found in agents list")
            for dep in deps:
                if dep not in self.agent_map:
                    raise ValueError(f"Dependency '{dep}' for agent '{agent_name}' not found in agents list")
    
    async def execute(
        self, 
        user_id: str, 
        task: str, 
        image_source: Optional[Union[str, List[str]]] = None,
        **kwargs
    ) -> WorkflowResult:
        """
        Execute the graph workflow with parallel optimization.
        
        Args:
            task: Initial task string
            image_source: Optional image source(s) for the first agents
            
        Returns:
            WorkflowResult with final output and execution metadata
        """
        start_time = time.time()
        self._validate_agents()
        
        # Get execution layers (groups of agents that can run in parallel)
        execution_layers = self._get_execution_layers()
        
        results = {}
        layer_results = []
        
        # Execute each layer
        for layer_idx, layer_agents in enumerate(execution_layers):
            self.logger.info(f"Executing layer {layer_idx + 1}/{len(execution_layers)} with {len(layer_agents)} agents")
            
            # Execute agents in current layer in parallel
            layer_tasks = []
            for agent_name in layer_agents:
                agent = self.agent_map[agent_name]
                input_text = self._prepare_agent_input(agent_name, task, results)
                
                # Only pass image_source to first layer agents with no dependencies
                agent_image_source = image_source if not self.dependencies.get(agent_name, []) else None
                
                layer_tasks.append(
                    agent.chat(
                        user_message=input_text,
                        user_id=user_id,
                        session_id=str(uuid.uuid4()),
                        image_source=agent_image_source
                    )
                )
            
            # Execute layer in parallel with concurrency control
            semaphore = asyncio.Semaphore(self.max_concurrent)
            
            async def execute_with_semaphore(task_coro, agent_name):
                async with semaphore:
                    try:
                        return agent_name, await task_coro
                    except Exception as e:
                        self.logger.error(f"Agent {agent_name} failed: {e}")
                        return agent_name, f"Error: {e}"
            
            layer_task_results = await asyncio.gather(*[
                execute_with_semaphore(task_coro, agent_name) 
                for task_coro, agent_name in zip(layer_tasks, layer_agents)
            ])
            
            # Store layer results
            layer_result = {}
            for agent_name, result in layer_task_results:
                results[agent_name] = result
                layer_result[agent_name] = result
            
            layer_results.append({
                "layer": layer_idx + 1,
                "agents": layer_agents,
                "results": layer_result
            })
            
            self.logger.info(f"Layer {layer_idx + 1} completed")
        
        execution_time = time.time() - start_time
        
        # Find final agents (those with no dependents)
        final_agents = self._get_final_agents()
        
        # If single final agent, return its result; otherwise combine results
        if len(final_agents) == 1:
            final_result = results[final_agents[0]]
        else:
            final_result = {agent: results[agent] for agent in final_agents}
        
        metadata = {
            "execution_layers": execution_layers,
            "layer_results": layer_results,
            "dependencies": self.dependencies,
            "all_results": results,
            "final_agents": final_agents,
            "total_agents": len(self.agents),
            "total_layers": len(execution_layers)
        }
        
        return WorkflowResult(
            result=final_result,
            execution_time=execution_time,
            pattern=WorkflowPatternType.GRAPH,
            metadata=metadata
        )
    
    def _get_execution_layers(self) -> List[List[str]]:
        """
        Group agents into layers where each layer can be executed in parallel.
        Uses topological sorting with level-based grouping.
        """
        # Calculate in-degrees
        in_degree = {agent: 0 for agent in self.agent_map.keys()}
        for agent, deps in self.dependencies.items():
            in_degree[agent] = len(deps)
        
        # Initialize queue with agents that have no dependencies
        queue = [agent for agent, degree in in_degree.items() if degree == 0]
        layers = []
        
        while queue:
            # Current layer: all agents with no remaining dependencies
            current_layer = queue[:]
            layers.append(current_layer)
            queue = []
            
            # Process current layer and update in-degrees
            for agent in current_layer:
                # Find all agents that depend on current agent
                for dependent, deps in self.dependencies.items():
                    if agent in deps:
                        in_degree[dependent] -= 1
                        if in_degree[dependent] == 0:
                            queue.append(dependent)
        
        return layers
    
    def _prepare_agent_input(self, agent_name: str, original_task: str, results: Dict[str, str]) -> str:
        """Prepare input text for an agent based on its dependencies."""
        deps = self.dependencies.get(agent_name, [])
        
        if not deps:
            # No dependencies, use original task
            return original_task
        elif len(deps) == 1:
            # Single dependency, use its result
            return results[deps[0]]
        else:
            # Multiple dependencies, combine them
            dep_results = []
            for dep in deps:
                dep_results.append(f"Result from {dep}:\n{results[dep]}")
            
            combined_input = f"Original task: {original_task}\n\n" + "\n\n".join(dep_results)
            return combined_input
    
    def _get_final_agents(self) -> List[str]:
        """Get agents that have no dependents (final output agents)."""
        all_deps = set()
        for deps in self.dependencies.values():
            all_deps.update(deps)
        
        final_agents = []
        for agent_name in self.agent_map.keys():
            if agent_name not in all_deps:
                final_agents.append(agent_name)
        
        return final_agents


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
        image_source: Optional[Union[str, List[str]]] = None,
        intermediate_results: Optional[bool] = False,
        user_id: Optional[str] = "default_user"
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
            user_id=user_id,
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
        image_source: Optional[Union[str, List[str]]] = None,
        max_concurrent: Optional[int] = 10,
        output_type: type[BaseModel] | None = None,
        user_id: Optional[str] = "default_user"
    ) -> WorkflowResult:
        """
        Directly execute parallel processing in one call.
        
        Args:
            agents: Multiple agents for redundant processing
            task: Task string to be processed by all agents
            image_source: Optional image source (URL, file path, or base64 string)
            max_concurrent: Maximum concurrent worker executions
            output_type: Expected output type for the workflow
            user_id: User identifier
        Returns:
            WorkflowResult with consensus or best validated output
        """
        pattern = ParallelWorkflow(agents=agents, name=f"{self.name}_parallel", output_type=output_type)
        result = await pattern.execute(
            user_id=user_id,
            task=task, 
            image_source=image_source,
            max_concurrent=max_concurrent
        )

        self.logger.info(
            f"Workflow {pattern.name} completed in {result.execution_time:.2f}s "
            f"using {result.pattern.value} pattern"
        )
        
        return result
    
    async def run_graph(
        self,
        agents: List[Agent],
        dependencies: Union[Dict[str, List[str]], str],
        task: str,
        image_source: Optional[Union[str, List[str]]] = None,
        max_concurrent: Optional[int] = 10,
        user_id: Optional[str] = "default_user"
    ) -> WorkflowResult:
        """
        Execute a graph-based workflow with automatic parallel optimization.
        
        Args:
            agents: List of agents to be used in the workflow
            dependencies: Either:
                - Dict mapping agent names to their dependencies
                  Example: {"B": ["A"], "C": ["A", "B"], "D": ["A"]}
                - DSL string with arrow notation
                  Example: "A->B, A->C, B&C->D"
            task: Original task string
            image_source: Optional image source for root agents
            max_concurrent: Maximum concurrent executions
            user_id: User identifier
            
        Returns:
            WorkflowResult with final output and execution metadata
            
        Examples:
            # Dictionary format
            dependencies = {
                "B": ["A"],      # B depends on A
                "C": ["A", "B"], # C depends on both A and B  
                "D": ["A"]       # D depends on A (can run parallel with B)
            }
            
            # DSL format (equivalent to above)
            dependencies = "A->B, A&B->C, A->D"
            
            result = await workflow.run_graph(
                agents=[agent_A, agent_B, agent_C, agent_D],
                dependencies=dependencies,
                task="Original task"
            )
        """
        pattern = GraphWorkflow(
            agents=agents, 
            dependencies=dependencies,
            name=f"{self.name}_graph",
            max_concurrent=max_concurrent
        )
        
        result = await pattern.execute(
            user_id=user_id,
            task=task,
            image_source=image_source
        )
        
        self.logger.info(
            f"Graph workflow {pattern.name} completed in {result.execution_time:.2f}s "
            f"with {result.metadata['total_layers']} execution layers"
        )
        
        return result
    

    async def run_hybrid(
        self,
        task: str,
        stages: List[Dict[str, Any]],
        user_id: Optional[str] = "default_user"
    ) -> Dict[str, Any]:
        """
        Execute a hybrid workflow with multiple stages combining sequential, parallel, and graph patterns.
        
        Args:
            stages: List of stage configurations, each containing:
                - pattern: "sequential", "parallel", or "graph"
                - agents: List of agents for this stage
                - task: Task string (can include placeholders like {previous_result} and {original_task})
                - name: Optional stage name
                - dependencies: Required for graph pattern - either dict mapping agent names to their dependencies
                  or DSL string like "A->B, A->C, B&C->D"
                - kwargs: Additional arguments for the pattern
            task: The original task that will replace {original_task} placeholders
            user_id: User identifier for the workflow
            
        Returns:
            Dict containing all stage results and metadata
            
        Example:
            stages = [
                {
                    "pattern": "sequential",
                    "agents": [researcher, planner],
                    "task": "Research and plan: {original_task}",
                    "name": "research_phase"
                },
                {
                    "pattern": "parallel", 
                    "agents": [expert1, expert2, expert3],
                    "task": "Review this research: {previous_result}",
                    "name": "expert_review"
                },
                {
                    "pattern": "graph",
                    "agents": [analyzer, synthesizer, validator, writer],
                    "dependencies": "analyzer->synthesizer, analyzer->validator, synthesizer&validator->writer",
                    "task": "Create final report from: {previous_result}",
                    "name": "final_synthesis"
                }
            ]
        """
        if not stages:
            raise ValueError("At least one stage is required")
        
        results = {}
        previous_result = None
        total_time = 0.0
        
        for i, stage_config in enumerate(stages):
            stage_name = stage_config.get("name", f"stage_{i+1}")
            pattern = stage_config.get("pattern", "sequential")
            agents = stage_config.get("agents", [])
            task_template = stage_config.get("task", "")
            stage_kwargs = stage_config.get("kwargs", {})
            
            self.logger.info(f"Executing hybrid stage {i+1}/{len(stages)}: {stage_name} ({pattern})")
            
            # Prepare task string with variable substitution
            mid_stage_task = task_template.format(
                previous_result=previous_result or "",
                original_task=task,
                **stage_kwargs
            )
            
            # Execute the appropriate pattern
            if pattern == "sequential":
                result = await self.run_sequential(
                    agents=agents,
                    task=mid_stage_task,
                    user_id=user_id,
                    **{k: v for k, v in stage_kwargs.items() if k not in ['previous_result', 'original_task']}
                )
            elif pattern == "parallel":
                result = await self.run_parallel(
                    agents=agents,
                    task=mid_stage_task,
                    user_id=user_id,
                    **{k: v for k, v in stage_kwargs.items() if k not in ['previous_result', 'original_task']}
                )
            elif pattern == "graph":
                dependencies = stage_config.get("dependencies", {})
                result = await self.run_graph(
                    agents=agents,
                    dependencies=dependencies,
                    task=mid_stage_task,
                    user_id=user_id,
                    **{k: v for k, v in stage_kwargs.items() if k not in ['previous_result', 'original_task', 'dependencies']}
                )
            else:
                raise ValueError(f"Unsupported pattern: {pattern}. Supported patterns: sequential, parallel, graph")
            
            # Store stage result
            results[stage_name] = {
                "pattern": pattern,
                "result": result.result,
                "execution_time": result.execution_time,
                "metadata": result.metadata
            }
            
            previous_result = result.result
            total_time += result.execution_time
            
            self.logger.info(f"Stage {stage_name} completed in {result.execution_time:.2f}s")
        
        # Compile final results
        hybrid_results = {
            "stages": results,
            "final_result": previous_result,
            "total_execution_time": total_time,
            "workflow_pattern": "hybrid",
            "stages_executed": len(stages),
            "stage_patterns": [stage.get("pattern", "sequential") for stage in stages]
        }
        
        self.logger.info(f"Hybrid workflow completed in {total_time:.2f}s with {len(stages)} stages")
        
        return hybrid_results

    async def run_auto(
        self,
        task: str,
        image_source: Optional[Union[str, List[str]]] = None,
        user_id: Optional[str] = "default_user"
    ) -> WorkflowResult:
        """
        Execute an automatic workflow that dynamically generates optimal agents and dependencies.
        
        This method:
        1. Uses ParallelWorkflow (3 parallel designers) to generate optimal number of agents (2-6) for the task
        2. Uses ParallelWorkflow (3 parallel designers) to generate optimal dependencies dictionary
        3. Executes the task using GraphWorkflow with generated agents and dependencies
        
        The number of agents is dynamically determined based on task complexity:
        - Simple tasks: 2-3 agents
        - Moderate complexity: 3-4 agents
        - Complex multi-domain tasks: 4-6 agents
        
        Args:
            task: The task to be executed
            image_source: Optional image source (URL, file path, or base64 string)
            user_id: User identifier
            
        Returns:
            WorkflowResult with final output and execution metadata including:
            - generated_agents: List of created agents with their specifications
            - agent_count: Number of agents created
            - agent_selection_reasoning: Explanation for chosen number of agents
            - generated_dependencies: The dependency dictionary used
            - dependencies_explanation: Explanation of the workflow structure
        """
        start_time = time.time()
        self.logger.info(f"Starting auto workflow for task: {task[:100]}...")
        
        # Step 1: Generate optimal agents using parallel workflow
        agent_generators = [
            Agent(
                name="agent_designer_1",
                system_prompt="""You are an expert AI agent designer. Analyze the given task and design the optimal number of agents with distinct roles and capabilities. 
Consider task complexity to determine if 2-6 agents are needed. Focus on task decomposition, specialized expertise, and complementary skills. 
Each agent should have a unique name and specific system prompt. Create agents that can work together effectively.
Provide reasoning for why this specific number of agents was chosen.""",
                output_type=AgentsList
            ),
            Agent(
                name="agent_designer_2", 
                system_prompt="""You are a workflow optimization specialist. Analyze the task complexity and create the right number of specialized agents (2-6) with optimal role distribution.
Focus on efficiency, coverage, and synergy between agents. Design clear responsibilities and expertise areas for each agent.
Ensure the agents can handle different aspects of the task comprehensively without redundancy.
Explain your reasoning for the chosen number of agents.""",
                output_type=AgentsList
            ),
            Agent(
                name="agent_designer_3",
                system_prompt="""You are a multi-agent system architect. Design the optimal number of agents (2-6) with perfect role separation and task alignment.
Focus on scalability, robustness, and task-specific optimization. Each agent should have distinct capabilities and clear boundaries.
Create agents that maximize task completion quality and efficiency while avoiding unnecessary complexity.
Justify why this specific number of agents is ideal for the task.""",
                output_type=AgentsList
            )
        ]
        
        agent_design_prompt = f"""
Task to analyze: {task}

Design the optimal number of agents (between 2-6) for this specific task. Consider:
- Task complexity and scope
- Required expertise domains
- Natural task decomposition boundaries
- Efficiency vs thoroughness trade-offs
- Coordination overhead
- Parallel execution opportunities

For simple tasks: 2-3 agents may suffice
For moderate complexity: 3-4 agents for good coverage
For complex multi-domain tasks: 4-6 agents for comprehensive handling

Return the optimal number of agents with unique names, specific system prompts, and clear reasoning for your choice.
"""
        
        agents_result = await self.run_parallel(
            agents=agent_generators,
            task=agent_design_prompt,
            image_source=image_source,
            output_type=AgentsList,
            user_id=user_id
        )
        
        # Extract the best agent design from parallel results
        best_agents_spec = agents_result.result
        self.logger.info(f"Generated {len(best_agents_spec.agents)} optimal agents")
        self.logger.info(f"Agent selection reasoning: {best_agents_spec.reasoning}")
        
        # Step 2: Generate optimal dependencies using parallel workflow
        dependencies_generators = [
            Agent(
                name="dependencies_designer_1",
                system_prompt="""You are a workflow dependencies expert. Analyze the task and agent capabilities to create optimal dependency patterns.
Focus on efficiency, parallelization opportunities, and logical flow. Return a list of agent dependencies with each entry specifying agent name and what it depends on.
Consider which agents can work in parallel and which need sequential dependencies. Use exact agent names from the provided list.""",
                output_type=DependenciesSpec
            ),
            Agent(
                name="dependencies_designer_2",
                system_prompt="""You are a dependency optimization specialist. Create dependency patterns that maximize parallel execution while maintaining logical order.
Focus on minimizing bottlenecks, optimizing throughput, and ensuring proper information flow between agents.
Design efficient execution patterns with clear dependencies. Return structured dependency specifications.""",
                output_type=DependenciesSpec
            ),
            Agent(
                name="dependencies_designer_3",
                system_prompt="""You are a workflow efficiency expert. Design dependency patterns that balance parallelism with dependency requirements.
Focus on optimal resource utilization, execution time minimization, and logical task flow.
Create patterns that ensure quality while maximizing efficiency. Return structured dependency list format.""",
                output_type=DependenciesSpec
            )
        ]
        
        # Prepare agent information for dependencies generation
        agent_info = "\n".join([
            f"Agent: {spec.name}\nRole: {spec.system_prompt[:200]}..."
            for spec in best_agents_spec.agents
        ])
        
        dependencies_design_prompt = f"""
Task: {task}

Available Agents:
{agent_info}

Create an optimal dependency pattern for these agents to execute the task efficiently.
Consider:
- Which agents can work in parallel
- Which agents need outputs from others
- Optimal execution flow
- Minimizing bottlenecks
- Maximizing parallelization

Return a list of agent dependencies where each entry specifies:
- agent_name: the name of the agent
- depends_on: list of agent names this agent depends on (empty list if no dependencies)

Example format:
[
  {{"agent_name": "B", "depends_on": ["A"]}},
  {{"agent_name": "C", "depends_on": ["A", "B"]}},
  {{"agent_name": "D", "depends_on": ["A"]}}
]

Use exact agent names from the list above. Include all agents in the list.
Return the dependencies specification and brief explanation.
"""
        
        dependencies_result = await self.run_parallel(
            agents=dependencies_generators,
            task=dependencies_design_prompt,
            output_type=DependenciesSpec,
            user_id=user_id
        )
        
        best_dependencies_spec = dependencies_result.result
        self.logger.info(f"Generated dependencies pattern: {best_dependencies_spec.to_dict()}")
        
        # Step 3: Create actual agents from specifications
        actual_agents = []
        for agent_spec in best_agents_spec.agents:
            actual_agent = Agent(
                name=agent_spec.name,
                system_prompt=agent_spec.system_prompt
            )
            actual_agents.append(actual_agent)
        
        # Step 4: Execute the task using GraphWorkflow
        final_result = await self.run_graph(
            agents=actual_agents,
            dependencies=best_dependencies_spec.to_dict(),
            task=task,
            image_source=image_source,
            user_id=user_id
        )
        
        # Update metadata with auto workflow information
        auto_metadata = {
            **final_result.metadata,
            "auto_workflow": True,
            "generated_agents": [{"name": spec.name, "system_prompt": spec.system_prompt} for spec in best_agents_spec.agents],
            "agent_count": len(best_agents_spec.agents),
            "agent_selection_reasoning": best_agents_spec.reasoning,
            "generated_dependencies": best_dependencies_spec.to_dict(),
            "dependencies_explanation": best_dependencies_spec.explanation,
            "agent_generation_time": agents_result.execution_time,
            "dependencies_generation_time": dependencies_result.execution_time,
            "total_auto_time": time.time() - start_time
        }
        
        final_result.metadata = auto_metadata
        
        self.logger.info(
            f"Auto workflow completed in {time.time() - start_time:.2f}s "
            f"(agent gen: {agents_result.execution_time:.2f}s, "
            f"dependencies gen: {dependencies_result.execution_time:.2f}s, "
            f"execution: {final_result.execution_time:.2f}s)"
        )
        
        return final_result