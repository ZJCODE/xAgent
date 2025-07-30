import base64
import tempfile
import os

from langfuse import observe
from langfuse.openai import AsyncOpenAI

from utils.tool_decorator import function_tool

from dotenv import load_dotenv
load_dotenv(override=True)

client = AsyncOpenAI()
DEFAULT_MODEL = "gpt-4o-mini"

@function_tool()
@observe()
async def web_search(search_query: str) -> str:
    "when the user wants to search the web using a search engine, use this tool"

    response = await client.responses.create(
        model=DEFAULT_MODEL,
        tools=[{"type": "web_search_preview"},],
        input=search_query,
        tool_choice="required"
    )

    return response.output_text

@function_tool()
@observe()
async def draw_image(prompt: str) -> str:
    """
    when the user wants to generate an image based on a prompt, use this tool
    """


    response = await client.responses.create(
        model=DEFAULT_MODEL,
        tools=[{"type": "image_generation", "quality": "low"}],
        input=prompt,
        tool_choice="required"
    )

    tool_calls = response.output
    for tool_call in tool_calls:
        if tool_call.type == "image_generation_call":
            image_base64 = tool_call.result
            try:
                with tempfile.NamedTemporaryFile(prefix="generated_", suffix=".png", delete=False) as tmp_file:
                    tmp_file.write(base64.b64decode(image_base64))
                    tmp_path = tmp_file.name
                # Upload and get URL
                url = upload_image(tmp_path)
                os.remove(tmp_path)
                return url
            except Exception as e:
                return f'![generated image](data:image/png;base64,{image_base64})'

def upload_image(image_path: str) -> str:
    from utils.image_upload import upload_image as s3_upload_image
    url = s3_upload_image(image_path)
    if not url:
        raise Exception("Image upload failed")
    return url


if __name__ == "__main__":

    import asyncio
    # search_result = asyncio.run(web_search("What is the capital of France?"))
    # print("Search Result:", search_result)

    image_url = asyncio.run(draw_image("A beautiful sunset over the mountains"))
    # print("Generated Image URL:", image_url)
    # Note: Ensure you have the necessary API keys and environment setup for OpenAI and sm.ms