"""
Sequential Workflow Example

This example demonstrates how to use the Sequential Workflow pattern
where agents process tasks in a pipeline: Agent A → Agent B → Agent C → Result

Use cases:
- Multi-step task decomposition (research → analysis → summary)
- Progressive refinement (draft → review → polish)
- Chain of reasoning (premise → logic → conclusion)
"""

import asyncio
from xagent import Agent
from xagent.multi.workflow import Workflow
from xagent.tools import web_search


async def main():
    # Create specialized agents for different steps
    research_agent = Agent(
        name="Research Agent",
        system_prompt="Specializes in gathering and organizing information on given topics",
        tools=[web_search],  # Use web search tool for research
    )

    writer_agent = Agent(
        name="Writer Agent",
        system_prompt="Drafts and refines written content based on research findings"
    )

    editor_agent = Agent(
        name="Editor Agent",
        system_prompt="Reviews and edits written content for clarity, coherence, and style"
    )

    # Create workflow orchestrator
    workflow = Workflow(name="research_pipeline")
    
    # Define the task
    task = "Research the impact of artificial intelligence on the job market in 2025"
    
    print("🚀 Starting Sequential Workflow Example")
    print(f"Task: {task}")
    print("-" * 60)
    
    try:
        # Execute sequential workflow
        result = await workflow.run_sequential(
            agents=[research_agent, writer_agent, editor_agent],
            task=task,
            intermediate_results=True,  # Include intermediate results in metadata
            user_id="demo_user"
        )
        
        print(f"✅ Workflow completed successfully!")
        print(f"⏱️  Execution time: {result.execution_time:.2f} seconds")
        print(f"🔄 Pattern used: {result.pattern.value}")
        print(f"📊 Agents used: {', '.join(result.metadata['agents_used'])}")
        print(f"🔢 Steps completed: {result.metadata['steps_completed']}")
        
        print("\n" + "="*60)
        print("FINAL RESULT:")
        print("="*60)
        print(result.result)
        
        # Show intermediate results if available
        if 'intermediate_results' in result.metadata:
            print("\n" + "="*60)
            print("INTERMEDIATE RESULTS:")
            print("="*60)
            for i, intermediate in enumerate(result.metadata['intermediate_results']):
                print(f"\nStep {i+1} Result:")
                print("-" * 30)
                print(intermediate)
        
    except Exception as e:
        print(f"❌ Workflow failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
