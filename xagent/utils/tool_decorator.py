# Standard library imports
import asyncio
import functools
import inspect
import sys
import enum
from typing import Any, Callable, Dict, Literal, Optional, Union, get_args, get_origin, get_type_hints


def _create_async_wrapper(func: Callable) -> Callable:
    """
    Create an async wrapper for a function using first principles approach.
    
    Key principles:
    1. Preserve function identity and metadata
    2. Efficient async/sync detection
    3. Robust error handling
    4. Optimal execution strategy based on context
    """
    # If already async, return as-is (zero-cost abstraction)
    if asyncio.iscoroutinefunction(func):
        return func
    
    # Cache the check for asyncio.to_thread availability (avoid repeated hasattr calls)
    _has_to_thread = hasattr(asyncio, "to_thread")
    
    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        """Async wrapper that handles both sync and async execution contexts."""
        try:
            # Check if we're in an async context
            loop = asyncio.get_running_loop()
            
            # Execute in thread pool to avoid blocking the event loop
            if _has_to_thread:
                # asyncio.to_thread is more efficient and preferred (Python 3.9+)
                return await asyncio.to_thread(func, *args, **kwargs)
            else:
                # Fallback to run_in_executor for older Python versions
                return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
                
        except RuntimeError:
            # No event loop running - execute synchronously
            # This handles cases where the function is called outside async context
            return func(*args, **kwargs)
        except Exception as e:
            # Preserve original exception context and stack trace
            raise e from None
    
    return async_wrapper


def python_type_to_openai_type(py_type: Any) -> Dict[str, Any]:
    """Convert Python types to OpenAI function call parameter schema."""
    # Enums -> enum schema based on member values
    if inspect.isclass(py_type) and issubclass(py_type, enum.Enum):
        values = [m.value for m in py_type]
        if values:
            base_type = "boolean" if isinstance(values[0], bool) else "integer" if isinstance(values[0], int) else "number" if isinstance(values[0], float) else "string"
            return {"type": base_type, "enum": values}
        return {"type": "string", "enum": []}

    # Basic type mappings
    basic_types = {
        int: {"type": "integer"}, float: {"type": "number"}, bool: {"type": "boolean"}, 
        str: {"type": "string"}, list: {"type": "array", "items": {"type": "string"}}, 
        dict: {"type": "object"}
    }
    if py_type in basic_types:
        return basic_types[py_type]

    origin = get_origin(py_type)
    args = get_args(py_type)

    # Literal
    if origin is Literal and args:
        base_type = "boolean" if isinstance(args[0], bool) else "integer" if isinstance(args[0], int) else "number" if isinstance(args[0], float) else "string"
        return {"type": base_type, "enum": list(args)}

    # Union and Optional
    if origin is Union:
        non_none_args = [a for a in args if a is not type(None)]
        if non_none_args:
            return python_type_to_openai_type(non_none_args[0])

    # Python 3.10+ union syntax (X | Y) - check for types.UnionType
    if sys.version_info >= (3, 10):
        import types
        if isinstance(py_type, types.UnionType):
            union_args = getattr(py_type, "__args__", ())
            if union_args:
                # Handle union by taking the first non-None type
                non_none_args = [a for a in union_args if a is not type(None)]
                if non_none_args:
                    return python_type_to_openai_type(non_none_args[0])

    # Sequence-like -> array
    if origin in (list, set, tuple):
        item_schema = python_type_to_openai_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}

    # Mapping/Dict -> object
    if origin in (dict,):
        if len(args) == 2:
            # Check if key type is string-like
            key_type = args[0]
            if key_type in (Any, str) or (get_origin(key_type) is Literal and all(isinstance(a, str) for a in get_args(key_type))):
                return {"type": "object", "additionalProperties": python_type_to_openai_type(args[1])}
        return {"type": "object"}

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

        # Create async wrapper using first principles approach
        async_func = _create_async_wrapper(func)

        # Attach metadata
        async_func.tool_spec = tool_spec
        async_func.__name__ = func.__name__
        async_func.__doc__ = func.__doc__
        async_func.__module__ = func.__module__
        async_func.__qualname__ = getattr(func, "__qualname__", func.__name__)

        return async_func

    return decorator