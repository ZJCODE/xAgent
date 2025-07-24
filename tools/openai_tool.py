import base64
import tempfile
import os

from langfuse import observe
from langfuse.openai import OpenAI

from utils.tool_decorator import function_tool

client = OpenAI()
DEFAULT_MODEL = "gpt-4o-mini"

@function_tool()
@observe()
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
@observe()
def draw_image(prompt: str) -> str:
    """
    when the user wants to generate an image based on a prompt, use this tool
    """


    response = client.responses.create(
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
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                    tmp_file.write(base64.b64decode(image_base64))
                    tmp_path = tmp_file.name
                # Upload and get URL
                url = upload_image(tmp_path)
                os.remove(tmp_path)
                return url
            except Exception as e:
                return f'![generated image](data:image/png;base64,{image_base64})'

def upload_image(image_path: str) -> str:
    raise Exception("Upload image function not implemented")


if __name__ == "__main__":
    # Example usage
    search_result = web_search("What is the capital of France?")
    print("Search Result:", search_result)

    image_url = draw_image("A beautiful sunset over the mountains")
    print("Generated Image URL:", image_url)
    # Note: Ensure you have the necessary API keys and environment setup for OpenAI and sm.ms
