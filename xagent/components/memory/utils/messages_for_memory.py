import logging
import os
import json
from typing import List, Dict, Any, Optional, Union
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode
from redis.exceptions import RedisError
import redis.asyncio as redis
from redis.asyncio.cluster import RedisCluster
import dotenv

dotenv.load_dotenv(override=True)


def _strip_query_param(url: str, key: str) -> str:
    """Strip a specific query parameter from a URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs.pop(key, None)
    new_query = urlencode([(k, v) for k, vs in qs.items() for v in vs])
    return urlunparse(parsed._replace(query=new_query))


def _looks_like_cluster(redis_url: str) -> bool:
    """Check if the Redis URL indicates cluster mode."""
    p = urlparse(redis_url)
    if p.scheme in ("redis+cluster", "rediss+cluster"):
        return True
    qs = parse_qs(p.query)
    flag = (qs.get("cluster", ["false"])[0] or "").lower()
    return flag in ("1", "true", "yes")


def create_redis_client(redis_url: str, **common_kwargs):
    """Create Redis client supporting both standalone and cluster modes."""
    if _looks_like_cluster(redis_url):
        return RedisCluster.from_url(_strip_query_param(redis_url, "cluster"), **common_kwargs)
    else:
        return redis.Redis.from_url(redis_url, **common_kwargs)


class RedisMessagesForMemoryConfig:
    """Configuration constants for RedisMessagesForMemory class."""
    
    # Redis key constants
    USER_MSG_PREFIX = "messages_for_memory"
    
    # Time constants (in seconds) 
    DEFAULT_TTL = 2592000  # 30 days
    HEALTH_CHECK_INTERVAL = 30
    SOCKET_CONNECT_TIMEOUT = 5
    SOCKET_TIMEOUT = 5
    
    # Redis client settings
    CLIENT_NAME = "xagent-messages-for-memory"
    
    # Default values
    DEFAULT_MAX_MESSAGES = 100  # Maximum messages to store per user


class RedisMessagesForMemory:
    """
    Redis-based temporary user message storage for Upstash Memory.
    
    This class provides Redis storage for temporary user messages that are used
    by UpstashMemory before they are converted to long-term memory storage.
    
    Features:
    - Per-user message lists
    - Automatic TTL management
    - Atomic operations for thread safety
    - Connection pooling and error handling
    
    Storage Format:
    - Keys: "upstash_memory:user_messages:<user_id>"
    - Values: JSON-serialized message dictionaries in Redis lists
    """
    
    def __init__(self, redis_url: Optional[str] = None):
        """
        Initialize RedisMessagesForMemory instance.
        
        Args:
            redis_url: Redis connection URL. If None, reads from REDIS_URL environment variable
        
        Raises:
            ValueError: If Redis connection information is not provided
        """
        self.redis_url = self._get_redis_url(redis_url)
        self.r: Optional[Union[redis.Redis, RedisCluster]] = None
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
    
    def _get_redis_url(self, redis_url: Optional[str]) -> str:
        """Get Redis URL from parameter or environment variable."""
        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            raise ValueError(
                "Redis connection information not provided. "
                "Either pass redis_url parameter or set REDIS_URL environment variable."
            )
        return url
    
    async def _get_client(self) -> Union[redis.Redis, RedisCluster]:
        """Get Redis client, creating it if necessary."""
        if self.r is None:
            self.r = await self._create_redis_client()
            await self._validate_connection()
        return self.r
    
    async def _create_redis_client(self) -> Union[redis.Redis, RedisCluster]:
        """Create and configure Redis client with optimal settings and cluster support."""
        common_kwargs = dict(
            decode_responses=True,
            health_check_interval=RedisMessagesForMemoryConfig.HEALTH_CHECK_INTERVAL,
            socket_connect_timeout=RedisMessagesForMemoryConfig.SOCKET_CONNECT_TIMEOUT,
            socket_timeout=RedisMessagesForMemoryConfig.SOCKET_TIMEOUT,
            retry_on_timeout=True,
            client_name=RedisMessagesForMemoryConfig.CLIENT_NAME,
        )
        
        return create_redis_client(self.redis_url, **common_kwargs)
    
    async def _validate_connection(self) -> None:
        """Validate Redis connection with initial ping."""
        try:
            await self.r.ping()
            self.logger.info("Redis connection established successfully")
        except Exception as e:
            self.logger.error("Redis connection failed: %s", e)
            self.r = None  # Reset client on failure
            raise RedisError(f"Failed to establish Redis connection: {e}") from e
    
    def _make_key(self, user_id: str) -> str:
        """Create Redis key for user messages."""
        return f"{RedisMessagesForMemoryConfig.USER_MSG_PREFIX}:{user_id}"
    
    async def add_messages(self, user_id: str, messages: List[Dict[str, Any]]) -> None:
        """
        Add messages to user's temporary storage.
        
        Args:
            user_id: User identifier
            messages: List of message dictionaries to add
        """
        if not messages:
            self.logger.debug("No messages provided for user %s", user_id)
            return
        
        client = await self._get_client()
        key = self._make_key(user_id)
        
        try:
            # Use pipeline for atomic operations
            async with client.pipeline(transaction=False) as pipe:
                # Add messages to list
                pipe.rpush(key, *(json.dumps(msg) for msg in messages))
                
                # Trim to max length to prevent memory overflow
                pipe.ltrim(key, -RedisMessagesForMemoryConfig.DEFAULT_MAX_MESSAGES, -1)
                
                # Set TTL
                pipe.expire(key, RedisMessagesForMemoryConfig.DEFAULT_TTL)
                
                await pipe.execute()
            
            self.logger.info("Added %d messages for user %s", len(messages), user_id)
            
        except RedisError as e:
            self.logger.info("Failed to add messages for user %s: %s", user_id, e)
            raise
    
    async def get_messages(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Get all messages for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            List of message dictionaries
        """
        client = await self._get_client()
        key = self._make_key(user_id)
        
        try:
            raw_messages = await client.lrange(key, 0, -1)
            
            if not raw_messages:
                self.logger.debug("No messages found for user %s", user_id)
                return []
            
            # Parse JSON messages
            messages = []
            for i, raw_msg in enumerate(raw_messages):
                try:
                    message = json.loads(raw_msg)
                    messages.append(message)
                except json.JSONDecodeError as e:
                    self.logger.warning(
                        "Skipping invalid message at index %d for user %s: %s",
                        i, user_id, e
                    )
            
            self.logger.debug("Retrieved %d messages for user %s", len(messages), user_id)
            return messages
            
        except RedisError as e:
            self.logger.error("Failed to get messages for user %s: %s", user_id, e)
            raise
    
    async def get_message_count(self, user_id: str) -> int:
        """
        Get the number of messages for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            Number of messages
        """
        client = await self._get_client()
        key = self._make_key(user_id)
        
        try:
            count = await client.llen(key)
            self.logger.debug("User %s has %d messages", user_id, count)
            return count
            
        except RedisError as e:
            self.logger.error("Failed to get message count for user %s: %s", user_id, e)
            raise
    
    async def keep_recent_messages(self, user_id: str, keep_count: int) -> None:
        """
        Keep only the most recent N messages for a user.
        
        Args:
            user_id: User identifier
            keep_count: Number of recent messages to keep
        """
        if keep_count <= 0:
            await self.clear_messages(user_id)
            return
        
        client = await self._get_client()
        key = self._make_key(user_id)
        
        try:
            # Trim to keep only the last N messages
            await client.ltrim(key, -keep_count, -1)
            self.logger.debug("Kept %d recent messages for user %s", keep_count, user_id)
            
        except RedisError as e:
            self.logger.error("Failed to trim messages for user %s: %s", user_id, e)
            raise
    
    async def clear_messages(self, user_id: str) -> None:
        """
        Clear all messages for a user.
        
        Args:
            user_id: User identifier
        """
        client = await self._get_client()
        key = self._make_key(user_id)
        
        try:
            await client.delete(key)
            self.logger.debug("Cleared all messages for user %s", user_id)
            
        except RedisError as e:
            self.logger.error("Failed to clear messages for user %s: %s", user_id, e)
            raise
    
    async def extend_messages(self, user_id: str, messages: List[Dict[str, Any]]) -> None:
        """
        Extend existing messages for a user (alias for add_messages for compatibility).
        
        Args:
            user_id: User identifier
            messages: List of message dictionaries to add
        """
        await self.add_messages(user_id, messages)
    
    async def close(self) -> None:
        """Close Redis connection."""
        if self.r:
            await self.r.close()
            self.r = None
            self.logger.info("Redis connection closed")
    
    async def ping(self) -> bool:
        """
        Check Redis connection status.
        
        Returns:
            True if connection is healthy, False otherwise
        """
        try:
            client = await self._get_client()
            await client.ping()
            return True
        except Exception as e:
            self.logger.error("Redis ping failed: %s", e)
            return False
    
    def __repr__(self) -> str:
        """String representation of RedisMessagesForMemory instance."""
        connected = "connected" if self.r else "disconnected"
        return f"RedisMessagesForMemory(url='{self.redis_url}', status='{connected}')"
