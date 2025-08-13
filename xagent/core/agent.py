import time
from typing import Optional, AsyncGenerator, Union
import json
import logging
import asyncio
import uuid
from enum import Enum
from dotenv import load_dotenv
from pydantic import BaseModel
import httpx
from typing import List


# 日志系统初始化（只需一次）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

from langfuse import observe
from langfuse.openai import AsyncOpenAI

from ..schemas import Message,ToolCall
from ..db import MessageDB
from ..core import Session
from ..utils.tool_decorator import function_tool
from ..utils.mcp_convertor import MCPTool

load_dotenv(override=True)

class ReplyType(Enum):
    SIMPLE_REPLY = "simple_reply"
    STRUCTURED_REPLY = "structured_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"

class Agent:
    """
    基础 Agent 类，支持与 OpenAI 模型交互。
    """

    DEFAULT_NAME = "default_agent"
    DEFAULT_MODEL = "gpt-4.1-mini"

    DEFAULT_SYSTEM_PROMPT = (
        "**Context Information:**\n"
        "- Current user_id: {user_id}\n"
        "- Current date: {date}\n" 
        "- Current timezone: {timezone}\n\n"
        "**Basic Capabilities:**\n" \
        "- Respond directly when you can answer a question without tools\n"
        "- Use available tools when specialized functionality is needed\n"
        "- Handle multi-step reasoning and break down complex problems\n"
        "- Be concise yet informative in your responses\n"
        "- When uncertain, ask clarifying questions\n"
        "- Acknowledge when a request is beyond your capabilities\n\n\n"
    )


    def __init__(
        self, 
        name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        description: Optional[str] = None, # simple description of the agent for tool conversion , what the Agent does
        model: Optional[str] = None,
        client: Optional[AsyncOpenAI] = None,
        tools: Optional[list] = None,
        mcp_servers: Optional[Union[str, list]] = None,
        sub_agents: Optional[List[Union[tuple[str, str, str], 'Agent']]] = None # (name, description, server_url) or Agent instances
    ):
        self.name: str = name or self.DEFAULT_NAME
        self.system_prompt: str = self.DEFAULT_SYSTEM_PROMPT + (system_prompt or "")
        self.description: str = description
        self.model: str = model or self.DEFAULT_MODEL
        self.client: AsyncOpenAI = client or AsyncOpenAI()
        self.tools: dict = {}
        self._register_tools(tools or [])
        self.mcp_servers: list = mcp_servers or []
        self.mcp_tools: dict = {}
        self.mcp_tools_last_updated: Optional[float] = None
        self.mcp_cache_ttl: int = 300  # 5 minutes
        self.agent_tools = self._convert_sub_agents_to_tools(sub_agents)
        self._register_tools(self.agent_tools)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info("sub_agents: %s",sub_agents)
        if self.agent_tools:
            self.logger.info("Registered agent tools: %s", [tool.tool_spec['name'] for tool in self.agent_tools])

    async def __call__(
            self,
            user_message: Message | str,
            session: Session,
            history_count: int = 16,
            max_iter: int = 5,
            image_source: Optional[str] = None,
            output_type: type[BaseModel] = None,
            stream=False
    ) -> str | BaseModel | AsyncGenerator[str, None]:
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
        return await self.chat(
            user_message=user_message,
            session=session,
            history_count=history_count,
            max_iter=max_iter,
            image_source=image_source,
            output_type=output_type,
            stream=stream
        )

    @observe()
    async def chat(
        self,
        user_message: Message | str,
        session: Session,
        history_count: int = 16,
        max_iter: int = 10,
        image_source: Optional[str] = None,
        output_type: type[BaseModel] = None,
        stream=False
    ) -> str | BaseModel | AsyncGenerator[str, None]:
        """
        Generate a reply from the agent given a user message and session.

        Args:
            user_message (Message | str): The latest user message.
            session (Session): The session object managing message history.
            history_count (int, optional): Number of previous messages to include. Defaults to 20.
            max_iter (int, optional): Maximum model call attempts. Defaults to 10.
            image_source (Optional[str], optional): Source of the image, if any can be a URL or File path or base64 string.
            output_type (type[BaseModel], optional): Pydantic model for structured output.
            stream (bool, optional): Whether to stream the response. Defaults to False.

        Returns:
            str | BaseModel: The agent's reply or error message.
        """

        try:
            # Register tools and MCP servers in each chat call to make sure they are up-to-date
            await self._register_mcp_servers(self.mcp_servers)

            # Store the incoming user message in session history
            await self._store_user_message(user_message, session, image_source)

            # Build input messages once outside the loop
            input_messages = [msg.to_dict() for msg in await session.get_messages(history_count)]

            for attempt in range(max_iter):

                reply_type, response = await self._call_model(input_messages, session, output_type,stream)

                if reply_type == ReplyType.SIMPLE_REPLY:
                    if not stream:
                        await self._store_model_reply(str(response), session)
                    return response
                elif reply_type == ReplyType.STRUCTURED_REPLY:
                    await self._store_model_reply(response.model_dump_json(), session)
                    return response
                elif reply_type == ReplyType.TOOL_CALL:
                    await self._handle_tool_calls(response, session, input_messages)
                else:
                    self.logger.error("Unknown reply type: %s", reply_type)
                    return "Sorry, I encountered an error while processing your request."

            # If no valid reply after max_iter attempts
            self.logger.error("Failed to generate response after %d attempts", max_iter)
            return "Sorry, I could not generate a response after multiple attempts."

        except Exception as e:
            self.logger.exception("Agent chat error: %s", e)
            return "Sorry, something went wrong."

    def as_tool(self,name: str = None, description: str = None,message_db: Optional[MessageDB] = None):
        """
        将 Agent 实例转换为 OpenAI 工具函数。
        """
        @function_tool(name=name or self.name, description=description or self.description)
        async def tool_func(simple_input: str, shared_context: Optional[str] = None, image_source: Optional[str] = None):
            return await self.chat(user_message=f"### Shared Context:\n{shared_context}\n\n### User Input:\n{simple_input}" if shared_context else simple_input,
                                   image_source=image_source,
                                   session=Session(user_id=f"agent_{self.name}_as_tool", 
                                                   session_id=f"{uuid.uuid4()}",
                                                    message_db=message_db
                                                   ))
        return tool_func

    def _convert_sub_agents_to_tools(self, sub_agents: Optional[List[Union[tuple[str, str, str], 'Agent']]]) -> Optional[list]:
        """
        将子 Agent 列表转换为工具函数列表。
        """
        tools = []
        for item in sub_agents or []:
            if isinstance(item, tuple) and len(item) == 3:
                name, description, server = item
                tool = self._convert_http_agent_to_tool(server=server, name=name, description=description)
                tools.append(tool)
            elif isinstance(item, Agent):
                tool = item.as_tool()
                tools.append(tool)
            else:
                self.logger.warning(f"Invalid sub_agent type: {type(item)}. Must be tuple[name, description, server_url] or Agent instance.")
        return tools if tools else None

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
    async def _register_mcp_servers(self, mcp_servers: Optional[Union[str, list]]) -> None:
        """
        注册 MCP 服务器地址。
        """
        now = time.time()
        if self.mcp_tools_last_updated and (now - self.mcp_tools_last_updated) < self.mcp_cache_ttl:
            return

        self.mcp_tools = {}
        if isinstance(mcp_servers, str):
            mcp_servers = [mcp_servers]
        for url in mcp_servers or []:
            try:
                mt = MCPTool(url)
                mcp_tools = await mt.get_openai_tools()
                for tool in mcp_tools:
                    if tool.tool_spec['name'] not in self.mcp_tools:
                        self.mcp_tools[tool.tool_spec['name']] = tool
            except Exception as e:
                self.logger.error(f"Failed to get tools from MCP server {url}: {e}")
                # If one server fails, we should probably not update the timestamp
                # to try again on the next call. But for now, we continue with other servers.
                continue
        
        self.mcp_tools_last_updated = now

    async def _store_user_message(self, user_message: Message | str, session: Session, image_source: Optional[str]) -> None:
        if isinstance(user_message, str):
            user_message = Message.create(content=user_message, role="user", image_source=image_source)
        await session.add_messages(user_message)

    async def _store_model_reply(self, reply_text: str, session: Session) -> None:
        model_msg = Message.create(content=reply_text, role="assistant")
        await session.add_messages(model_msg)

    @observe()
    async def _call_model(self, input_msgs: list, session: Session, 
                          output_type: type[BaseModel] = None, stream: bool = False) -> tuple[ReplyType, object]:
        """
        调用大模型，返回响应对象或 None。
        """
        system_msg = {
            "role": "system",
            "content": self.system_prompt.format(
                user_id=session.user_id, 
                date=time.strftime('%Y-%m-%d'), 
                timezone=time.tzname[0]
            )
        }

        # 预先构建工具规格列表，避免重复计算
        all_tools = list(self.tools.values()) + list(self.mcp_tools.values())
        tool_specs = [fn.tool_spec for fn in all_tools] if all_tools else None
        
        # 预处理消息
        messages = [system_msg] + self._sanitize_input_messages(input_msgs)

        try:
            # 根据是否需要结构化输出选择不同的API调用,结构化输出强制不使用Stream 模式
            if output_type is not None:
                response = await self.client.responses.parse(
                    model=self.model,
                    tools=tool_specs,
                    input=messages,
                    text_format=output_type
                )
                # 检查结构化输出
                if hasattr(response, "output_parsed") and response.output_parsed is not None:
                    return ReplyType.STRUCTURED_REPLY, response.output_parsed
            else:
                response = await self.client.responses.create(
                    model=self.model,
                    tools=tool_specs,
                    input=messages,
                    stream=stream
                )

            # 统一处理响应，按优先级检查不同类型的输出
            if not stream:
                if hasattr(response, 'output_text') and response.output_text:
                    return ReplyType.SIMPLE_REPLY, response.output_text
                
                if hasattr(response, 'output') and response.output:
                    return ReplyType.TOOL_CALL, response.output
                
                # 如果没有有效输出，记录警告并返回错误
                self.logger.warning("Model response contains no valid output: %s", response)
                return ReplyType.ERROR, "No valid output from model response."
            else:
                
                # Get the third event to determine the stream type
                await anext(response, None)  # Skip first event
                await anext(response, None)  # Skip second event
                third_event = await anext(response, None)
                type = third_event.item.type if third_event else None

                if type == "message":
                    async def stream_generator():
                        async for event in response:
                            if event.type == 'response.output_text.delta':
                                content = event.delta
                                if content:
                                    yield content
                        await self._store_model_reply(event.response.output[0].content[0].text, session)
                    return ReplyType.SIMPLE_REPLY, stream_generator()
                elif type == "function_call":
                    async for event in response:
                        pass 
                    return ReplyType.TOOL_CALL, event.response.output
                else:
                    self.logger.warning("Stream response type is not recognized: %s", type)
                    async def stream_generator():
                        yield "Stream response type is not recognized."
                    # 返回一个生成器，避免直接返回错误信息
                    return ReplyType.ERROR, stream_generator()
                    
        except Exception as e:
            self.logger.exception("Model call failed: %s", e)
            if stream:
                async def stream_generator():
                    yield f"Model call error: {str(e)}"
                return ReplyType.ERROR, stream_generator()
            return ReplyType.ERROR, f"Model call error: {str(e)}"
    
    @observe()
    async def _handle_tool_calls(self, tool_calls: list, session: Session, input_messages: list) -> None:
        """
        异步并发处理所有 tool_call，返回特殊结果（如图片）或 None。
        """

        if tool_calls is None or not tool_calls:
            return None

        tasks = [self._act(tc, session) for tc in tool_calls if getattr(tc, "type", None) == "function_call"]
        results = await asyncio.gather(*tasks)
        # Safely add all tool messages after concurrent execution
        for tool_messages in results:
            if tool_messages:
                input_messages.extend([msg.to_dict() for msg in tool_messages])
        return None

    async def _act(self, tool_call, session: Session) -> Optional[list]:
        """
        异步执行工具函数调用，并将结果写入 session。
        返回工具调用和结果消息的列表。
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
            await session.add_messages([tool_call_msg, tool_res_msg])
            
            # Return the messages instead of modifying input_messages directly
            return [tool_call_msg, tool_res_msg]

        return None

    @staticmethod
    def _sanitize_input_messages(input_messages: list) -> list:
        """
        清理输入消息列表，确保其不以 'function_call_output' 类型的消息开头。
        """
        while input_messages and input_messages[0].get("type") == "function_call_output":
            input_messages.pop(0)
        return input_messages
    

    def _convert_http_agent_to_tool(self, server: str, name: str = None, description: str = None):
        """
        将 HTTP Agent 转换为 OpenAI 工具函数。
        
        Args:
            server: HTTP Agent 服务器地址，例如 "http://localhost:8011"
            name: 工具名称
            description: 工具描述
        """
        @function_tool(name=name, description=description)
        async def tool_func(simple_input: str, shared_context: Optional[str] = None, image_source: Optional[str] = None):
            """
            通过 HTTP 请求调用 Agent 的 chat 方法。
            
            Args:
                input: 用户输入消息
                image_source: 可选的图片源（URL、文件路径或base64字符串）
            """
            # 构建请求体，遵循 AgentInput 模型格式
            request_body = {
                "user_id": f"http_tool_{uuid.uuid4().hex[:8]}",
                "session_id": f"session_{uuid.uuid4().hex[:8]}",
                "user_message": f"### Shared Context:\n{shared_context}\n\n### User Input:\n{simple_input}" if shared_context else simple_input,
                "stream": False  # 工具调用不使用流式响应
            }
            
            # 如果提供了图片源，添加到请求体中
            if image_source:
                request_body["image_source"] = image_source
            
            # 构建完整的请求URL
            chat_url = f"{server}/chat"
            
            # 添加详细日志
            self.logger.info(f"Calling HTTP Agent at: {chat_url}")
            self.logger.debug(f"Request body: {request_body}")

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:  # 增加超时时间
                    response = await client.post(chat_url, json=request_body)
                    
                    self.logger.debug(f"HTTP response status: {response.status_code}")
                    
                    if response.status_code == 200:
                        try:
                            response_data = response.json()
                            reply = response_data.get("reply", "")
                            
                            if reply:
                                self.logger.info(f"HTTP Agent '{name}' responded successfully")
                                return reply
                            else:
                                error_msg = "Empty reply from HTTP Agent"
                                self.logger.warning(error_msg)
                                return error_msg
                                
                        except json.JSONDecodeError as e:
                            error_msg = f"Invalid JSON response from HTTP Agent: {str(e)}"
                            self.logger.error(f"{error_msg}. Response text: {response.text[:200]}...")
                            return error_msg
                            
                    elif response.status_code == 500:
                        # 处理服务器内部错误
                        try:
                            error_data = response.json()
                            error_detail = error_data.get("detail", "Internal server error")
                            error_msg = f"HTTP Agent internal error: {error_detail}"
                        except:
                            error_msg = f"HTTP Agent internal error: {response.text[:200]}"
                        
                        self.logger.error(error_msg)
                        return error_msg
                        
                    else:
                        # 处理其他HTTP错误
                        error_msg = f"HTTP Agent error {response.status_code}: {response.text[:200]}"
                        self.logger.error(error_msg)
                        return error_msg
                        
            except httpx.ConnectError as e:
                error_msg = f"Cannot connect to HTTP Agent at {chat_url}: {str(e)}"
                self.logger.error(error_msg)
                return error_msg
                
            except httpx.TimeoutException as e:
                error_msg = f"HTTP Agent request timeout: {str(e)}"
                self.logger.error(error_msg)
                return error_msg
                
            except httpx.RequestError as e:
                error_msg = f"HTTP Agent request error: {str(e)}"
                self.logger.error(error_msg)
                return error_msg
                
            except Exception as e:
                error_msg = f"Unexpected error calling HTTP Agent: {str(e)}"
                self.logger.exception(error_msg)
                return error_msg
        
        return tool_func