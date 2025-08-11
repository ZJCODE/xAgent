from xagent.utils.tool_decorator import function_tool

@function_tool()
def say_hello(name: str) -> str:
    """
    A simple tool that returns a greeting message.
    
    Args:
        name (str): The name of the person to greet.
        
    Returns:
        str: A greeting message.
    """
    return f"Hello, {name}!"