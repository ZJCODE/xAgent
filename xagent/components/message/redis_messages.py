# Standard library imports
import logging
import os
from typing import Dict, Final, List, Optional, Union
from urllib.parse import quote, urlparse, parse_qs, urlunparse, urlencode

# Third-party imports
import redis.asyncio as redis
from redis.asyncio.cluster import RedisCluster
from redis.exceptions import RedisError

# Local imports
from .base_messages import MessageStorageBase
from ...schemas import Message,MessageType


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


class MessageStorageRedisConfig:
    """Configuration constants for MessageStorageRedis class."""
    
    # Redis key constants
    MSG_PREFIX: Final[str] = "chat"
    
    # Time constants (in seconds)
    DEFAULT_TTL: Final[int] = 2592000  # 30 days
    HEALTH_CHECK_INTERVAL: Final[int] = 30
    SOCKET_CONNECT_TIMEOUT: Final[int] = 5
    SOCKET_TIMEOUT: Final[int] = 5
    
    # Default values
    DEFAULT_MESSAGE_COUNT: Final[int] = 20
    DEFAULT_MAX_HISTORY: Final[int] = 200
    
    # Redis client settings
    CLIENT_NAME: Final[str] = "xagent-message-storage"
    
    # Message preview settings
    MESSAGE_PREVIEW_LENGTH: Final[int] = 120
    
    # URL encoding safe characters
    URL_SAFE_CHARS: Final[str] = "-._~"


class MessageStorageRedis(MessageStorageBase):
    """
    Redis-based message storage backend for conversation history.

    This class provides a robust, scalable solution for storing and retrieving
    conversation messages using Redis as the backend. It supports:
    
    Features:
    - Multi-user and multi-session isolation  
    - Automatic message expiration (TTL)
    - History trimming to manage memory usage
    - Atomic operations using Redis pipelines
    - Connection pooling and health checks
    - Graceful error handling and recovery
    - URL-safe key sanitization (optional)
    
    Storage Format:
    - Keys: "chat:<user_id>:<session_id>"
    - Values: JSON-serialized Message objects in Redis lists
    - Expiration: Configurable TTL with sliding window support
    
    Attributes:
        redis_url: Redis connection URL
        r: Redis client instance (lazy-initialized)
        sanitize_keys: Whether to URL-encode keys for safety
        logger: Logger instance for this class
    """

    def __init__(
        self, 
        redis_url: Optional[str] = None,
        *, 
        sanitize_keys: bool = False
    ):
        """
        Initialize MessageDB instance.
        
        Args:
            redis_url: Redis connection URL. If None, reads from REDIS_URL environment variable
            sanitize_keys: Whether to URL-encode keys for safety. Defaults to False
        
        Raises:
            ValueError: If Redis connection information is not provided
            
        Note:
            The Redis connection is lazy-initialized on first use to avoid
            blocking the constructor with network operations.
        """
        self.redis_url = self._get_redis_url(redis_url)
        self.r: Optional[Union[redis.Redis, RedisCluster]] = None
        self.sanitize_keys = sanitize_keys
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
    
    def _get_redis_url(self, redis_url: Optional[str]) -> str:
        """Get Redis URL from parameter or environment variable."""
        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            raise ValueError(
                "Redis connection information not provided. "
                "Set REDIS_URL environment variable or pass redis_url parameter."
            )
        return url

    async def _get_client(self) -> Union[redis.Redis, RedisCluster]:
        """
        Get or create async Redis client with optimized configuration.
        
        Returns:
            Configured Redis client instance (standalone or cluster) with connection pooling and health checks
            
        Raises:
            RedisError: If Redis connection fails during initial health check
            
        Note:
            The client is cached and reused across method calls. Connection
            parameters are optimized for stability and performance.
            Supports both standalone Redis and Redis Cluster modes.
        """
        if self.r is None:
            self.r = await self._create_redis_client()
            await self._validate_connection()
        return self.r
    
    async def _create_redis_client(self) -> Union[redis.Redis, RedisCluster]:
        """Create and configure Redis client with optimal settings and cluster support."""
        common_kwargs = dict(
            decode_responses=True,
            health_check_interval=MessageStorageRedisConfig.HEALTH_CHECK_INTERVAL,
            socket_connect_timeout=MessageStorageRedisConfig.SOCKET_CONNECT_TIMEOUT,
            socket_timeout=MessageStorageRedisConfig.SOCKET_TIMEOUT,
            retry_on_timeout=True,
            client_name=MessageStorageRedisConfig.CLIENT_NAME,
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

    def _make_key(self, user_id: str, session_id: str) -> str:
        """
        Generate Redis key with optional sanitization.
        
        Args:
            user_id: User identifier (required)
            session_id: Session identifier (required)
            
        Returns:
            Redis key in format 'chat:<user_id>:<session_id>'
            
        Raises:
            ValueError: If user_id or session_id is empty or invalid
            
        Note:
            When sanitize_keys is enabled, user_id and session_id are URL-encoded
            to ensure compatibility with Redis key naming conventions.
        """
        if not user_id or not isinstance(user_id, str):
            raise ValueError("user_id must be a non-empty string")
        
        if not session_id or not isinstance(session_id, str):
            raise ValueError("session_id must be a non-empty string")
        
        # Sanitize identifiers if requested
        sanitized_user_id = self._sanitize_identifier(user_id)
        sanitized_session_id = self._sanitize_identifier(session_id)
        
        # Build key without agent namespace
        return f"{MessageStorageRedisConfig.MSG_PREFIX}:{sanitized_user_id}:{sanitized_session_id}"
    
    def _sanitize_identifier(self, identifier: str) -> str:
        """Sanitize identifier for Redis key usage."""
        if self.sanitize_keys:
            return quote(identifier, safe=MessageStorageRedisConfig.URL_SAFE_CHARS)
        return identifier

    async def add_messages(
        self,
        user_id: str,
        session_id: str,
        messages: Union[Message, List[Message]],
        ttl: int = MessageStorageRedisConfig.DEFAULT_TTL,
        *,
        max_len: Optional[int] = None,
        reset_ttl: bool = True,
    ) -> None:
        """
        Append messages to conversation history with atomic operations.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            messages: Single Message object or list of Message objects
            ttl: Expiration time in seconds
            max_len: Maximum history length (triggers trimming if exceeded)
            reset_ttl: Whether to refresh expiration time (sliding window)
            
        Raises:
            ValueError: If parameters are invalid
            RedisError: If Redis operation fails
            
        Note:
            Uses Redis pipeline for atomic operations to ensure data consistency.
            If max_len is provided, history is automatically trimmed after addition.
        """
        # Validate parameters
        self._validate_add_messages_params(ttl, max_len)
        
        # Normalize input
        normalized_messages = self._normalize_messages_input(messages)
        if not normalized_messages:
            self.logger.info("No messages to add, skipping operation")
            return

        # Execute atomic operation
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        
        try:
            await self._execute_add_messages_pipeline(
                client, key, normalized_messages, ttl, max_len, reset_ttl
            )
            self.logger.info(
                "Added %d messages to key %s", len(normalized_messages), key
            )
        except RedisError as e:
            self.logger.error("Failed to add messages for key %s: %s", key, e)
            raise
    
    def _validate_add_messages_params(self, ttl: int, max_len: Optional[int]) -> None:
        """Validate parameters for add_messages method."""
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be a positive integer")
        if max_len is not None and max_len <= 0:
            raise ValueError("max_len must be a positive integer")
    
    def _normalize_messages_input(
        self, 
        messages: Union[Message, List[Message]]
    ) -> List[Message]:
        """Normalize message input to a list."""
        if not isinstance(messages, list):
            return [messages] if messages else []
        return messages
    
    async def _execute_add_messages_pipeline(
        self,
        client: redis.Redis,
        key: str,
        messages: List[Message],
        ttl: int,
        max_len: Optional[int],
        reset_ttl: bool
    ) -> None:
        """Execute Redis pipeline for adding messages atomically."""
        async with client.pipeline(transaction=False) as pipe:
            # Add messages to list
            pipe.rpush(key, *(m.model_dump_json() for m in messages))
            
            # Trim if max_len specified
            if max_len is not None:
                pipe.ltrim(key, -max_len, -1)
            
            # Set/refresh TTL
            if reset_ttl and ttl is not None:
                pipe.expire(key, ttl)
            
            await pipe.execute()
        

    async def get_messages(
        self, 
        user_id: str, 
        session_id: str, 
        count: int = MessageStorageRedisConfig.DEFAULT_MESSAGE_COUNT
    ) -> List[Message]:
        """
        Retrieve recent messages from conversation history.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            count: Number of recent messages to retrieve
            
        Returns:
            List of Message objects in chronological order (oldest first)
            
        Raises:
            ValueError: If count is not positive
            RedisError: If Redis operation fails
            
        Note:
            Invalid messages in Redis are skipped with warnings logged.
            The method is resilient to data corruption and partial failures.
        """
        if count <= 0:
            raise ValueError("count must be a positive integer")
        
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        
        try:
            raw_messages = await client.lrange(key, -count, -1)
            valid_messages = self._parse_raw_messages(raw_messages, key)
            
            self.logger.debug(
                "Retrieved %d/%d valid messages for key %s", 
                len(valid_messages), len(raw_messages), key
            )
            return valid_messages
            
        except RedisError as e:
            self.logger.error("Failed to get messages for key %s: %s", key, e)
            raise
    
    def _parse_raw_messages(self, raw_messages: List[str], key: str) -> List[Message]:
        """Parse raw Redis messages into Message objects with error handling."""
        valid_messages: List[Message] = []
        
        for i, raw_msg in enumerate(raw_messages):
            try:
                message = Message.model_validate_json(raw_msg)
                valid_messages.append(message)
            except Exception as e:
                preview = self._create_message_preview(raw_msg)
                self.logger.warning(
                    "Skipping invalid message at index %d for key %s: %s | preview=%s",
                    i, key, e, preview
                )
        
        return valid_messages
    
    def _create_message_preview(self, raw_message: str) -> str:
        """Create a safe preview of raw message for logging."""
        if len(raw_message) <= MessageStorageRedisConfig.MESSAGE_PREVIEW_LENGTH:
            return repr(raw_message)
        return repr(
            raw_message[:MessageStorageRedisConfig.MESSAGE_PREVIEW_LENGTH] + "..."
        )

    async def trim_history(
        self, 
        user_id: str, 
        session_id: str, 
        max_len: int = MessageStorageRedisConfig.DEFAULT_MAX_HISTORY
    ) -> None:
        """
        Trim conversation history to maximum length.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            max_len: Maximum number of messages to retain
            
        Raises:
            ValueError: If max_len is not positive
            RedisError: If Redis operation fails
            
        Note:
            Keeps the most recent max_len messages and removes older ones.
        """
        if max_len <= 0:
            raise ValueError("max_len must be a positive integer")
        
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        
        try:
            await client.ltrim(key, -max_len, -1)
            self.logger.debug("Trimmed history to %d messages for key %s", max_len, key)
        except RedisError as e:
            self.logger.error("Failed to trim history for key %s: %s", key, e)
            raise

    async def set_expire(
        self, 
        user_id: str, 
        session_id: str, 
        ttl: int = MessageStorageRedisConfig.DEFAULT_TTL
    ) -> None:
        """
        Set expiration time for conversation history.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            ttl: Time to live in seconds
            
        Raises:
            ValueError: If ttl is not positive
            RedisError: If Redis operation fails
        """
        if ttl <= 0:
            raise ValueError("ttl must be a positive integer")
        
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        
        try:
            await client.expire(key, ttl)
            self.logger.debug("Set TTL to %d seconds for key %s", ttl, key)
        except RedisError as e:
            self.logger.error("Failed to set expire for key %s: %s", key, e)
            raise

    async def clear_history(self, user_id: str, session_id: str) -> None:
        """
        Clear all messages from conversation history.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Raises:
            RedisError: If Redis operation fails
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        
        try:
            deleted_count = await client.delete(key)
            self.logger.debug("Cleared history for key %s (deleted: %d)", key, deleted_count)
        except RedisError as e:
            self.logger.error("Failed to clear history for key %s: %s", key, e)
            raise

    async def pop_message(self, user_id: str, session_id: str) -> Optional[Message]:
        """
        Remove and return the last non-tool message from history.
        
        This method automatically skips tool-related messages and continues
        popping until a regular message is found or the history is empty.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            The removed Message object (non-tool), or None if history is empty
            or contains only tool messages
            
        Raises:
            RedisError: If Redis operation fails
            
        Note:
            Tool messages are automatically removed but not returned.
            This ensures only meaningful conversation messages are popped.
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        
        messages_popped = 0
        while True:
            try:
                raw_msg = await client.rpop(key)
                messages_popped += 1
            except RedisError as e:
                self.logger.error("Failed to pop message for key %s: %s", key, e)
                raise

            # No more messages
            if raw_msg is None:
                self.logger.debug("No messages found for key %s", key)
                return None

            # Try to parse message
            try:
                message = Message.model_validate_json(raw_msg)
            except Exception as e:
                preview = self._create_message_preview(raw_msg)
                self.logger.warning(
                    "Skipping invalid popped message for key %s: %s | preview=%s", 
                    key, e, preview
                )
                continue  # Continue to next message

            # Return first non-tool message
            if not self._is_tool_message(message):
                self.logger.debug(
                    "Popped non-tool message for key %s (checked %d messages)", 
                    key, messages_popped
                )
                return message
            
            # Continue if this is a tool message
            self.logger.debug("Skipping tool message for key %s", key)
    
    def _is_tool_message(self, message: Message) -> bool:
        """Check if a message is a tool-related message."""
        return message.type in {MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT}


    async def close(self) -> None:
        """
        Close the Redis connection and cleanup resources.
        
        This method is idempotent and safe to call multiple times.
        """
        if self.r:
            try:
                await self.r.aclose()
                self.logger.debug("Redis connection closed successfully")
            except Exception as e:
                self.logger.warning("Error closing Redis connection: %s", e)
            finally:
                self.r = None

    async def __aenter__(self):
        """
        Async context manager entry.
        
        Returns:
            Self instance with established Redis connection
        """
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Async context manager exit with automatic cleanup.
        
        Args:
            exc_type: Exception type (if any)
            exc_val: Exception value (if any)
            exc_tb: Exception traceback (if any)
        """
        await self.close()
    
    async def ping(self) -> bool:
        """
        Test Redis connection health.
        
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
    
    async def get_key_info(self, user_id: str, session_id: str) -> dict:
        """
        Get metadata about a conversation key.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            Dictionary containing key metadata
        """
        client = await self._get_client()
        key = self._make_key(user_id, session_id)
        
        try:
            length = await client.llen(key)
            ttl = await client.ttl(key)
            
            return {
                "key": key,
                "message_count": length,
                "ttl_seconds": ttl if ttl != -1 else None,
                "exists": length > 0
            }
        except RedisError as e:
            self.logger.error("Failed to get key info for %s: %s", key, e)
            return {
                "key": key,
                "message_count": 0,
                "ttl_seconds": None,
                "exists": False,
                "error": str(e)
            }
    
    def get_session_info(self, user_id: str, session_id: str) -> Dict[str, str]:
        """
        Get session information for MessageDB.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            Dictionary containing session metadata
        """
        return {
            "user_id": user_id,
            "session_id": session_id,
            "backend": "redis",
            "session_key": f"{user_id}:{session_id}",
            "redis_url": self.redis_url,
            "sanitize_keys": str(self.sanitize_keys)
        }

    def __str__(self) -> str:
        """String representation of MessageDB instance."""
        return f"MessageDB(url='{self.redis_url}', sanitize_keys={self.sanitize_keys})"
    
    def __repr__(self) -> str:
        """Detailed string representation of MessageDB instance."""
        connected = "connected" if self.r else "disconnected"
        return (
            f"MessageDB(url='{self.redis_url}', "
            f"sanitize_keys={self.sanitize_keys}, "
            f"status='{connected}')"
        )