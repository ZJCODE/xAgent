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
    Stores all message history using Redis as the backend.

    All message histories are isolated with a unified prefix (chat:), support multiple sessions, and allow message trimming and expiration.
    Main features:
    - Store message history by user/session
    - Support message append, retrieval, trimming, and expiration
    - Unified Redis key encapsulation for easier maintenance
    """
    MSG_PREFIX: Final[str] = "chat"
    DEFAULT_TTL: Final[int] = 2592000  # 30 days

    def __init__(self, redis_url: str = None, *, sanitize_keys: bool = False):
        """
        Initialize MessageDB instance and connect to Redis.
        
        Args:
            redis_url (str, optional): Redis connection URL. Uses parameter first, 
                otherwise reads from REDIS_URL environment variable.
            sanitize_keys (bool, optional): Whether to URL-encode keys for safety. Defaults to False.
        
        Raises:
            ValueError: Redis connection information not provided.
        """
        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            raise ValueError("REDIS_URL not set in environment or not provided as argument")
        self.redis_url = url
        self.r: Optional[redis.Redis] = None
        self._sanitize_keys = sanitize_keys

    async def _get_client(self) -> redis.Redis:
        """
        Get or create async Redis client with sane defaults.
        
        Returns:
            redis.Redis: Configured Redis client instance.
            
        Raises:
            Exception: If Redis connection fails during initial ping.
        """
        if self.r is None:
            # Use reasonable connection parameters to improve stability and robustness
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
        Generate Redis key.
        
        Args:
            user_id (str): User ID.
            session_id (str, optional): Session ID.
            
        Returns:
            str: Redis key in format 'chat:<user_id>' or 'chat:<user_id>:<session_id>'.
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
        Append one or more messages to message history and set expiration time.
        
        Args:
            user_id (str): User ID.
            messages (Message | List[Message]): Message object or list of message objects.
            session_id (str, optional): Session ID.
            ttl (int): Expiration time in seconds, defaults to 30 days.
            max_len (Optional[int]): If provided, trim history to this maximum length after appending.
            reset_ttl (bool): Whether to refresh expiration time (sliding expiration). Defaults to True.
            
        Raises:
            ValueError: If ttl or max_len are invalid.
            RedisError: If Redis operation fails.
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

        # Use pipeline to batch multiple round trips and maintain atomicity
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
        Get message history, retrieving the most recent `count` messages in reverse order.
        
        Args:
            user_id (str): User ID.
            session_id (str, optional): Session ID.
            count (int): Number of messages to retrieve, defaults to 20.
            
        Returns:
            List[Message]: List of message objects, sorted in chronological order.
            
        Raises:
            RedisError: If Redis operation fails.
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
                # Control log size to avoid printing overly long strings
                preview = m[:120] + ("..." if len(m) > 120 else "")
                logger.warning(
                    "Skip invalid message at index %d for key %s: %s | payload preview=%r",
                    i, key, e, preview,
                )
        return messages

    async def trim_history(self, user_id: str, session_id: Optional[str] = None, max_len: int = 200) -> None:
        """
        Trim message history, keeping only the most recent `max_len` messages.
        
        Args:
            user_id (str): User ID.
            session_id (str, optional): Session ID.
            max_len (int): Maximum number of messages to retain, defaults to 200.
            
        Raises:
            ValueError: If max_len is not positive.
            RedisError: If Redis operation fails.
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
        Set expiration time for message history.
        
        Args:
            user_id (str): User ID.
            session_id (str, optional): Session ID.
            ttl (int): Expiration time in seconds, defaults to 30 days.
            
        Raises:
            ValueError: If ttl is not positive.
            RedisError: If Redis operation fails.
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
        Clear message history.
        
        Args:
            user_id (str): User ID.
            session_id (str, optional): Session ID.
            
        Raises:
            RedisError: If Redis operation fails.
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
        Remove and return the last non-tool_result message. If the last message is a tool_result,
        automatically continue popping until a non-tool_result message is found or the list is empty.
        
        Args:
            user_id (str): User ID.
            session_id (str, optional): Session ID.
            
        Returns:
            Optional[Message]: The removed message object (non-tool_result), or None if empty.
            
        Raises:
            RedisError: If Redis operation fails.
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
                # Continue to next message if this is a tool_result
                continue

            if not msg.tool_call:
                return msg
            # If it's a tool_result, continue the loop

    async def close(self) -> None:
        """Close the Redis connection."""
        if self.r:
            try:
                await self.r.aclose()
            finally:
                self.r = None

    async def __aenter__(self):
        """Async context manager entry."""
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """Async context manager exit."""
        await self.close()