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
from ...schemas import Message, MessageType


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
    return redis.Redis.from_url(redis_url, **common_kwargs)


class MessageStorageCloudConfig:
    """Configuration constants for MessageStorageCloud."""

    MSG_PREFIX: Final[str] = "xagent:stream"
    DEFAULT_STREAM_NAME: Final[str] = "default_agent"
    DEFAULT_TTL: Final[int] = 2592000
    HEALTH_CHECK_INTERVAL: Final[int] = 30
    SOCKET_CONNECT_TIMEOUT: Final[int] = 5
    SOCKET_TIMEOUT: Final[int] = 5
    DEFAULT_MESSAGE_COUNT: Final[int] = 100
    DEFAULT_MAX_HISTORY: Final[int] = 500
    CLIENT_NAME: Final[str] = "xagent-message-storage"
    MESSAGE_PREVIEW_LENGTH: Final[int] = 120
    URL_SAFE_CHARS: Final[str] = "-._~"


class MessageStorageCloud(MessageStorageBase):
    """Cloud-backed single-stream message storage using Redis."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        *,
        stream_name: str = MessageStorageCloudConfig.DEFAULT_STREAM_NAME,
        sanitize_keys: bool = False,
    ):
        self.redis_url = self._get_redis_url(redis_url)
        self.stream_name = stream_name or MessageStorageCloudConfig.DEFAULT_STREAM_NAME
        self.sanitize_keys = sanitize_keys
        self.r: Optional[Union[redis.Redis, RedisCluster]] = None
        self.logger = logging.getLogger(f"{self.__class__.__name__}")

    def _get_redis_url(self, redis_url: Optional[str]) -> str:
        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            raise ValueError(
                "Redis connection information not provided. "
                "Set REDIS_URL environment variable or pass redis_url parameter."
            )
        return url

    async def _get_client(self) -> Union[redis.Redis, RedisCluster]:
        if self.r is None:
            self.r = await self._create_redis_client()
            await self._validate_connection()
        return self.r

    async def _create_redis_client(self) -> Union[redis.Redis, RedisCluster]:
        common_kwargs = dict(
            decode_responses=True,
            health_check_interval=MessageStorageCloudConfig.HEALTH_CHECK_INTERVAL,
            socket_connect_timeout=MessageStorageCloudConfig.SOCKET_CONNECT_TIMEOUT,
            socket_timeout=MessageStorageCloudConfig.SOCKET_TIMEOUT,
            retry_on_timeout=True,
            client_name=MessageStorageCloudConfig.CLIENT_NAME,
        )
        return create_redis_client(self.redis_url, **common_kwargs)

    async def _validate_connection(self) -> None:
        try:
            await self.r.ping()
            self.logger.info("Redis connection established successfully")
        except Exception as exc:
            self.logger.error("Redis connection failed: %s", exc)
            self.r = None
            raise RedisError(f"Failed to establish Redis connection: {exc}") from exc

    def _make_key(self) -> str:
        if not self.stream_name or not isinstance(self.stream_name, str):
            raise ValueError("stream_name must be a non-empty string")

        stream_name = self._sanitize_identifier(self.stream_name)
        return f"{MessageStorageCloudConfig.MSG_PREFIX}:{stream_name}"

    def _sanitize_identifier(self, identifier: str) -> str:
        if self.sanitize_keys:
            return quote(identifier, safe=MessageStorageCloudConfig.URL_SAFE_CHARS)
        return identifier

    async def add_messages(
        self,
        messages: Union[Message, List[Message]],
        ttl: int = MessageStorageCloudConfig.DEFAULT_TTL,
        *,
        max_len: Optional[int] = None,
        reset_ttl: bool = True,
    ) -> None:
        self._validate_add_messages_params(ttl, max_len)
        normalized_messages = self._normalize_messages_input(messages)
        if not normalized_messages:
            return

        client = await self._get_client()
        key = self._make_key()
        try:
            await self._execute_add_messages_pipeline(
                client=client,
                key=key,
                messages=normalized_messages,
                ttl=ttl,
                max_len=max_len,
                reset_ttl=reset_ttl,
            )
        except RedisError as exc:
            self.logger.error("Failed to add messages for key %s: %s", key, exc)
            raise

    def _validate_add_messages_params(self, ttl: int, max_len: Optional[int]) -> None:
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be a positive integer")
        if max_len is not None and max_len <= 0:
            raise ValueError("max_len must be a positive integer")

    def _normalize_messages_input(
        self,
        messages: Union[Message, List[Message]],
    ) -> List[Message]:
        if not isinstance(messages, list):
            return [messages] if messages else []
        return messages

    async def _execute_add_messages_pipeline(
        self,
        client: Union[redis.Redis, RedisCluster],
        key: str,
        messages: List[Message],
        ttl: int,
        max_len: Optional[int],
        reset_ttl: bool,
    ) -> None:
        async with client.pipeline(transaction=False) as pipe:
            pipe.rpush(key, *(m.model_dump_json() for m in messages))
            if max_len is not None:
                pipe.ltrim(key, -max_len, -1)
            if reset_ttl and ttl is not None:
                pipe.expire(key, ttl)
            await pipe.execute()

    async def get_messages(
        self,
        count: int = MessageStorageCloudConfig.DEFAULT_MESSAGE_COUNT,
    ) -> List[Message]:
        if count <= 0:
            raise ValueError("count must be a positive integer")

        client = await self._get_client()
        key = self._make_key()
        try:
            raw_messages = await client.lrange(key, -count, -1)
        except RedisError as exc:
            self.logger.error("Failed to get messages for key %s: %s", key, exc)
            raise
        return self._parse_raw_messages(raw_messages, key)

    def _parse_raw_messages(self, raw_messages: List[str], key: str) -> List[Message]:
        valid_messages: List[Message] = []
        for index, raw_msg in enumerate(raw_messages):
            try:
                valid_messages.append(Message.model_validate_json(raw_msg))
            except Exception as exc:
                preview = self._create_message_preview(raw_msg)
                self.logger.warning(
                    "Skipping invalid message at index %d for key %s: %s | preview=%s",
                    index,
                    key,
                    exc,
                    preview,
                )
        return valid_messages

    def _create_message_preview(self, raw_message: str) -> str:
        if len(raw_message) <= MessageStorageCloudConfig.MESSAGE_PREVIEW_LENGTH:
            return repr(raw_message)
        return repr(raw_message[:MessageStorageCloudConfig.MESSAGE_PREVIEW_LENGTH] + "...")

    async def clear_messages(self) -> None:
        client = await self._get_client()
        key = self._make_key()
        try:
            await client.delete(key)
        except RedisError as exc:
            self.logger.error("Failed to clear message stream for key %s: %s", key, exc)
            raise

    async def pop_message(self) -> Optional[Message]:
        client = await self._get_client()
        key = self._make_key()

        while True:
            try:
                raw_msg = await client.rpop(key)
            except RedisError as exc:
                self.logger.error("Failed to pop message for key %s: %s", key, exc)
                raise

            if raw_msg is None:
                return None

            try:
                message = Message.model_validate_json(raw_msg)
            except Exception as exc:
                preview = self._create_message_preview(raw_msg)
                self.logger.warning(
                    "Skipping invalid popped message for key %s: %s | preview=%s",
                    key,
                    exc,
                    preview,
                )
                continue

            if not self._is_tool_message(message):
                return message

    def _is_tool_message(self, message: Message) -> bool:
        return message.type in {MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT}

    async def get_message_count(self) -> int:
        client = await self._get_client()
        key = self._make_key()
        try:
            return int(await client.llen(key))
        except RedisError as exc:
            self.logger.error("Failed to count messages for key %s: %s", key, exc)
            raise

    def get_stream_info(self) -> Dict[str, str]:
        return {
            "stream": self.stream_name,
            "backend": "cloud",
            "redis_url": self.redis_url,
            "sanitize_keys": str(self.sanitize_keys),
        }

    async def close(self) -> None:
        if self.r:
            try:
                await self.r.aclose()
            except Exception as exc:
                self.logger.warning("Error closing Redis connection: %s", exc)
            finally:
                self.r = None

    async def __aenter__(self):
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def ping(self) -> bool:
        try:
            client = await self._get_client()
            await client.ping()
            return True
        except Exception as exc:
            self.logger.error("Redis ping failed: %s", exc)
            return False

    def __str__(self) -> str:
        return (
            f"MessageStorageCloud("
            f"url='{self.redis_url}', stream_name='{self.stream_name}', sanitize_keys={self.sanitize_keys})"
        )

    def __repr__(self) -> str:
        connected = "connected" if self.r else "disconnected"
        return (
            f"MessageStorageCloud(url='{self.redis_url}', "
            f"stream_name='{self.stream_name}', "
            f"sanitize_keys={self.sanitize_keys}, "
            f"status='{connected}')"
        )
