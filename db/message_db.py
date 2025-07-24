import redis
from typing import List, Optional
from schemas.messages import Message

import os
from dotenv import load_dotenv

load_dotenv(override=True)

class MessageDB:
    """
    MessageDB
    -------------------
    以Redis为后端存储所有消息历史

    所有消息历史都以统一前缀（chat:）隔离，支持多 session，支持消息裁剪和过期。
    主要功能：
    - 按用户/会话存储消息历史
    - 支持消息追加、获取、裁剪、设置过期
    - Redis key 统一封装，便于维护
    """
    MSG_PREFIX: str = "chat"

    def __init__(self, redis_url: str = None):
        """
        初始化 MessageDB 实例，连接 Redis。
        Args:
            redis_url (str, optional): Redis 连接 URL。优先使用参数，否则读取环境变量 REDIS_URL。
        Raises:
            ValueError: 未提供 Redis 连接信息。
            ConnectionError: Redis 连接失败。
        """
        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            raise ValueError("REDIS_URL not set in environment or not provided as argument")
        try:
            self.r = redis.StrictRedis.from_url(url, decode_responses=True)
            self.r.ping()
        except Exception as e:
            raise ConnectionError(f"Failed to connect to Redis at {url}: {e}")

    def _make_key(self, user_id: str, session_id: Optional[str] = None) -> str:
        """
        生成 Redis key。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
        Returns:
            str: Redis key，格式为 'chat:<user_id>' 或 'chat:<user_id>:<session_id>'。
        """
        if session_id:
            return f"{self.MSG_PREFIX}:{user_id}:{session_id}"
        return f"{self.MSG_PREFIX}:{user_id}"

    def add_message(self, user_id: str, message: Message, session_id: Optional[str] = None, ttl: int = 2592000):
        """
        向消息历史追加一条消息，并设置过期时间。
        Args:
            user_id (str): 用户 ID。
            message (Message): 消息对象。
            session_id (str, optional): 会话 ID。
            ttl (int): 过期时间（秒），默认 30 天。
        """
        key = self._make_key(user_id, session_id)
        self.r.rpush(key, message.model_dump_json())
        self.r.expire(key, ttl)

    def get_messages(self, user_id: str, session_id: Optional[str] = None, count: int = 20) -> List[Message]:
        """
        获取消息历史，按倒序获取最近 count 条。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
            count (int): 获取条数，默认 20。
        Returns:
            List[Message]: 消息对象列表，按时间正序排列。
        """
        key = self._make_key(user_id, session_id)
        raw_msgs = self.r.lrange(key, -count, -1)
        return [Message.model_validate_json(m) for m in raw_msgs]

    def trim_history(self, user_id: str, session_id: Optional[str] = None, max_len: int = 200):
        """
        裁剪消息历史，只保留最近 max_len 条。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
            max_len (int): 最大保留条数，默认 200。
        """
        key = self._make_key(user_id, session_id)
        self.r.ltrim(key, -max_len, -1)

    def set_expire(self, user_id: str, session_id: Optional[str] = None, ttl: int = 2592000):
        """
        设置消息历史的过期时间。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
            ttl (int): 过期时间（秒），默认 30 天。
        """
        key = self._make_key(user_id, session_id)
        self.r.expire(key, ttl)

    def clear_history(self, user_id: str, session_id: Optional[str] = None):
        """
        清空消息历史。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
        """
        key = self._make_key(user_id, session_id)
        self.r.delete(key)

    def pop_message(self, user_id: str, session_id: Optional[str] = None) -> Optional[Message]:
        """
        移除并返回最后一条非 tool_result 消息。如果最后一条消息是 tool_result，则自动继续 pop，直到遇到非 tool_result 或为空。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
        Returns:
            Optional[Message]: 被移除的消息对象（非 tool_result），如果没有则返回 None。
        """
        key = self._make_key(user_id, session_id)
        while True:
            raw_msg = self.r.rpop(key)
            if raw_msg is None:
                return None
            msg = Message.model_validate_json(raw_msg)
            if not msg.is_tool_result:
                return msg