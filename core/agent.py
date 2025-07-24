import time
from typing import List, Optional
import json
import logging  # 新增
from langfuse import observe
from langfuse.openai import OpenAI
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
    负责与大模型交互，生成回复。
    """

    DEFAULT_MODEL = "gpt-4o-mini"
    DEFAULT_SYSTEM_PROMPT = "**Current user_id**: {user_id}"

    def __init__(self, 
                 model: Optional[str] = None,
                 system_prompt: Optional[str] = None,
                 client: Optional[OpenAI] = None,
                 tools: Optional[list] = None,
                 ):
        
        self.model = model or self.DEFAULT_MODEL
        self.system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self.client = client or OpenAI()
        # 工具函数去重
        tool_fns = tools or []
        self.tools = {}
        for fn in tool_fns:
            if fn.__name__ not in self.tools:
                self.tools[fn.__name__] = fn
        self.logger = logging.getLogger(self.__class__.__name__)

    @observe()
    def chat(
        self,
        user_message: Message | str,
        session: Session,
        history_count: int = 20,
        max_iter: int = 5
    ) -> str:
        """
        只需传入用户最新消息（Message 或 str）和 session。
        自动更新 session，生成回复并存储。
        Args:
            user_message (Message or str): 用户最新消息。
            session (Session): 会话对象。
            history_count (int): 获取历史条数，默认 20。
            max_iter (int): 最大迭代次数，默认 5。
        Returns:
            str: 大模型回复内容
        """
        try:
            if isinstance(user_message, str):
                user_message = Message(role="user", content=user_message, timestamp=time.time())
            session.add_message(user_message)

            reply = None
            iter_count = 0

            while not reply and iter_count < max_iter:
                iter_count += 1

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
                try:
                    response = self.client.responses.create(
                        model=self.model,
                        tools=[fn.tool_spec for fn in self.tools.values()],
                        input=input_msgs
                    )
                except Exception as e:
                    self.logger.error("Model call failed: %s", e)
                    break

                reply = response.output_text

                tool_calls = response.output
                for tool_call in tool_calls:
                    history_count += 1
                    if tool_call.type == "function_call":
                        name = tool_call.name
                        try:
                            args = json.loads(tool_call.arguments)
                        except Exception as e:
                            self.logger.error("Tool args parse error: %s", e)
                            continue
                        func = self.tools.get(name)
                        if func:
                            self.logger.info("Calling tool: %s with args: %s", name, args)
                            try:
                                result = func(**args)
                            except Exception as e:
                                self.logger.error("Tool call error: %s", e)
                                result = f"Tool error: {e}"
                            # Temp add extra logic for image generation
                            if name == "draw_image" and isinstance(result, str) and result.startswith("![generated image](data:image/png;base64,"):
                                image_msg = Message(
                                    role="assistant",
                                    content=f"just generated an image with prompt `{args['prompt']}`",
                                    timestamp=time.time(),
                                    is_tool_result=True
                                )
                                session.add_message(image_msg)
                                return result  # Return early if image generation
                            # END OF TEMP ADD
                            tool_msg = Message(role="assistant", content=f"[tool_call id {tool_call.id}] tool_call result from tool `{name}` with args {args} is: {result}", timestamp=time.time(), is_tool_result=True)
                            session.add_message(tool_msg)

            model_msg = Message(role="assistant", content=reply, timestamp=time.time())
            session.add_message(model_msg)

            return reply
        except Exception as e:
            self.logger.error("Agent chat error: %s", e)
            return "Sorry, something went wrong."
    

if __name__ == "__main__":

    # Simple Test Example

    @function_tool()
    def add(a: int, b: int) -> int:
        "Add two numbers."
        return a + b
    
    from tools.openai_tool import web_search

    agent = Agent(tools=[add, web_search])
    session = Session(user_id="user123123")
    session.clear_history()  # 清空历史以便测试
    # user_msg = "the answer for 12 + 13 is"
    # reply = agent.chat(user_msg, session)
    # user_msg = "the answer for 10 + 20 is and 21 + 22 is"
    # reply = agent.chat(user_msg, session)
    user_msg = "The Weather in Hangzhou is"
    reply = agent.chat(user_msg, session)
    # print("Session history:")
    # for msg in session.get_history():
    #     print(f"{msg.role}: {msg.content} (at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.timestamp))})")

    # # DB Session Example
    # print("DB Session Example:")
    # agent = Agent()
    # session = Session(user_id="user1", session_id="session1", message_db=MessageDB())
    # session.clear_history()  # 清空历史以便测试
    # user_msg = "You can call me Jun."
    # reply = agent.chat(user_msg, session)
    # user_msg = "What time is it now?"
    # reply = agent.chat(user_msg, session)
    # user_msg = "Weather in Hangzhou"
    # reply = agent.chat(user_msg, session)
    # user_msg = "Do you know who I am and what my user ID is?"
    # reply = agent.chat(user_msg, session)
    # print("Session history:")
    # for msg in session.get_history():
    #     print(f"{msg.role}: {msg.content} (at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.timestamp))})")

    # agent2 = Agent()
    # session2 = Session(user_id="user2", session_id="session2", message_db=MessageDB())
    # session2.clear_history()  # 清空历史以便测试
    # user_msg = "Do you know who I am?"
    # reply = agent2.chat(user_msg, session2)
    # print("Session 2 history:")
    # for msg in session2.get_history():
    #     print(f"{msg.role}: {msg.content} (at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.timestamp))})")


    # # Local Session Example
    # print("\nLocal Session Example:")
    # agent = Agent()
    # session = Session(user_id="user1")
    # session.clear_history()  # 清空历史以便测试
    # user_msg = "You can call me Jun."
    # reply = agent.chat(user_msg, session)
    # user_msg = "Hello, how are you?"
    # reply = agent.chat(user_msg, session)
    # user_msg = "Do you know who I am?"
    # reply = agent.chat(user_msg, session)
    # print("Session history:")
    # for msg in session.get_history():
    #     print(f"{msg.role}: {msg.content} (at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.timestamp))})")

    # agent2 = Agent()
    # session2 = Session(user_id="user2")
    # session2.clear_history()  # 清空历史以便测试
    # user_msg = "Do you know who I am?"
    # reply = agent2.chat(user_msg, session2)
    # print("Session 2 history:")
    # for msg in session2.get_history():
    #     print(f"{msg.role}: {msg.content} (at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.timestamp))})")