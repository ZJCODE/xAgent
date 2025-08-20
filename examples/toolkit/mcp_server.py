import random
from fastmcp import FastMCP

mcp = FastMCP(name="MCP Server")

## Tools

@mcp.tool(enabled=True)
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
    return "current user is Jun, 31 years old, lives in Hangzhou, China , hobby is reading books"


## Resources

@mcp.resource("data://config")
def get_config() -> dict:
    """Provides the application configuration."""
    return {"theme": "dark", "version": "1.0"}


# Prompts

@mcp.prompt
def analyze_data(data_points: list[float]) -> str:
    """Creates a prompt asking for analysis of numerical data."""
    formatted_data = ", ".join(str(point) for point in data_points)
    return f"Please analyze these data points: {formatted_data}"

def main():
    """Main entry point for xagent-mcp command."""
    import argparse
    
    parser = argparse.ArgumentParser(description="xAgent MCP Server")
    parser.add_argument("--port", type=int, default=8001, help="Port to bind to")
    parser.add_argument("--host", default="localhost", help="Host to bind to")
    parser.add_argument("--transport", default="http", help="Transport type")
    
    args = parser.parse_args()
    
    print(f"Starting xAgent MCP Server on {args.host}:{args.port}")
    mcp.run(transport=args.transport, port=args.port, host=args.host)

if __name__ == "__main__":
    main()