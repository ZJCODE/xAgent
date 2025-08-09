import redis.asyncio as redis
from typing import List, Optional, Final
from xagent.schemas import Message

import os
import asyncio
import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)


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
    MSG_PREFIX: Final[str] = "chat"

    def __init__(self, redis_url: str = None, *, sanitize_keys: bool = False):
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
        self.r: Optional[redis.Redis] = None
        self._loop_id: Optional[int] = None  # 记录当前客户端绑定的事件循环 id
        self._client_lock = asyncio.Lock()
        self._sanitize_keys = sanitize_keys

    async def _get_client(self):
        """Get or create async Redis client, and rebuild when event loop changes. Thread-safe across coroutines."""
        async with self._client_lock:
            current_loop_id = id(asyncio.get_running_loop())

            # 如果已有客户端但事件循环已变化，则关闭旧客户端并重建
            if self.r is not None and self._loop_id is not None and self._loop_id != current_loop_id:
                try:
                    # 优先使用 aclose（redis>=5.0）
                    close = getattr(self.r, "aclose", None)
                    if callable(close):
                        await close()
                    else:
                        await self.r.close()
                except Exception:
                    pass
                self.r = None
                self._loop_id = None

            if self.r is None:
                try:
                    # 仅保留稳定参数，避免不兼容
                    self.r = redis.Redis.from_url(
                        self.redis_url,
                        decode_responses=True,
                    )
                    await self.r.ping()
                    self._loop_id = current_loop_id
                except Exception as e:
                    raise ConnectionError(f"Failed to connect to Redis at {self.redis_url}: {e}")
            return self.r

    @staticmethod
    def _sanitize_component(value: str) -> str:
        """URL 编码组件，避免分隔符冲突或非法字符。"""
        return quote(value, safe="-._~")

    def _make_key(self, user_id: str, session_id: Optional[str] = None) -> str:
        """
        生成 Redis key。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
        Returns:
            str: Redis key，格式为 'chat:<user_id>' 或 'chat:<user_id>:<session_id>'。
        """
        uid = self._sanitize_component(user_id) if self._sanitize_keys else user_id
        if session_id:
            sid = self._sanitize_component(session_id) if self._sanitize_keys else session_id
            return f"{self.MSG_PREFIX}:{uid}:{sid}"
        return f"{self.MSG_PREFIX}:{uid}"

    async def add_messages(
        self,
        user_id: str,
        messages: Message | List[Message],
        session_id: Optional[str] = None,
        ttl: int = 2592000,
        *,
        max_len: Optional[int] = None,
        reset_ttl: bool = True,
    ):
        """
        向消息历史追加一条或多条消息，并设置过期时间。
        Args:
            user_id (str): 用户 ID。
            messages (Message | List[Message]): 消息对象或消息对象列表。
            session_id (str, optional): 会话 ID。
            ttl (int): 过期时间（秒），默认 30 天。
            max_len (Optional[int]): 若提供，则在追加后裁剪历史到该最大长度。
            reset_ttl (bool): 是否刷新过期时间（滑动过期）。默认 True。
        """
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be a positive integer when provided")
        if max_len is not None and max_len <= 0:
            # 不保留任何历史，相当于只保留即将写入的消息中的最后 0 条 => 直接清空
            # 这里选择抛错而不是清空，避免误操作
            raise ValueError("max_len must be a positive integer when provided")

        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        if not isinstance(messages, list):
            messages = [messages]
        if not messages:
            return

        pipe = client.pipeline(transaction=True)
        try:
            pipe.rpush(key, *(m.model_dump_json() for m in messages))
            if max_len is not None:
                pipe.ltrim(key, -max_len, -1)
            if reset_ttl and ttl is not None:
                pipe.expire(key, ttl)
            await pipe.execute()
        finally:
            try:
                reset = getattr(pipe, "reset", None)
                if callable(reset):
                    maybe_coro = reset()
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
            except Exception:
                pass

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
        if count <= 0:
            return []
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        raw_msgs = await client.lrange(key, -count, -1)
        messages: List[Message] = []
        for i, m in enumerate(raw_msgs):
            try:
                messages.append(Message.model_validate_json(m))
            except Exception as e:
                logger.warning("Skip invalid message at index %d for key %s: %s", i, key, e)
        return messages

    async def trim_history(self, user_id: str, session_id: Optional[str] = None, max_len: int = 200):
        """
        裁剪消息历史，只保留最近 max_len 条。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
            max_len (int): 最大保留条数，默认 200。
        """
        if max_len <= 0:
            # 保护：不接受非正数
            raise ValueError("max_len must be a positive integer")
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
        if ttl <= 0:
            raise ValueError("ttl must be a positive integer")
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
            try:
                close = getattr(self.r, "aclose", None)
                if callable(close):
                    await close()
                else:
                    await self.r.close()
            finally:
                self._loop_id = None
                self.r = None

    async def __aenter__(self):
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()