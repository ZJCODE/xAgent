# Standard library imports
import asyncio
import functools
import inspect
import sys
from typing import Any, Callable, Dict, List, Literal, Optional, Union, get_args, get_origin, get_type_hints

# Module-level constants and configuration
class ToolDecoratorConfig:
    """Configuration constants for tool decorator functionality."""
    
    # OpenAI tool schema constants
    FUNCTION_TYPE = "function"
    OBJECT_TYPE = "object"
    ARRAY_TYPE = "array"
    STRING_TYPE = "string"
    INTEGER_TYPE = "integer"
    NUMBER_TYPE = "number"
    BOOLEAN_TYPE = "boolean"
    
    # Default values
    DEFAULT_ARRAY_ITEM_TYPE = {"type": STRING_TYPE}
    DEFAULT_FALLBACK_TYPE = {"type": STRING_TYPE}
    
    # Schema properties
    ADDITIONAL_PROPERTIES_DEFAULT = False
    
    # Error messages
    ERROR_INVALID_UNION_TYPE = "Unsupported Union type structure"
    ERROR_UNSUPPORTED_TYPE = "Unsupported type for OpenAI tool parameter"


class TypeMappingError(Exception):
    """Exception raised when type mapping fails."""
    pass

def python_type_to_openai_type(py_type: Any) -> Dict[str, Any]:
    """
    Convert Python types to OpenAI function call parameter schema.
    
    This function handles a wide range of Python types and converts them to
    JSON Schema format compatible with OpenAI function calling.
    
    Args:
        py_type: Python type annotation to convert
        
    Returns:
        Dictionary containing OpenAI-compatible type schema
        
    Raises:
        TypeMappingError: If the type cannot be mapped to OpenAI schema
        
    Supported Types:
        - Basic types: str, int, float, bool
        - Collections: list, dict
        - Optional types: Optional[T], Union[T, None]
        - Literal types: Literal["a", "b", "c"]
        - Generic types: List[T]
        - Union types (uses first type)
        - Python 3.10+ union syntax (X | Y)
    """
    try:
        # Handle Literal types (enums)
        if get_origin(py_type) is Literal:
            return _handle_literal_type(py_type)
        
        # Handle Union types (including Optional)
        if get_origin(py_type) is Union:
            return _handle_union_type(py_type)
        
        # Handle generic List types
        if get_origin(py_type) is list:
            return _handle_list_type(py_type)
        
        # Handle basic types
        basic_type_result = _handle_basic_type(py_type)
        if basic_type_result:
            return basic_type_result
        
        # Handle Python 3.10+ union syntax
        if _is_python310_union_type(py_type):
            return _handle_python310_union_type(py_type)
        
        # Fallback to string type with warning
        return ToolDecoratorConfig.DEFAULT_FALLBACK_TYPE.copy()
        
    except Exception as e:
        raise TypeMappingError(f"Failed to convert type {py_type}: {e}") from e


def _handle_literal_type(py_type: Any) -> Dict[str, Any]:
    """Handle Literal type annotations."""
    args = get_args(py_type)
    if not args:
        return {ToolDecoratorConfig.STRING_TYPE: ToolDecoratorConfig.STRING_TYPE}
    
    # Infer the base type from literal values
    first_value = args[0]
    if isinstance(first_value, str):
        base_type = ToolDecoratorConfig.STRING_TYPE
    elif isinstance(first_value, int):
        base_type = ToolDecoratorConfig.INTEGER_TYPE
    elif isinstance(first_value, float):
        base_type = ToolDecoratorConfig.NUMBER_TYPE
    elif isinstance(first_value, bool):
        base_type = ToolDecoratorConfig.BOOLEAN_TYPE
    else:
        base_type = ToolDecoratorConfig.STRING_TYPE
    
    return {"type": base_type, "enum": list(args)}


def _handle_union_type(py_type: Any) -> Dict[str, Any]:
    """Handle Union type annotations, including Optional."""
    args = get_args(py_type)
    
    if not args:
        raise TypeMappingError("Union type has no arguments")
    
    # Handle Optional type (Union[T, None])
    if len(args) == 2 and type(None) in args:
        non_none_type = args[0] if args[1] is type(None) else args[1]
        return python_type_to_openai_type(non_none_type)
    
    # For complex unions, use the first non-None type
    for arg in args:
        if arg is not type(None):
            return python_type_to_openai_type(arg)
    
    raise TypeMappingError(ToolDecoratorConfig.ERROR_INVALID_UNION_TYPE)


def _handle_list_type(py_type: Any) -> Dict[str, Any]:
    """Handle List type annotations."""
    args = get_args(py_type)
    
    if args:
        item_type_schema = python_type_to_openai_type(args[0])
        return {"type": ToolDecoratorConfig.ARRAY_TYPE, "items": item_type_schema}
    
    return {
        "type": ToolDecoratorConfig.ARRAY_TYPE, 
        "items": ToolDecoratorConfig.DEFAULT_ARRAY_ITEM_TYPE.copy()
    }


def _handle_basic_type(py_type: Any) -> Optional[Dict[str, Any]]:
    """Handle basic Python types."""
    type_mapping = {
        int: {"type": ToolDecoratorConfig.INTEGER_TYPE},
        float: {"type": ToolDecoratorConfig.NUMBER_TYPE},
        bool: {"type": ToolDecoratorConfig.BOOLEAN_TYPE},
        str: {"type": ToolDecoratorConfig.STRING_TYPE},
        list: {
            "type": ToolDecoratorConfig.ARRAY_TYPE, 
            "items": ToolDecoratorConfig.DEFAULT_ARRAY_ITEM_TYPE.copy()
        },
        dict: {"type": ToolDecoratorConfig.OBJECT_TYPE},
    }
    
    return type_mapping.get(py_type)


def _is_python310_union_type(py_type: Any) -> bool:
    """Check if type is Python 3.10+ union syntax (X | Y)."""
    return (
        sys.version_info >= (3, 10) and 
        hasattr(py_type, '__class__') and 
        py_type.__class__.__name__ == 'UnionType'
    )


def _handle_python310_union_type(py_type: Any) -> Dict[str, Any]:
    """Handle Python 3.10+ union type syntax."""
    if not hasattr(py_type, '__args__'):
        raise TypeMappingError("Python 3.10 union type has no __args__")
    
    args = py_type.__args__
    if not args:
        raise TypeMappingError("Python 3.10 union type has empty __args__")
    
    # Use the first type in the union
    return python_type_to_openai_type(args[0])

def function_tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    strict: bool = False,
    param_descriptions: Optional[Dict[str, str]] = None
) -> Callable[[Callable], Callable]:
    """
    Decorator to convert Python functions into OpenAI function call tools.
    
    This decorator analyzes function signatures and type hints to automatically
    generate OpenAI-compatible tool specifications. It also handles async/sync
    function conversion transparently.
    
    Args:
        name: Tool name (defaults to function name)
        description: Tool description (defaults to function docstring)
        strict: Enable strict mode for parameter validation
        param_descriptions: Dictionary mapping parameter names to descriptions
        
    Returns:
        Decorated function with tool_spec attribute containing OpenAI tool schema
        
    Raises:
        TypeMappingError: If function parameter types cannot be converted
        ValueError: If function signature is invalid
        
    Example:
        @function_tool(
            name="calculate_sum",
            description="Calculate the sum of two numbers",
            param_descriptions={
                "a": "First number to add",
                "b": "Second number to add"
            }
        )
        def add_numbers(a: int, b: int) -> int:
            '''Add two numbers together.'''
            return a + b
            
        # Access the tool specification
        print(add_numbers.tool_spec)
        
        # Call the function normally
        result = await add_numbers(5, 3)
    """
    def decorator(func: Callable) -> Callable:
        try:
            # Generate tool specification
            tool_spec = _generate_tool_spec(
                func, name, description, strict, param_descriptions
            )
            
            # Create async wrapper if needed
            async_func = _ensure_async_function(func)
            
            # Attach tool spec and preserve metadata
            async_func.tool_spec = tool_spec
            async_func.__name__ = func.__name__
            async_func.__doc__ = func.__doc__
            async_func.__module__ = func.__module__
            async_func.__qualname__ = getattr(func, '__qualname__', func.__name__)
            
            return async_func
            
        except Exception as e:
            raise ValueError(f"Failed to create tool from function {func.__name__}: {e}") from e
    
    return decorator


def _generate_tool_spec(
    func: Callable,
    name: Optional[str],
    description: Optional[str],
    strict: bool,
    param_descriptions: Optional[Dict[str, str]]
) -> Dict[str, Any]:
    """Generate OpenAI tool specification from function metadata."""
    signature = inspect.signature(func)
    type_hints = get_type_hints(func)
    
    # Build parameter schema
    parameters_schema = _build_parameters_schema(
        signature, type_hints, param_descriptions
    )
    
    # Build tool specification
    tool_spec = {
        "type": ToolDecoratorConfig.FUNCTION_TYPE,
        "name": name or func.__name__,
        "description": _extract_function_description(func, description),
        "parameters": parameters_schema
    }
    
    # Add strict mode if enabled
    if strict:
        tool_spec["strict"] = True
    
    return tool_spec


def _build_parameters_schema(
    signature: inspect.Signature,
    type_hints: Dict[str, Any],
    param_descriptions: Optional[Dict[str, str]]
) -> Dict[str, Any]:
    """Build OpenAI parameters schema from function signature."""
    properties = {}
    required = []
    
    for param in signature.parameters.values():
        # Skip *args and **kwargs
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        
        # Get parameter type and convert to OpenAI schema
        param_type = type_hints.get(param.name, str)
        param_schema = python_type_to_openai_type(param_type)
        
        # Add description if provided
        if param_descriptions and param.name in param_descriptions:
            param_schema["description"] = param_descriptions[param.name]
        
        properties[param.name] = param_schema
        
        # Add to required if no default value
        if param.default is param.empty:
            required.append(param.name)
    
    return {
        "type": ToolDecoratorConfig.OBJECT_TYPE,
        "properties": properties,
        "required": required,
        "additionalProperties": ToolDecoratorConfig.ADDITIONAL_PROPERTIES_DEFAULT
    }


def _extract_function_description(func: Callable, description: Optional[str]) -> str:
    """Extract function description from parameter or docstring."""
    if description:
        return description
    
    if func.__doc__:
        # Clean up docstring
        docstring = inspect.cleandoc(func.__doc__)
        # Use first line if multiline
        return docstring.split('\n')[0] if '\n' in docstring else docstring
    
    return f"Function {func.__name__}"


def _ensure_async_function(func: Callable) -> Callable:
    """Ensure function is async, wrapping sync functions as needed."""
    if asyncio.iscoroutinefunction(func):
        return func
    
    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        """Async wrapper for synchronous functions."""
        try:
            # Try to get the current event loop
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop running, create one
            return func(*args, **kwargs)
        
        # Run in thread pool to avoid blocking the event loop
        partial_func = functools.partial(func, *args, **kwargs)
        return await loop.run_in_executor(None, partial_func)
    
    return async_wrapper


# Utility functions for tool validation and introspection

def validate_tool_spec(tool_spec: Dict[str, Any]) -> bool:
    """
    Validate an OpenAI tool specification.
    
    Args:
        tool_spec: Tool specification dictionary to validate
        
    Returns:
        True if valid, False otherwise
        
    Raises:
        ValueError: If tool specification is invalid
    """
    required_fields = ["type", "name", "parameters"]
    
    for field in required_fields:
        if field not in tool_spec:
            raise ValueError(f"Missing required field: {field}")
    
    if tool_spec["type"] != ToolDecoratorConfig.FUNCTION_TYPE:
        raise ValueError(f"Invalid type: {tool_spec['type']}")
    
    if not isinstance(tool_spec["name"], str) or not tool_spec["name"]:
        raise ValueError("Tool name must be a non-empty string")
    
    # Validate parameters schema
    params = tool_spec["parameters"]
    if not isinstance(params, dict):
        raise ValueError("Parameters must be a dictionary")
    
    if params.get("type") != ToolDecoratorConfig.OBJECT_TYPE:
        raise ValueError("Parameters type must be 'object'")
    
    return True


def get_tool_signature(func: Callable) -> str:
    """
    Get a human-readable signature for a tool function.
    
    Args:
        func: Function with tool_spec attribute
        
    Returns:
        String representation of the tool signature
    """
    if not hasattr(func, 'tool_spec'):
        raise ValueError("Function is not a tool (missing tool_spec)")
    
    tool_spec = func.tool_spec
    params = tool_spec["parameters"]["properties"]
    required = set(tool_spec["parameters"]["required"])
    
    param_strs = []
    for param_name, param_spec in params.items():
        param_type = param_spec.get("type", "unknown")
        if param_name in required:
            param_strs.append(f"{param_name}: {param_type}")
        else:
            param_strs.append(f"{param_name}: {param_type} = optional")
    
    signature = f"{tool_spec['name']}({', '.join(param_strs)})"
    return signature


def list_supported_types() -> List[str]:
    """
    Get a list of supported Python types for tool parameters.
    
    Returns:
        List of supported type names
    """
    return [
        "int", "float", "bool", "str", "list", "dict",
        "Optional[T]", "Union[T, ...]", "Literal[...]", 
        "List[T]", "typing.Any"
    ]


def create_tool_from_callable(
    func: Callable,
    **kwargs
) -> Callable:
    """
    Create a tool from any callable with automatic name and description inference.
    
    Args:
        func: Callable to convert to a tool
        **kwargs: Additional arguments to pass to function_tool decorator
        
    Returns:
        Tool-decorated function
    """
    # Infer name from function if not provided
    if 'name' not in kwargs:
        kwargs['name'] = getattr(func, '__name__', 'unnamed_tool')
    
    # Infer description from docstring if not provided
    if 'description' not in kwargs and hasattr(func, '__doc__') and func.__doc__:
        kwargs['description'] = inspect.cleandoc(func.__doc__).split('\n')[0]
    
    # Apply decorator
    decorator = function_tool(**kwargs)
    return decorator(func)


def get_tool_metadata(func: Callable) -> Dict[str, Any]:
    """
    Extract comprehensive metadata from a tool function.
    
    Args:
        func: Tool function to analyze
        
    Returns:
        Dictionary containing tool metadata
    """
    if not hasattr(func, 'tool_spec'):
        raise ValueError("Function is not a tool (missing tool_spec)")
    
    tool_spec = func.tool_spec
    signature = inspect.signature(func)
    
    return {
        "name": tool_spec["name"],
        "description": tool_spec["description"],
        "parameter_count": len(tool_spec["parameters"]["properties"]),
        "required_parameters": tool_spec["parameters"]["required"],
        "optional_parameters": [
            param for param in tool_spec["parameters"]["properties"].keys()
            if param not in tool_spec["parameters"]["required"]
        ],
        "is_async": asyncio.iscoroutinefunction(func),
        "strict_mode": tool_spec.get("strict", False),
        "function_signature": str(signature),
        "module": getattr(func, '__module__', 'unknown'),
    }