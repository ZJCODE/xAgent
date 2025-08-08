import random
from fastmcp import FastMCP

from vocabulary.vocabulary import Vocabulary

mcp = FastMCP(name="MCP Server")

vocabulary = Vocabulary()

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

@mcp.tool(enabled=True)
def look_up_word(word: str, user_id: str) -> str:
    """when user asks for a word definition or meaning, use this tool to look it up."""
    try:
        result = vocabulary.lookup_word(word=word, user_id=user_id)
        result = f"Definition of '{word}':\n {result}"
    except Exception as e:
        result = f"Error looking up word: {str(e)}"
    return result

@mcp.tool(enabled=True)
def get_vocabulary(user_id: str, n: int) -> list[str]:
    """when user asks for vocabulary list for review or study, use this tool to retrieve it."""
    try:
        vocabularies = vocabulary.get_vocabulary(user_id=user_id, n=n, exclude_known=True)
        vocabularies = [f"Vocabulary:\n {vocab}" for vocab in vocabularies]
    except Exception as e:
        vocabularies = [f"Error retrieving vocabulary: {str(e)}"]
    return vocabularies

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