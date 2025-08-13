import inspect
import functools
from typing import get_type_hints, get_origin, get_args, Union
import sys

def python_type_to_openai_type(py_type):
    # 处理 Optional 类型 (Optional[T] 等价于 Union[T, None])
    if get_origin(py_type) is Union:
        args = get_args(py_type)
        # 如果是 Optional 类型，去掉 None 后处理
        if len(args) == 2 and type(None) in args:
            non_none_type = args[0] if args[1] is type(None) else args[1]
            return python_type_to_openai_type(non_none_type)
        # 处理联合类型，选择第一个类型作为主要类型
        return python_type_to_openai_type(args[0])
    
    # 处理泛型 List
    if get_origin(py_type) is list:
        args = get_args(py_type)
        if args:
            item_type = python_type_to_openai_type(args[0])
            return {"type": "array", "items": item_type}
        return {"type": "array", "items": {"type": "string"}}
    
    # 基础类型处理
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
    
    # Python 3.10+ 的联合类型语法 (X | Y)
    if sys.version_info >= (3, 10) and hasattr(py_type, '__class__') and py_type.__class__.__name__ == 'UnionType':
        args = py_type.__args__
        return python_type_to_openai_type(args[0])
    
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