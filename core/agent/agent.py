import time
from typing import Optional
import json
import logging
import asyncio
from dotenv import load_dotenv
from pydantic import BaseModel


# 日志系统初始化（只需一次）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

from langfuse import observe
from langfuse.openai import AsyncOpenAI

from schemas.messages import Message,ToolCall
from core.agent.session import Session
from utils.tool_decorator import function_tool

load_dotenv(override=True)

class Agent:
    """
    基础 Agent 类，支持与 OpenAI 模型交互。
    """

    DEFAULT_MODEL = "gpt-4.1-mini"
    DEFAULT_SYSTEM_PROMPT = "**Current date**: {date}, **Current timezone**: {timezone}"

    def __init__(
        self, 
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        client: Optional[AsyncOpenAI] = None,
        tools: Optional[list] = None,
    ):
        self.model: str = model or self.DEFAULT_MODEL
        self.system_prompt: str = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self.client: AsyncOpenAI = client or AsyncOpenAI()
        self.tools: dict = {}
        self._register_tools(tools)
        self.logger = logging.getLogger(self.__class__.__name__)


    def __call__(
            self, 
            user_message: Message | str, 
            session: Session, 
            history_count: int = 20, 
            max_iter: int = 5,
            image_source: Optional[str] = None,
            output_type: type[BaseModel] = None
    ) -> str | BaseModel:
        """
        支持同步调用 Agent（自动转异步）。
        """
        return asyncio.run(self.chat(user_message, session, history_count, max_iter, image_source, output_type))

    @observe()
    async def chat(
        self,
        user_message: Message | str,
        session: Session,
        history_count: int = 20,
        max_iter: int = 10,
        image_source: Optional[str] = None,
        output_type: type[BaseModel] = None
    ) -> str | BaseModel:
        """
        Generate a reply from the agent given a user message and session.

        Args:
            user_message (Message | str): The latest user message.
            session (Session): The session object managing message history.
            history_count (int, optional): Number of previous messages to include. Defaults to 20.
            max_iter (int, optional): Maximum model call attempts. Defaults to 10.
            image_source (Optional[str], optional): Source of the image, if any can be a URL or File path or base64 string.
            output_type (type[BaseModel], optional): Pydantic model for structured output.

        Returns:
            str | BaseModel: The agent's reply or error message.
        """
        try:
            # Store the incoming user message in session history
            self._store_user_message(user_message, session, image_source)

            for attempt in range(max_iter):
                # Build input messages for the model
                input_messages = self._build_input_messages(session, history_count)

                # Call the model and get response
                response = await self._call_model(input_messages, output_type)
                if response is None:
                    self.logger.warning("Model did not respond on attempt %d", attempt + 1)
                    return "Sorry, model did not respond."

                # Handle any tool calls in the response
                special_result = await self._handle_tool_calls(response.output, session)
                if special_result is not None:
                    return special_result

                if output_type is not None and hasattr(response, "output_parsed"):
                    # Structured output
                    self._store_model_reply(str(response.output_parsed), session)
                    return response.output_parsed

                # If model returned a text reply, store and return it
                reply_text = response.output_text
                if reply_text:
                    self._store_model_reply(reply_text, session)
                    return reply_text

            # If no valid reply after max_iter attempts
            self.logger.error("Failed to generate response after %d attempts", max_iter)
            return "Sorry, I could not generate a response after multiple attempts."

        except Exception as e:
            self.logger.exception("Agent chat error: %s", e)
            return "Sorry, something went wrong."

    def _register_tools(self, tools: Optional[list]) -> None:
        """
        注册工具函数，确保每个工具是异步的且唯一。
        """
        for fn in tools or []:
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(f"Tool function '{fn.tool_spec['name']}' must be async.")
            if fn.tool_spec['name'] not in self.tools:
                self.tools[fn.tool_spec['name']] = fn

    def _store_user_message(self, user_message: Message | str, session: Session, image_source: Optional[str]) -> None:
        if isinstance(user_message, str):
            user_message = Message.create(content=user_message, role="user", image_source=image_source)
        session.add_messages(user_message)

    def _store_model_reply(self, reply_text: str, session: Session) -> None:
        model_msg = Message.create(content=reply_text, role="assistant")
        session.add_messages(model_msg)

    def _build_input_messages(self, session: Session, history_count: int) -> list:
        """
        构造输入给大模型的消息列表。
        """
        # Sytem Message
        system_msg = {
            "role": "system",
            "content": self.system_prompt.format(date=time.strftime('%Y-%m-%d'), timezone=time.tzname[0])
        }
        # User History Messages
        history_msgs = []
        for msg in session.get_messages(history_count):
            history_msgs.append(msg.to_dict())
        return [system_msg] + history_msgs
    
    @observe()
    async def _call_model(self, input_msgs: list, output_type: type[BaseModel] = None) -> Optional[object]:
        """
        调用大模型，返回响应对象或 None。
        """
        try:
            if output_type is not None:
                return await self.client.responses.parse(
                    model=self.model,
                    tools=[fn.tool_spec for fn in self.tools.values()],
                    input=input_msgs,
                    text_format=output_type
                )
            else:
                return await self.client.responses.create(
                    model=self.model,
                    tools=[fn.tool_spec for fn in self.tools.values()],
                    input=input_msgs
                )
        except Exception as e:
            self.logger.error("Model call failed: %s", e)
            return None

    async def _handle_tool_calls(self, tool_calls: list, session: Session) -> Optional[str]:
        """
        异步并发处理所有 tool_call，返回特殊结果（如图片）或 None。
        """
        tasks = [self._act(tc, session) for tc in tool_calls if getattr(tc, "type", None) == "function_call"]
        results = await asyncio.gather(*tasks)
        for result in results:
            if result is not None:
                return result
        return None

    async def _act(self, tool_call, session: Session) -> Optional[str]:
        """
        异步执行工具函数调用，并将结果写入 session。
        """
        name = getattr(tool_call, "name", None)
        try:
            args = json.loads(getattr(tool_call, "arguments", "{}"))
        except Exception as e:
            self.logger.error("Tool args parse error: %s", e)
            return None
        func = self.tools.get(name)
        if func:
            self.logger.info("Calling tool: %s with args: %s", name, args)

            tool_call_msg = Message(
                type="function_call",
                role="tool", 
                content=f"Calling tool: `{name}` with args: {args}",
                tool_call=ToolCall(
                    call_id=getattr(tool_call, "call_id", ""),
                    name=name,
                    arguments=json.dumps(args)
                )
            )

            session.add_messages(tool_call_msg)

            try:
                result = await func(**args)
            except Exception as e:
                self.logger.error("Tool call error: %s", e)
                result = f"Tool error: {e}"

            tool_res_msg = Message(
                type="function_call_output",
                role="tool",
                content=f"Tool `{name}` result: {str(result) if len(str(result)) < 20 else str(result)[:20] + '...'}",
                tool_call=ToolCall(
                    call_id=getattr(tool_call, "call_id", "001"),
                    output=json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
                )
            )
            session.add_messages(tool_res_msg)

        return None

if __name__ == "__main__":


    from tools.openai_tool import web_search
    from db.message_db import MessageDB
    from tools.mcp_tool import MCPTool


    @function_tool()
    def add(a: int, b: int) -> int:
        "Add two numbers."
        return a + b
    
    @function_tool()
    def multiply(a: int, b: int) -> int:
        "Multiply two numbers."
        return a * b
    
    normal_tools = [add, multiply, web_search]
    mcp_tools = []

    try:
        mt = MCPTool("http://127.0.0.1:8000/mcp/")
        mcp_tools = asyncio.run(mt.get_openai_tools())
    except ImportError:
        print("MCPTool not available, skipping MCP tools.")
    
    agent = Agent(tools=normal_tools + mcp_tools,
                  system_prompt="when you need to calculate, you can use the tools provided, such as add and multiply. If you need to search the web, use the web_search tool. If you want roll a dice, use the roll_dice tool.",
                  model="gpt-4.1-mini")

    # session = Session(user_id="user123123", session_id="test_session", message_db=MessageDB())
    session = Session(user_id="user123", message_db=MessageDB())
    session.clear_session()  # 清空历史以便测试

    # reply = agent("the answer for 12 + 13 is", session)
    # print("Reply:", reply)

    reply = agent("roll a dice three times", session)
    print("Reply:", reply)

    # reply = agent("the answer for 10 + 20 is and 21 + 22 is", session)
    # print("Reply:", reply)

    # reply = agent("What is 18+2*4+3+4*5?", session)
    # print("Reply:", reply)

    # assistant_item = session.pop_message()  # Remove agent's response
    # user_item = session.pop_message()  # Remove user's question

    # print("Last user message:", user_item.content)
    # print("Last assistant message:", assistant_item.content)

    # reply = agent("The Weather in Hangzhou and Beijing is", session)
    # print("Reply:", reply)

    # class Step(BaseModel):
    #     explanation: str
    #     output: str

    # class MathReasoning(BaseModel):
    #     steps: list[Step]
    #     final_answer: str

    # reply = agent("how can I solve 8x + 7 = -23", session, output_type=MathReasoning)
    # for step in reply.steps:
    #     print(f"Step: {step.explanation} => Output: {step.output}")
    # print("Final Answer:", reply.final_answer)


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