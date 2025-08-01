import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from pydantic import BaseModel
from tools.openai_tool import web_search
from xagent.utils.tool_decorator import function_tool
from xagent.utils.mcp_convertor import MCPTool
from xagent.db import MessageDB
from xagent.core import Agent
from xagent.core import Session



@function_tool()
def add(a: int, b: int) -> int:
    "Add two numbers."
    return a + b

@function_tool()
def multiply(a: int, b: int) -> int:
    "Multiply two numbers."
    return a * b

# normal_tools = [add, multiply, web_search]
# mcp_tools = []

# try:
#     mt = MCPTool("http://127.0.0.1:8001/mcp/")
#     mcp_tools = asyncio.run(mt.get_openai_tools())
# except ImportError:
#     print("MCPTool not available, skipping MCP tools.")

# agent = Agent(tools=normal_tools + mcp_tools,
#               system_prompt="when you need to calculate, you can use the tools provided, such as add and multiply. If you need to search the web, use the web_search tool. If you want roll a dice, use the roll_dice tool.",
#               model="gpt-4.1-mini")

story_agent = Agent(name = "story_agent",
                    system_prompt="you are a story teller.",
                    model="gpt-4.1-mini")

story_tool = story_agent.as_tool(name="story_tool", description="A tool to tell stories based on user input and return the story for reference.")

# res = asyncio.run(story_tool("Can you tell me a story about a brave knight?"))
# print("Story Tool Result:", res)

agent = Agent(tools=[add, multiply, web_search, story_tool],
                mcp_servers="http://127.0.0.1:8001/mcp/",
                system_prompt="when you need to calculate, you can use the tools provided, such as add and multiply. " \
                "If you need to search the web, use the web_search tool. " \
                "If you want roll a dice, use the roll_dice tool." \
                "If you want to tell a story, use the story tool.",
                model="gpt-4.1")


# session = Session(user_id="user123123", session_id="test_session", message_db=MessageDB())
session = Session(user_id="user123", message_db=MessageDB())
session.clear_session()  # 清空历史以便测试

# reply = agent("the answer for 12 + 13 is", session)
# print("Reply:", reply)

# reply = agent("roll a dice three times", session)
# print("Reply:", reply)

# reply = agent("the answer for 10 + 20 is and 21 + 22 is", session)
# print("Reply:", reply)

reply = agent("Can you tell me a story about a brave knight?", session)
print("Reply:", reply)

# reply = agent("What is 18+2*4+3+4*5?", session)
# print("Reply:", reply)

# assistant_item = session.pop_message()  # Remove agent's response
# user_item = session.pop_message()  # Remove user's question

# print("Last user message:", user_item.content)
# print("Last assistant message:", assistant_item.content)

# reply = agent("The Weather in Hangzhou and Beijing is", session)
# print("Reply:", reply)

class Step(BaseModel):
    explanation: str
    output: str

class MathReasoning(BaseModel):
    steps: list[Step]
    final_answer: str

reply = agent("how can I solve 8x + 7 = -23", session, output_type=MathReasoning)
for step in reply.steps:
    print(f"Step: {step.explanation} => Output: {step.output}")
print("Final Answer:", reply.final_answer)


# reply = agent("Can you describe the image?", session = session,image_source="https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/2560px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg")
# print("Reply:", reply)


# import base64
# def encode_image(image_path):
#     with open(image_path, "rb") as image_file:
#         return base64.b64encode(image_file.read()).decode("utf-8")

# # Path to your image
# image_path = "tests/assets/test_image.png"
# # Getting the Base64 string
# base64_image = f"data:image/jpeg;base64,{encode_image(image_path)}"

# reply = agent("Can you describe the image?", session = session,image_source=base64_image)
# print("Reply:", reply)

# reply = agent("Can you describe the image?", session = session,image_source="tests/assets/test_image.png")
# print("Reply:", reply)

print("Session history:")
for msg in session.get_messages():
    print(f"{msg.role}: {msg.content} (at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.timestamp))})")