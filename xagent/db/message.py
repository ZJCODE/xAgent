import redis.asyncio as redis
from typing import List, Optional, Final

from ..schemas import Message

import os
import logging
from urllib.parse import quote
from redis.exceptions import RedisError

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
    DEFAULT_TTL: Final[int] = 2592000  # 30 days

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
        self._sanitize_keys = sanitize_keys

    async def _get_client(self) -> redis.Redis:
        """Get or create async Redis client with sane defaults."""
        if self.r is None:
            # 采用合理的连接参数，提升稳定性与鲁棒性
            self.r = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                health_check_interval=30,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                client_name="xagent-message-db",
            )
            try:
                await self.r.ping()
            except Exception as e:
                logger.error("Redis initial ping failed: %s", e)
                raise
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
        if self._sanitize_keys:
            user_id = quote(user_id, safe="-._~")
            if session_id:
                session_id = quote(session_id, safe="-._~")
        
        if session_id:
            return f"{self.MSG_PREFIX}:{user_id}:{session_id}"
        return f"{self.MSG_PREFIX}:{user_id}"

    async def add_messages(
        self,
        user_id: str,
        messages: Message | List[Message],
        session_id: Optional[str] = None,
        ttl: int = 2592000,
        *,
        max_len: Optional[int] = None,
        reset_ttl: bool = True,
    ) -> None:
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
            raise ValueError("max_len must be a positive integer when provided")

        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        
        if not isinstance(messages, list):
            messages = [messages]
        if not messages:
            return

        # 使用 pipeline 合并多次往返，并保持逻辑上的原子性顺序
        try:
            async with client.pipeline(transaction=False) as pipe:
                pipe.rpush(key, *(m.model_dump_json() for m in messages))
                if max_len is not None:
                    pipe.ltrim(key, -max_len, -1)
                if reset_ttl and ttl is not None:
                    pipe.expire(key, ttl)
                await pipe.execute()
        except RedisError as e:
            logger.error("Failed to add messages for key %s: %s", key, e)
            raise
        

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
        try:
            raw_msgs = await client.lrange(key, -count, -1)
        except RedisError as e:
            logger.error("Failed to get messages for key %s: %s", key, e)
            raise
        
        messages: List[Message] = []
        for i, m in enumerate(raw_msgs):
            try:
                messages.append(Message.model_validate_json(m))
            except Exception as e:
                # 控制日志尺寸，避免打印超长字符串
                preview = m[:120] + ("..." if len(m) > 120 else "")
                logger.warning(
                    "Skip invalid message at index %d for key %s: %s | payload preview=%r",
                    i, key, e, preview,
                )
        return messages

    async def trim_history(self, user_id: str, session_id: Optional[str] = None, max_len: int = 200) -> None:
        """
        裁剪消息历史，只保留最近 max_len 条。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
            max_len (int): 最大保留条数，默认 200。
        """
        if max_len <= 0:
            raise ValueError("max_len must be a positive integer")
        
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        try:
            await client.ltrim(key, -max_len, -1)
        except RedisError as e:
            logger.error("Failed to trim history for key %s: %s", key, e)
            raise

    async def set_expire(self, user_id: str, session_id: Optional[str] = None, ttl: int = 2592000) -> None:
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
        try:
            await client.expire(key, ttl)
        except RedisError as e:
            logger.error("Failed to set expire for key %s: %s", key, e)
            raise

    async def clear_history(self, user_id: str, session_id: Optional[str] = None) -> None:
        """
        清空消息历史。
        Args:
            user_id (str): 用户 ID。
            session_id (str, optional): 会话 ID。
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        try:
            await client.delete(key)
        except RedisError as e:
            logger.error("Failed to clear history for key %s: %s", key, e)
            raise

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
            try:
                raw_msg = await client.rpop(key)
            except RedisError as e:
                logger.error("Failed to pop message for key %s: %s", key, e)
                raise

            if raw_msg is None:
                return None

            try:
                msg = Message.model_validate_json(raw_msg)
            except Exception as e:
                preview = raw_msg[:120] + ("..." if len(raw_msg) > 120 else "")
                logger.warning("Skip invalid popped message for key %s: %s | payload preview=%r", key, e, preview)
                # 继续尝试下一条
                continue

            if not msg.tool_call:
                return msg
            # 若为 tool_result，继续循环

    async def close(self) -> None:
        """Close the Redis connection."""
        if self.r:
            try:
                await self.r.aclose()
            finally:
                self.r = None

    async def __aenter__(self):
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()