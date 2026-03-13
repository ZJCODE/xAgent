from openai import AsyncOpenAI

from xagent.utils.tool_decorator import function_tool

DEFAULT_MODEL = "gpt-4o-mini"
_SHARED_CLIENT: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = AsyncOpenAI()
    return _SHARED_CLIENT

@function_tool(name="web_search",
               description="Search the web using a search engine.",
               param_descriptions={
                     "search_query": "The query to search for on the web."
               }
)
async def web_search(search_query: str) -> str:
    "when the user wants to search the web using a search engine, use this tool"

    query = (search_query or "").strip()
    if not query:
        return ""
    
    client = _get_client()
    response = await client.responses.create(
        model=DEFAULT_MODEL,
        tools=[{"type": "web_search_preview"},],
        input=query,
        tool_choice="required"
    )

    return (getattr(response, "output_text", "") or "").strip()


@function_tool(name="draw_image", 
               description="Generate an image based on a text prompt.",
               param_descriptions={
                     "prompt": "A detailed text prompt describing the image to generate. Include style, subject, composition, colors, and any specific details."
               }
)
async def draw_image(prompt: str) -> str:
    """
    when the user wants to generate an image based on a prompt, use this tool
    """

    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return ""
    
    client = _get_client()
    response = await client.responses.create(
        model=DEFAULT_MODEL,
        tools=[{"type": "image_generation", "quality": "low"}],
        input=[{
            "role": "user",
            "content": clean_prompt
        }],
        tool_choice="required"
    )

    tool_calls = getattr(response, "output", [])
    image_call = next((tc for tc in tool_calls if getattr(tc, "type", "") == "image_generation_call"), None)
    if not image_call or not getattr(image_call, "result", None):
        return ""

    image_base64 = image_call.result
    return f"![generated image](data:image/png;base64,{image_base64})"


if __name__ == "__main__":
    print(draw_image.tool_spec)
