import time
from typing import List, Optional
import json
import logging
import asyncio

from langfuse import observe
from langfuse.openai import OpenAI,AsyncOpenAI
from schemas.messages import Message
from db.message_db import MessageDB
from utils.tool_decorator import function_tool

from dotenv import load_dotenv

# 日志系统初始化（只需一次）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

load_dotenv(override=True)

class Session:
    """
    管理单个会话的消息历史。
    支持本地内存和 Redis 存储。
    """
    _local_messages = {}  # {(user_id, session_id): [Message, ...]}

    def __init__(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        message_db: Optional[MessageDB] = None
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.message_db = message_db
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{user_id}:{session_id}]")  # 新增
        # 本地消息用 (user_id, session_id) 区分
        key = (user_id, session_id)
        if not self.message_db and key not in Session._local_messages:
            Session._local_messages[key] = []

    def add_message(self, message: Message) -> None:
        try:
            if self.message_db:
                self.logger.info("Adding message to DB: %s", message)
                self.message_db.add_message(self.user_id, message, self.session_id)
            else:
                key = (self.user_id, self.session_id)
                self.logger.info("Adding message to local session: %s", message)
                # 限制本地消息最大长度，防止内存泄漏
                max_local_history = 100
                Session._local_messages[key].append(message)
                if len(Session._local_messages[key]) > max_local_history:
                    Session._local_messages[key] = Session._local_messages[key][-max_local_history:]
        except Exception as e:
            self.logger.error("Failed to add message: %s", e)

    def get_history(self, count: int = 20) -> List[Message]:
        try:
            if self.message_db:
                self.logger.info("Fetching last %d messages from DB", count)
                return self.message_db.get_messages(self.user_id, self.session_id, count)
            key = (self.user_id, self.session_id)
            self.logger.info("Fetching last %d messages from local session", count)
            return Session._local_messages[key][-count:]
        except Exception as e:
            self.logger.error("Failed to get history: %s", e)
            return []

    def clear_history(self) -> None:
        try:
            if self.message_db:
                self.logger.info("Clearing history in DB")
                self.message_db.clear_history(self.user_id, self.session_id)
            else:
                key = (self.user_id, self.session_id)
                self.logger.info("Clearing local session history")
                Session._local_messages[key] = []
        except Exception as e:
            self.logger.error("Failed to clear history: %s", e)

class Agent:
    """
    基础 Agent 类，支持与 OpenAI 模型交互。
    """

    DEFAULT_MODEL = "gpt-4o-mini"
    DEFAULT_SYSTEM_PROMPT = "**Current user_id**: {user_id}"

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
        for fn in tools or []:
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(f"Tool function '{fn.__name__}' must be async.")
            if fn.__name__ not in self.tools:
                self.tools[fn.__name__] = fn
        self.logger = logging.getLogger(self.__class__.__name__)

    def __call__(
            self, 
            user_message: Message | str, 
            session: Session, 
            history_count: int = 20, 
            max_iter: int = 5) -> str:
        """
        支持同步调用 Agent（自动转异步）。
        """
        def execute():
            return asyncio.run(self.chat(user_message, session, history_count, max_iter))
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor() as executor:
            future = executor.submit(execute)
            return future.result()

    @observe()
    async def chat(
        self,
        user_message: Message | str,
        session: Session,
        history_count: int = 20,
        max_iter: int = 5
    ) -> str:
        """
        Generate a reply from the agent given a user message and session.

        Args:
            user_message (Message | str): The latest user message.
            session (Session): The session object managing message history.
            history_count (int, optional): Number of previous messages to include. Defaults to 20.
            max_iter (int, optional): Maximum model call attempts. Defaults to 5.

        Returns:
            str: The agent's reply or error message.
        """
        try:
            # Store the incoming user message in session history
            self._store_user_message(user_message, session)

            for attempt in range(max_iter):
                # Build input messages for the model
                input_messages = self._build_input_messages(session, history_count)

                # Call the model and get response
                response = await self._call_model(input_messages)
                if response is None:
                    self.logger.warning("Model did not respond on attempt %d", attempt + 1)
                    return "Sorry, model did not respond."

                # Handle any tool calls in the response
                special_result = await self._handle_tool_calls(response.output, session)
                if special_result is not None:
                    return special_result

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

    def _store_user_message(self, user_message: Message | str, session: Session) -> None:
        if isinstance(user_message, str):
            user_message = Message(role="user", content=user_message, timestamp=time.time())
        session.add_message(user_message)

    def _store_model_reply(self, reply_text: str, session: Session) -> None:
        model_msg = Message(role="assistant", content=reply_text, timestamp=time.time())
        session.add_message(model_msg)

    def _build_input_messages(self, session: Session, history_count: int) -> list:
        """
        构造输入给大模型的消息列表。
        """
        history = session.get_history(history_count)
        input_msgs = [
            {
                "role": "system",
                "content": self.system_prompt.format(user_id=session.user_id)
            }
        ]
        input_msgs.extend(
            {"role": msg.role, "content": msg.content} for msg in history
        )
        input_msgs.append({
            "role": "assistant",
            "content": f"Current time is {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"
        })
        return input_msgs

    async def _call_model(self, input_msgs: list) -> Optional[object]:
        """
        调用大模型，返回响应对象或 None。
        """
        try:
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
            try:
                result = await func(**args)
            except Exception as e:
                self.logger.error("Tool call error: %s", e)
                result = f"Tool error: {e}"
            # 图片生成特殊处理
            if name == "draw_image" and isinstance(result, str) and result.startswith("![generated image](data:image/png;base64,"):
                image_msg = Message(
                    role="assistant",
                    content=f"just generated an image with prompt `{args.get('prompt', '')}`",
                    timestamp=time.time(),
                    is_tool_result=True
                )
                session.add_message(image_msg)
                return result
            tool_msg = Message(
                role="assistant",
                content=f"[tool_call id {getattr(tool_call, 'id', '')}] tool_call result from tool `{name}` with args {args} is: {result}",
                timestamp=time.time(),
                is_tool_result=True
            )
            session.add_message(tool_msg)
        return None

if __name__ == "__main__":


    @function_tool()
    def add(a: int, b: int) -> int:
        "Add two numbers."
        return a + b

    from tools.openai_tool import web_search

    agent = Agent(tools=[add, web_search])

    # session = Session(user_id="user123123", session_id="test_session", message_db=MessageDB())
    session = Session(user_id="user123")
    session.clear_history()  # 清空历史以便测试

    reply = agent("the answer for 12 + 13 is", session)
    print("Reply:", reply)

    reply = agent("the answer for 10 + 20 is and 21 + 22 is", session)
    print("Reply:", reply)

    reply = agent("The Weather in Hangzhou and Beijing is", session)
    print("Reply:", reply)

    print("Session history:")
    for msg in session.get_history():
        print(f"{msg.role}: {msg.content} (at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.timestamp))})")