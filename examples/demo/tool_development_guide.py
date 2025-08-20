"""
Tool Development Guide Examples

This file demonstrates different approaches to tool development,
including sync vs async patterns and best practices.
"""

import asyncio
import time
import httpx
from xagent.utils.tool_decorator import function_tool
from xagent.core import Agent

# Understanding Automatic Async Conversion Examples

# ✅ Sync function - automatically wrapped for thread-pool execution
@function_tool()
def cpu_heavy_task(n: int) -> int:
    """Calculate sum of squares (CPU-intensive)."""
    time.sleep(0.1)  # Simulate heavy computation
    return sum(i**2 for i in range(n))

# ✅ Async function - used directly on event loop  
@function_tool()
async def network_request(url: str) -> str:
    """Fetch data from URL (I/O-intensive)."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.text[:100]

# ✅ Simple sync function - no need to make it async
@function_tool()
def simple_math(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b  # No async needed for simple operations

# When to Use Sync vs Async Examples

# Use Sync Functions For:
@function_tool()
def mathematical_calculation(x: float, y: float) -> float:
    """Mathematical calculations - sync"""
    return x ** y + (x * y) / 2

@function_tool()
def data_transformation(data: list[int]) -> list[int]:
    """Data transformations - sync"""
    return [x * 2 for x in data if x > 0]

@function_tool()
def file_operation(filename: str, content: str) -> str:
    """File operations (small files) - sync"""
    with open(filename, 'w') as f:
        f.write(content)
    return f"File {filename} written successfully"

@function_tool()
def string_processing(text: str, operation: str) -> str:
    """Simple string/data processing - sync"""
    if operation == "upper":
        return text.upper()
    elif operation == "reverse":
        return text[::-1]
    return text

# Use Async Functions For:
@function_tool()
async def http_request(url: str, method: str = "GET") -> str:
    """HTTP requests - async"""
    async with httpx.AsyncClient() as client:
        if method.upper() == "GET":
            response = await client.get(url)
        else:
            response = await client.post(url)
        return response.text[:200]

@function_tool()
async def database_query(query: str) -> str:
    """Database queries - async (simulated)"""
    await asyncio.sleep(0.1)  # Simulate DB query
    return f"Query result for: {query}"

@function_tool()
async def large_file_io(filepath: str) -> str:
    """File I/O (large files) - async"""
    await asyncio.sleep(0.2)  # Simulate large file operation
    return f"Processed large file: {filepath}"

@function_tool()
async def external_api_call(api_endpoint: str, params: dict) -> str:
    """External API calls - async"""
    async with httpx.AsyncClient() as client:
        response = await client.get(api_endpoint, params=params)
        return f"API response: {response.status_code}"

# Advanced Tool Development Examples

# Tool with complex logic
@function_tool()
def complex_calculation(numbers: list[int], operation: str) -> dict:
    """Complex CPU-bound calculation with structured output."""
    time.sleep(0.05)  # Simulate computation time
    
    if operation == "statistics":
        return {
            "sum": sum(numbers),
            "average": sum(numbers) / len(numbers) if numbers else 0,
            "max": max(numbers) if numbers else None,
            "min": min(numbers) if numbers else None,
            "count": len(numbers)
        }
    elif operation == "squares":
        return {"squares": [x**2 for x in numbers]}
    else:
        return {"error": f"Unknown operation: {operation}"}

# Async tool with error handling
@function_tool()
async def resilient_api_call(url: str, retries: int = 3) -> str:
    """API call with retry logic."""
    async with httpx.AsyncClient() as client:
        for attempt in range(retries):
            try:
                response = await client.get(url, timeout=5.0)
                return f"Success on attempt {attempt + 1}: {response.status_code}"
            except Exception as e:
                if attempt == retries - 1:
                    return f"Failed after {retries} attempts: {str(e)}"
                await asyncio.sleep(0.1 * (attempt + 1))  # Exponential backoff

# Concurrent execution example
async def demo_concurrent_tools():
    """Performance Characteristics - Concurrent execution example"""
    agent = Agent(tools=[
        cpu_heavy_task,    # Runs in thread pool
        network_request,   # Runs on event loop  
        simple_math        # Runs in thread pool
    ])
    
    # All tools execute concurrently when called by agent
    # - sync tools don't block the event loop
    # - async tools run directly for optimal I/O performance
    # - total execution time = max(individual_times), not sum
    
    response = await agent.chat(
        user_message="Calculate sum of squares for 1000, fetch https://httpbin.org/json, and add 5+3",
        user_id="demo",
        session_id="demo_session"
    )
    return response

# Basic tool development example
async def basic_tool_development():
    """Adding New Tools - Basic example"""
    
    # Sync tool - perfect for CPU-bound operations
    @function_tool()
    def my_sync_tool(input_text: str) -> str:
        """Process text synchronously (runs in thread pool)."""
        # Simulate CPU-intensive work
        time.sleep(0.1)
        return f"Sync processed: {input_text}"

    # Async tool - ideal for I/O-bound operations  
    @function_tool()
    async def my_async_tool(input_text: str) -> str:
        """Process text asynchronously."""
        # Simulate async I/O operation
        await asyncio.sleep(0.1)
        return f"Async processed: {input_text}"

    # Use with agent
    agent = Agent(tools=[my_sync_tool, my_async_tool])
    
    response = await agent.chat(
        user_message="Use both tools to process 'hello world'",
        user_id="user123",
        session_id="session456"
    )
    return response

async def main():
    """Run all tool development examples"""
    print("1. Concurrent tools demo:")
    result1 = await demo_concurrent_tools()
    print(result1)
    
    print("\n2. Basic tool development:")
    result2 = await basic_tool_development()
    print(result2)
    
    print("\n3. Testing individual tools:")
    
    # Test sync tools
    print("Mathematical calculation:", mathematical_calculation(5.0, 2.0))
    print("Data transformation:", data_transformation([1, -2, 3, -4, 5]))
    print("String processing:", string_processing("Hello World", "upper"))
    
    # Test async tools
    print("Database query:", await database_query("SELECT * FROM users"))
    print("Large file I/O:", await large_file_io("/path/to/large/file.txt"))

if __name__ == "__main__":
    asyncio.run(main())
