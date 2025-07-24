import inspect
import functools
from typing import get_type_hints

def python_type_to_openai_type(py_type):
    # 只支持基础类型
    if py_type is int:
        return {"type": "integer"}
    if py_type is float:
        return {"type": "number"}
    if py_type is bool:
        return {"type": "boolean"}
    if py_type is str:
        return {"type": "string"}
    if py_type is list:
        return {"type": "array", "items": {"type": "string"}}
    if py_type is dict:
        return {"type": "object"}
    return {"type": "string"}

def function_tool(name: str = None, description: str = None):
    """
    将函数包装成 openai tool call 所需的规范格式，仅支持基础类型

    Args:
        name (str): 工具名称，默认为函数名
        description (str): 工具描述，默认为函数文档字符串
    Returns:
        function: 包装后的函数，具有 tool_spec 属性
    Raises:
        TypeError: 如果函数参数类型不支持
        ValueError: 如果函数没有参数或返回值类型不支持

    Example usage:
        @function_tool(name="my_tool", description="This is my tool")
        def my_tool(param1: int, param2: str):
            \"\"\"This is my tool function.\"\"\"
            return f"Received {param1} and {param2}"

        print(my_tool.tool_spec)
    """
    import asyncio
    def decorator(func):
        sig = inspect.signature(func)
        type_hints = get_type_hints(func)
        properties = {}
        required = []
        for param in sig.parameters.values():
            param_type = type_hints.get(param.name, str)
            properties[param.name] = python_type_to_openai_type(param_type)
            if param.default is param.empty:
                required.append(param.name)
        parameters = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False
        }
        tool = {
            "type": "function",
            "name": name or func.__name__,
            "description": description or (func.__doc__.strip() if func.__doc__ else ""),
            "parameters": parameters
        }
        # 自动包装为异步
        if asyncio.iscoroutinefunction(func):
            async_func = func
        else:
            async def async_func(*args, **kwargs):
                loop = asyncio.get_running_loop()
                partial_func = functools.partial(func, *args, **kwargs)
                return await loop.run_in_executor(None, partial_func)
            async_func.__name__ = func.__name__
            async_func.__doc__ = func.__doc__
        async_func.tool_spec = tool
        return async_func
    return decorator