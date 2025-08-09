import redis.asyncio as redis
from typing import List, Optional
from xagent.schemas import Message

import os
from dotenv import load_dotenv
import asyncio

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
        """
        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            raise ValueError("REDIS_URL not set in environment or not provided as argument")
        self.redis_url = url
        self.r = None
        self._loop_id: Optional[int] = None  # 记录当前客户端绑定的事件循环 id

    async def _get_client(self):
        """Get or create async Redis client, and rebuild when event loop changes."""
        current_loop_id = id(asyncio.get_running_loop())

        # 如果已有客户端但事件循环已变化，则关闭旧客户端并重建
        if self.r is not None and self._loop_id is not None and self._loop_id != current_loop_id:
            try:
                await self.r.close()
            except Exception:
                pass
            self.r = None
            self._loop_id = None

        if self.r is None:
            try:
                self.r = redis.Redis.from_url(self.redis_url, decode_responses=True)
                await self.r.ping()
                self._loop_id = current_loop_id
            except Exception as e:
                raise ConnectionError(f"Failed to connect to Redis at {self.redis_url}: {e}")
        return self.r

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

    async def add_messages(self, user_id: str, messages: Message | List[Message], session_id: Optional[str] = None, ttl: int = 2592000):
        """
        向消息历史追加一条或多条消息，并设置过期时间。
        Args:
            user_id (str): 用户 ID。
            messages (Message | List[Message]): 消息对象或消息对象列表。
            session_id (str, optional): 会话 ID。
            ttl (int): 过期时间（秒），默认 30 天。
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        if not isinstance(messages, list):
            messages = [messages]
        if messages:
            await client.rpush(key, *(m.model_dump_json() for m in messages))
            await client.expire(key, ttl)

    async def get_messages(self, user_id: str, session_id: Optional[str] = None, count: int = 20) -> List[Message]:
        """
        获取消息历史，按倒序获取最近 count 条。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
            count (int): 获取条数，默认 20。
        Returns:
            List[Message]: 消息对象列表，按时间正序排列。
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        raw_msgs = await client.lrange(key, -count, -1)
        return [Message.model_validate_json(m) for m in raw_msgs]

    async def trim_history(self, user_id: str, session_id: Optional[str] = None, max_len: int = 200):
        """
        裁剪消息历史，只保留最近 max_len 条。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
            max_len (int): 最大保留条数，默认 200。
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        await client.ltrim(key, -max_len, -1)

    async def set_expire(self, user_id: str, session_id: Optional[str] = None, ttl: int = 2592000):
        """
        设置消息历史的过期时间。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
            ttl (int): 过期时间（秒），默认 30 天。
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        await client.expire(key, ttl)

    async def clear_history(self, user_id: str, session_id: Optional[str] = None):
        """
        清空消息历史。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        await client.delete(key)

    async def pop_message(self, user_id: str, session_id: Optional[str] = None) -> Optional[Message]:
        """
        移除并返回最后一条非 tool_result 消息。如果最后一条消息是 tool_result，则自动继续 pop，直到遇到非 tool_result 或为空。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
        Returns:
            Optional[Message]: 被移除的消息对象（非 tool_result），如果没有则返回 None。
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        while True:
            raw_msg = await client.rpop(key)
            if raw_msg is None:
                return None
            msg = Message.model_validate_json(raw_msg)
            if not msg.tool_call:
                return msg

    async def close(self):
        """Close the Redis connection."""
        if self.r:
            await self.r.close()
            self._loop_id = None