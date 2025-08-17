# Standard library imports
import asyncio
import functools
import inspect
import sys
from typing import Any, Callable, Dict, List, Literal, Optional, Union, get_args, get_origin, get_type_hints


class TypeMappingError(Exception):
    """Exception raised when type mapping fails."""
    pass


def python_type_to_openai_type(py_type: Any) -> Dict[str, Any]:
    """Convert Python types to OpenAI function call parameter schema."""
    # Basic type mappings
    basic_types = {
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        str: {"type": "string"},
        list: {"type": "array", "items": {"type": "string"}},
        dict: {"type": "object"},
    }
    
    # Handle basic types
    if py_type in basic_types:
        return basic_types[py_type]
    
    origin = get_origin(py_type)
    args = get_args(py_type)
    
    # Handle Literal types
    if origin is Literal:
        if not args:
            return {"type": "string"}
        first_val = args[0]
        base_type = {str: "string", int: "integer", float: "number", bool: "boolean"}.get(type(first_val), "string")
        return {"type": base_type, "enum": list(args)}
    
    # Handle Union types (including Optional)
    if origin is Union:
        if len(args) == 2 and type(None) in args:
            # Optional type
            non_none_type = args[0] if args[1] is type(None) else args[1]
            return python_type_to_openai_type(non_none_type)
        # Use first non-None type for complex unions
        for arg in args:
            if arg is not type(None):
                return python_type_to_openai_type(arg)
    
    # Handle List types
    if origin is list:
        item_schema = python_type_to_openai_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}
    
    # Handle Python 3.10+ union syntax
    if sys.version_info >= (3, 10) and hasattr(py_type, '__class__') and py_type.__class__.__name__ == 'UnionType':
        return python_type_to_openai_type(py_type.__args__[0]) if py_type.__args__ else {"type": "string"}
    
    # Fallback
    return {"type": "string"}


def function_tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    strict: bool = False,
    param_descriptions: Optional[Dict[str, str]] = None
) -> Callable[[Callable], Callable]:
    """Decorator to convert Python functions into OpenAI function call tools."""
    
    def decorator(func: Callable) -> Callable:
        # Generate tool spec
        signature = inspect.signature(func)
        type_hints = get_type_hints(func)
        
        # Build parameters
        properties = {}
        required = []
        
        for param in signature.parameters.values():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            
            param_type = type_hints.get(param.name, str)
            param_schema = python_type_to_openai_type(param_type)
            
            if param_descriptions and param.name in param_descriptions:
                param_schema["description"] = param_descriptions[param.name]
            
            properties[param.name] = param_schema
            
            if param.default is param.empty:
                required.append(param.name)
        
        # Build tool spec
        tool_spec = {
            "type": "function",
            "name": name or func.__name__,
            "description": description or (func.__doc__.split('\n')[0] if func.__doc__ else f"Function {func.__name__}"),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False
            }
        }
        
        if strict:
            tool_spec["strict"] = True
        
        # Create async wrapper
        if asyncio.iscoroutinefunction(func):
            async_func = func
        else:
            @functools.wraps(func)
            async def async_func(*args, **kwargs):
                try:
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
                except RuntimeError:
                    return func(*args, **kwargs)
        
        # Attach metadata
        async_func.tool_spec = tool_spec
        async_func.__name__ = func.__name__
        async_func.__doc__ = func.__doc__
        
        return async_func
    
    return decorator