"""Default local message storage backed by the unified experience memory store."""

from __future__ import annotations

from ..memory import ExperienceMemoryStore, ExperienceMemoryStoreConfig


class MessageStorageLocalConfig:
    """Compatibility constants for the default local event stream store."""

    DEFAULT_PATH = ExperienceMemoryStoreConfig.DEFAULT_PATH
    DEFAULT_MESSAGE_COUNT = 100
    CONNECT_TIMEOUT = ExperienceMemoryStoreConfig.CONNECT_TIMEOUT
    TABLE_NAME = "events"


class MessageStorageLocal(ExperienceMemoryStore):
    """Default local storage for message events.

    The old separate message database has been removed. This class remains as
    a narrow import-compatible alias for callers that need a message-storage
    object; all data is stored in the unified experience memory database.
    """

    pass
