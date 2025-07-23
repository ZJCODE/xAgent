import time
from typing import List, Optional
import json

from langfuse import observe
from langfuse.openai import OpenAI
from schemas.messages import Message
from db.message_db import MessageDB
from utils.tool_decorator import function_tool

from dotenv import load_dotenv


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
        # 本地消息用 (user_id, session_id) 区分
        key = (user_id, session_id)
        if not self.message_db and key not in Session._local_messages:
            Session._local_messages[key] = []

    def add_message(self, message: Message):
        if self.message_db:
            self.message_db.add_message(self.user_id, message, self.session_id)
        else:
            key = (self.user_id, self.session_id)
            Session._local_messages[key].append(message)

    def get_history(self, count: int = 20) -> List[Message]:
        if self.message_db:
            return self.message_db.get_messages(self.user_id, self.session_id, count)
        key = (self.user_id, self.session_id)
        return Session._local_messages[key][-count:]
    
    def clear_history(self):
        if self.message_db:
            self.message_db.clear_history(self.user_id, self.session_id)
        else:
            key = (self.user_id, self.session_id)
            Session._local_messages[key] = []

class Agent:
    """
    负责与大模型交互，生成回复。
    """

    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, 
                 model: str = DEFAULT_MODEL,
                 client: OpenAI = None,
                 tools: Optional[list] = None):
        self.model = model
        self.client = client or OpenAI()
        # tools: list[callable], e.g. [add, ...]
        tool_fns = tools or [add]
        self.tools = {fn.__name__: fn for fn in tool_fns}

    @observe()
    def chat(
        self,
        user_message: Message | str,
        session: Session,
        history_count: int = 20
    ) -> str:
        """
        只需传入用户最新消息（Message 或 str）和 session。
        自动更新 session，生成回复并存储。
        Args:
            user_message (Message or str): 用户最新消息。
            session (Session): 会话对象。
            history_count (int): 获取历史条数，默认 20。
        Returns:
            str: 大模型回复内容。
        """
        if isinstance(user_message, str):
            user_message = Message(role="user", content=user_message, timestamp=time.time())
        session.add_message(user_message)

        reply = None

        while not reply:

            history = session.get_history(history_count)
            input_msgs = [
                {"role": "system", "content": f"**Current user_id**: {session.user_id} \n**Current time**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"}
            ]
            input_msgs.extend(
                {"role": msg.role, "content": msg.content} for msg in history
            )
            response = self.client.responses.create(
                model=self.model,
                tools=[{"type": "web_search_preview"}] + [fn.__tool_spec__ for fn in self.tools.values()],
                input=input_msgs
            )
            reply = response.output_text

            tool_calls = response.output
            for tool_call in tool_calls:
                history_count += 1  # 增加历史条数以弥补Tool消息的占用
                if tool_call.type != "function_call":
                    continue
                name = tool_call.name
                args = json.loads(tool_call.arguments)
                func = self.tools.get(name)
                if func:
                    result = func(**args)
                    tool_msg = Message(role="assistant", content=f"tool call result from tool `{name}` is: {result}", timestamp=time.time(), is_tool_result=True)
                    session.add_message(tool_msg)

        model_msg = Message(role="assistant", content=reply, timestamp=time.time())
        session.add_message(model_msg)
        return reply
    

if __name__ == "__main__":


    # Simple Test Example

    @function_tool()
    def add(a: int, b: int) -> int:
        "Add two numbers."
        return a + b

    agent = Agent(tools=[add])
    session = Session(user_id="user1")
    session.clear_history()  # 清空历史以便测试
    user_msg = "the answer for 12 + 13 is"
    print("User:", user_msg)
    reply = agent.chat(user_msg, session)
    print("Agent:", reply)
    user_msg = "The Weather in Hangzhou is"
    print("User:", user_msg)
    reply = agent.chat(user_msg, session)
    print("Agent:", reply)
    user_msg = "Can you explain aesthetics?"
    print("User:", user_msg)
    reply = agent.chat(user_msg, session)
    print("Agent:", reply)
    print("Session history:")
    for msg in session.get_history():
        print(f"{msg.role}: {msg.content} (at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.timestamp))})")

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