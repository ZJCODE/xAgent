"""Workflow management for multi-agent coordination."""

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid
import time

from ..core.agent import Agent


def parse_dependencies_dsl(dsl_string: str) -> Dict[str, List[str]]:
    """
    Parse DSL string to dependency dictionary.
    
    Supported syntax:
    - A->B: A depends on nothing, B depends on A
    - A->B->C: A->B, B->C (sequential chain)
    - A->B, A->C: A->B and A->C (parallel branches)
    - A&B->C: C depends on both A and B
    - Complex: A->B, A->C, B&C->D
    
    Args:
        dsl_string: DSL string like "A->B, A->C, B&C->D"
        
    Returns:
        Dict mapping agent names to their dependencies
        
    Examples:
        "A->B" -> {"B": ["A"]}
        "A->B->C" -> {"B": ["A"], "C": ["B"]}
        "A->B, A->C" -> {"B": ["A"], "C": ["A"]}
        "A->B, B&C->D" -> {"B": ["A"], "D": ["B", "C"]}
    """
    if not dsl_string or not dsl_string.strip():
        return {}
    
    dependencies = {}
    
    # Normalize arrows: convert -> to → for consistent processing (internally)
    normalized_dsl = dsl_string.replace('->', '→')
    
    # Split by comma to get individual rules
    rules = [rule.strip() for rule in normalized_dsl.split(',')]
    
    for rule in rules:
        if not rule:
            continue
        
        # Handle chain syntax (A->B->C becomes A->B, B->C)
        if rule.count('→') > 1:
            # Split into chain segments
            segments = [seg.strip() for seg in rule.split('→')]
            # Create pairs: A->B->C becomes [(A,B), (B,C)]
            for i in range(len(segments) - 1):
                left_part = segments[i]
                right_part = segments[i + 1]
                
                if not right_part:
                    continue
                
                # Parse left part (dependencies) - handle & for multiple dependencies
                if '&' in left_part:
                    deps = [dep.strip() for dep in left_part.split('&')]
                else:
                    deps = [left_part.strip()] if left_part else []
                
                target = right_part.strip()
                
                if target:
                    if target in dependencies:
                        # Merge dependencies if target already exists
                        existing_deps = set(dependencies[target])
                        new_deps = set(deps)
                        dependencies[target] = list(existing_deps.union(new_deps))
                    else:
                        dependencies[target] = deps
        else:
            # Single arrow rule
            if '→' in rule:
                left_part, right_part = rule.split('→', 1)
                left_part = left_part.strip()
                right_part = right_part.strip()
                
                # Parse left part (dependencies) - handle & for multiple dependencies
                if '&' in left_part:
                    deps = [dep.strip() for dep in left_part.split('&')]
                else:
                    deps = [left_part.strip()] if left_part else []
                
                # Parse right part (target) - currently only support single target
                target = right_part.strip()
                
                if target:
                    if target in dependencies:
                        # Merge dependencies if target already exists
                        existing_deps = set(dependencies[target])
                        new_deps = set(deps)
                        dependencies[target] = list(existing_deps.union(new_deps))
                    else:
                        dependencies[target] = deps
    
    return dependencies


def validate_dsl_syntax(dsl_string: str) -> Tuple[bool, str]:
    """
    Validate DSL syntax.
    
    Args:
        dsl_string: DSL string to validate (supports both → and -> arrows)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not dsl_string or not dsl_string.strip():
        return True, ""
    
    try:
        # Check for valid characters and patterns
        # First check for invalid arrow patterns like --, ->, ->>, etc.
        if '--' in dsl_string or '->>' in dsl_string or '<<-' in dsl_string:
            return False, "Invalid arrow patterns detected. Use → or -> (single dash followed by >)."
        
        # Check for valid characters (letters, numbers, underscore, arrows, ampersand, comma, space, hyphen, >)
        valid_pattern = re.compile(r'^[a-zA-Z0-9_→&,\s\-\>]+$')
        if not valid_pattern.match(dsl_string):
            return False, "Invalid characters in DSL string. Only letters, numbers, underscore, →, ->, &, comma, and spaces are allowed."
        
        # Normalize arrows for consistent processing
        normalized_dsl = dsl_string.replace('->', '→')
        
        # Split by comma to get individual rules
        rules = [rule.strip() for rule in normalized_dsl.split(',')]
        for rule in rules:
            if not rule:
                continue
            if '→' not in rule:
                return False, f"Each rule must contain at least one arrow (→ or ->). Invalid rule: '{rule}'"
            
            # Handle chain syntax (multiple arrows)
            segments = [seg.strip() for seg in rule.split('→')]
            
            # Check that we don't have empty segments
            for i, segment in enumerate(segments):
                if not segment and i != 0:  # Allow empty first segment (e.g., "→B")
                    return False, f"Empty segment in rule: '{rule}'"
                
                # Check for valid agent names (no empty names after splitting by &)
                if segment and '&' in segment:
                    deps = [dep.strip() for dep in segment.split('&')]
                    for dep in deps:
                        if not dep:
                            return False, f"Empty dependency name in rule: '{rule}'"
        
        # Try to parse to ensure it's valid
        parse_dependencies_dsl(dsl_string)
        return True, ""
        
    except Exception as e:
        return False, f"DSL parsing error: {str(e)}"

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
        
        self.consensus_validator = Agent(
            name="consensus_validator",
            system_prompt="Consensus validator and synthesizer agent for parallel processing"
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
                - DSL string with arrow notation (supports both → and ->)
                  Example: "A→B, A→C, B&C→D" or "A->B, A->C, B&C->D"
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
        image_source: Optional[str] = None,
        **kwargs
    ) -> WorkflowResult:
        """
        Execute the graph workflow with parallel optimization.
        
        Args:
            task: Initial task string
            image_source: Optional image source for the first agents
            
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
        image_source: Optional[str] = None,
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
        image_source: Optional[str] = None,
        max_concurrent: Optional[int] = 10,
        user_id: Optional[str] = "default_user"
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
        image_source: Optional[str] = None,
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
                - DSL string with arrow notation (supports both → and ->)
                  Example: "A→B, A→C, B&C→D" or "A->B, A->C, B&C->D"
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
            
            # DSL format with Unicode arrow (equivalent to above)
            dependencies = "A→B, A&B→C, A→D"
            
            # DSL format with ASCII arrow (equivalent to above)
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
                  or DSL string like "A→B, A→C, B&C→D"
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
                    "agents": [analyzer, synthesizer, validator],
                    "dependencies": "analyzer→synthesizer, analyzer→validator, synthesizer&validator→final",
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