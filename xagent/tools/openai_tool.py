import base64
import tempfile
import os
from dotenv import load_dotenv

load_dotenv(override=True)

from langfuse import observe
from langfuse.openai import AsyncOpenAI

from xagent.utils.tool_decorator import function_tool
from xagent.utils.image_upload import upload_image as s3_upload_image

DEFAULT_MODEL = "gpt-4o-mini"

@function_tool()
@observe()
async def web_search(search_query: str) -> str:
    "when the user wants to search the web using a search engine, use this tool"

    query = (search_query or "").strip()
    if not query:
        return ""
    
    client = AsyncOpenAI()
    response = await client.responses.create(
        model=DEFAULT_MODEL,
        tools=[{"type": "web_search_preview"},],
        input=query,
        tool_choice="required"
    )

    return (getattr(response, "output_text", "") or "").strip()

@function_tool()
@observe()
async def draw_image(prompt: str) -> str:
    """
    when the user wants to generate an image based on a prompt, use this tool
    """


    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return ""
    
    client = AsyncOpenAI()
    response = await client.responses.create(
        model=DEFAULT_MODEL,
        tools=[{"type": "image_generation", "quality": "low"}],
        input=clean_prompt,
        tool_choice="required"
    )

    tool_calls = getattr(response, "output", [])
    image_call = next((tc for tc in tool_calls if getattr(tc, "type", "") == "image_generation_call"), None)
    if not image_call or not getattr(image_call, "result", None):
        return ""

    image_base64 = image_call.result
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="generated_", suffix=".png", delete=False) as tmp_file:
            tmp_file.write(base64.b64decode(image_base64))
            tmp_path = tmp_file.name
        # Upload and get URL
        url = upload_image(tmp_path)
        return url
    except Exception:
        # Fallback to inline base64 image markdown
        return f'![generated image](data:image/png;base64,{image_base64})'
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

def upload_image(image_path: str) -> str:
    url = s3_upload_image(image_path)
    if not url:
        raise Exception("Image upload failed")
    return url


if __name__ == "__main__":

    import asyncio
    search_result = asyncio.run(web_search("What is the weather like today in hangzhou?"))
    print("Search Result:", search_result)

    image_url = asyncio.run(draw_image("A beautiful sunset over the mountains"))
    print("Generated Image URL:", image_url)