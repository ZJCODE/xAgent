import random
from fastmcp import FastMCP

mcp = FastMCP(name="Test MCP Server")

@mcp.tool
def roll_dice(n_dice: int) -> list[int]:
    """Roll `n_dice` 6-sided dice and return the results."""
    return [random.randint(1, 6) for _ in range(n_dice)]

@mcp.tool(enabled=False)
def add_numbers(a: int, b: int) -> int:
    """Add two numbers and return the result."""
    return a + b

@mcp.tool(
    name="get_user_details",
    exclude_args=["user_id"]
)
def get_user_details(user_id: str = None) -> str:
    """Retrieve user details based on user_id."""
    # user_id will be injected by the server, not provided by the LLM
    return "current user is 31 years old, lives in Hangzhou, China"

@mcp.resource("data://config")
def get_config() -> dict:
    """Provides the application configuration."""
    return {"theme": "dark", "version": "1.0"}

@mcp.prompt
def analyze_data(data_points: list[float]) -> str:
    """Creates a prompt asking for analysis of numerical data."""
    formatted_data = ", ".join(str(point) for point in data_points)
    return f"Please analyze these data points: {formatted_data}"

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)