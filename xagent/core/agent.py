import time
from typing import Optional
import json
import logging
import asyncio
import uuid
from dotenv import load_dotenv
from pydantic import BaseModel


# 日志系统初始化（只需一次）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

from langfuse import observe
from langfuse.openai import AsyncOpenAI

from xagent.schemas import Message,ToolCall
from xagent.db import MessageDB
from xagent.core import Session
from xagent.utils.tool_decorator import function_tool
from xagent.utils.mcp_convertor import MCPTool

load_dotenv(override=True)

class Agent:
    """
    基础 Agent 类，支持与 OpenAI 模型交互。
    """

    DEFAULT_NAME = "default_agent"
    DEFAULT_MODEL = "gpt-4.1-mini"
    DEFAULT_SYSTEM_PROMPT = "**Current user_id**: {user_id}, **Current date**: {date}, **Current timezone**: {timezone}\n"

    REPLY_TOOL_NAME = "ready_to_reply"
    NEED_MORE_INFO_TOOL_NAME = "need_more_info"
    ANSWER_ACTION = "answer"

    def __init__(
        self, 
        name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[AsyncOpenAI] = None,
        tools: Optional[list] = None,
        mcp_servers: Optional[str | list] = None
    ):
        self.name: str = name or self.DEFAULT_NAME
        self.system_prompt: str = self.DEFAULT_SYSTEM_PROMPT + (system_prompt or "")
        self.model: str = model or self.DEFAULT_MODEL
        self.client: AsyncOpenAI = client or AsyncOpenAI()
        self.tools: dict = {}
        self._register_tools(self._default_tools + (tools or []))
        self.mcp_servers: list = mcp_servers or []
        self.mcp_tools: dict = {}
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
            # Register tools and MCP servers in each chat call to make sure they are up-to-date
            await self._register_mcp_servers(self.mcp_servers)

            # Store the incoming user message in session history
            self._store_user_message(user_message, session, image_source)

            # Build input messages once outside the loop
            input_messages = self._build_input_messages(session, history_count)

            for attempt in range(max_iter):
                # Call choose tools to see if any tool calls are needed
                tool_calls = await self._choose_tools(input_messages)

                # Handle any tool calls in the response
                tool_call_result = await self._handle_tool_calls(tool_calls, session, input_messages)

                if tool_call_result == self.ANSWER_ACTION:
                    response = await self._call_model(input_messages, output_type)

                    if response:
                        response_str = response.model_dump_json() if output_type else str(response)
                        self._store_model_reply(response_str, session)
                        return response

            # If no valid reply after max_iter attempts
            self.logger.error("Failed to generate response after %d attempts", max_iter)
            return "Sorry, I could not generate a response after multiple attempts."

        except Exception as e:
            self.logger.exception("Agent chat error: %s", e)
            return "Sorry, something went wrong."

    @property
    def _default_tools(self) -> list:

        @function_tool(name=self.REPLY_TOOL_NAME)
        def ready_to_reply() -> str:
            """When Agent is ready to reply to user, use this tool."""
            return "Ready to reply!"

        @function_tool(name=self.NEED_MORE_INFO_TOOL_NAME)
        def need_more_info() -> str:
            """When Agent needs more information to answer, use this tool."""
            return "I need more information to answer your question."

        return [ready_to_reply, need_more_info]

    def _register_tools(self, tools: Optional[list]) -> None:
        """
        注册工具函数，确保每个工具是异步的且唯一。
        """
        for fn in tools or []:
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(f"Tool function '{fn.tool_spec['name']}' must be async.")
            if fn.tool_spec['name'] not in self.tools:
                self.tools[fn.tool_spec['name']] = fn

    @observe()
    async def _register_mcp_servers(self, mcp_servers: Optional[str | list]) -> None:
        """
        注册 MCP 服务器地址。
        """
        self.mcp_tools = {}
        if isinstance(mcp_servers, str):
            mcp_servers = [mcp_servers]
        for url in mcp_servers or []:
            mt = MCPTool(url)
            mcp_tools = await mt.get_openai_tools()
            for tool in mcp_tools:
                if tool.tool_spec['name'] not in self.mcp_tools:
                    self.mcp_tools[tool.tool_spec['name']] = tool

    def _store_user_message(self, user_message: Message | str, session: Session, image_source: Optional[str]) -> None:
        if isinstance(user_message, str):
            user_message = Message.create(content=user_message, role="user", image_source=image_source)
        session.add_messages(user_message)

    def _store_model_reply(self, reply_text: str, session: Session) -> None:
        model_msg = Message.create(content=reply_text, role="assistant")
        session.add_messages(model_msg)

    @observe()
    def _build_input_messages(self, session: Session, history_count: int) -> list:
        """
        构造输入给大模型的消息列表。
        """
        # Sytem Message
        system_msg = {
            "role": "system",
            "content": self.system_prompt.format(user_id= session.user_id, date=time.strftime('%Y-%m-%d'), timezone=time.tzname[0])
        }
        # User History Messages
        history_msgs = []
        for msg in session.get_messages(history_count):
            history_msgs.append(msg.to_dict())
        return [system_msg] + history_msgs
    
    @observe()
    async def _choose_tools(self, input_msgs: list) -> Optional[list]:
        try:
            response = await self.client.responses.create(
                    model=self.model,
                    tools=[fn.tool_spec for fn in list(self.tools.values()) + list(self.mcp_tools.values())],
                    input=input_msgs,
                    tool_choice ="required",
            )
            return response.output
        except Exception as e:
            self.logger.error("Tool selection failed: %s", e)
            return None

    @observe()
    async def _call_model(self, input_msgs: list, output_type: type[BaseModel] = None) -> Optional[object]:
        """
        调用大模型，返回响应对象或 None。
        """
        try:
            if output_type is not None:
                response =  await self.client.responses.parse(
                    model=self.model,
                    input=input_msgs,
                    text_format=output_type
                )
                return response.output_parsed
            else:
                response =  await self.client.responses.create(
                    model=self.model,
                    input=input_msgs
                )
                return response.output_text
        except Exception as e:
            self.logger.error("Model call failed: %s", e)
            return None

    async def _handle_tool_calls(self, tool_calls: list, session: Session, input_messages: list) -> Optional[str]:
        """
        异步并发处理所有 tool_call，返回特殊结果（如图片）或 None。
        """

        if tool_calls is None or not tool_calls:
            return None

        tool_names = [tc.name for tc in tool_calls]
        # Execute in order if specific tools are present
        if self.REPLY_TOOL_NAME in tool_names or self.NEED_MORE_INFO_TOOL_NAME in tool_names:
            for tc in tool_calls:
                if getattr(tc, "type", None) == "function_call":
                    await self._act(tc, session, input_messages)
                    if tc.name in [self.REPLY_TOOL_NAME, self.NEED_MORE_INFO_TOOL_NAME]:
                        return self.ANSWER_ACTION
        # Otherwise, execute all tool calls concurrently
        tasks = [self._act(tc, session, input_messages) for tc in tool_calls if getattr(tc, "type", None) == "function_call"]
        await asyncio.gather(*tasks)
        return None

    async def _act(self, tool_call, session: Session, input_messages: list) -> Optional[str]:
        """
        异步执行工具函数调用，并将结果写入 session。
        """
        name = getattr(tool_call, "name", None)
        try:
            args = json.loads(getattr(tool_call, "arguments", "{}"))
        except Exception as e:
            self.logger.error("Tool args parse error: %s", e)
            return None
        func = self.tools.get(name) or self.mcp_tools.get(name)
        if func:
            self.logger.info("Calling tool: %s with args: %s", name, args)

            try:
                result = await func(**args)
            except Exception as e:
                self.logger.error("Tool call error: %s", e)
                result = f"Tool error: {e}"

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

            tool_res_msg = Message(
                type="function_call_output",
                role="tool",
                content=f"Tool `{name}` result: {str(result) if len(str(result)) < 20 else str(result)[:20] + '...'}",
                tool_call=ToolCall(
                    call_id=getattr(tool_call, "call_id", "001"),
                    output=json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
                )
            )
            session.add_messages([tool_call_msg, tool_res_msg])
            
            # Append tool messages to input_messages to avoid rebuilding
            input_messages.extend([tool_call_msg.to_dict(), tool_res_msg.to_dict()])

        return None

    def as_tool(self,name: str = None, description: str = None,message_db: Optional[MessageDB] = None):
        """
        将 Agent 实例转换为 OpenAI 工具函数。
        """
        @function_tool(name=name or self.name, description=description or self.system_prompt)
        async def tool_func(message: str):
            return await self.chat(user_message=message, 
                                   session=Session(user_id=f"agent_{self.name}_as_tool", 
                                                   session_id=f"{uuid.uuid4()}",
                                                    message_db=message_db
                                                   ))
        return tool_func
