
from langfuse import observe
from langfuse.openai import OpenAI

from utils.tool_decorator import function_tool

client = OpenAI()
DEFAULT_MODEL = "gpt-4o-mini"

@function_tool()
def web_search(search_query: str) -> str:
    "when the user wants to search the web using a search engine, use this tool"

    response = client.responses.create(
        model=DEFAULT_MODEL,
        tools=[{"type": "web_search_preview"},],
        input=search_query,
        tool_choice="required"
    )

    return response.output_text

@function_tool()
def draw_image(prompt: str, quality: str = "low") -> str:
    """
    when the user wants to generate an image based on a prompt, use this tool
    """
    response = client.responses.create(
        model=DEFAULT_MODEL,
        tools=[{"type": "image_generation", "quality": quality}],
        input=prompt,
        tool_choice="required"
    )

    tool_calls = response.output
    for tool_call in tool_calls:
        history_count += 1  # 增加历史条数以弥补Tool消息的占用
        if tool_call.type == "image_generation_call":
            image_base64 = tool_call.result
            return f'![generated image](data:image/png;base64,{image_base64})'

    