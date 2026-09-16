"""Independent world process, not an agent runtime.

This package must not import xagent.core or xagent.integrations.
"""

from __future__ import annotations

__version__ = "0.2.0"

WORLD_ACTOR_ID = "world"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7182
DEFAULT_DATA_ROOT = "~/.agents-world"

# Wire protocol. Bump when adding required client-visible fields.
PROTOCOL_VERSION = 1

MAX_TEXT_LENGTH = 8000
MAX_MENTIONS = 32
MAX_ID_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 64
MAX_FILE_NAME_LENGTH = 180
MAX_ATTACHMENTS = 4
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENTS_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_BYTES = 12 * 1024 * 1024
SPEAK_RATE_PER_SEC = 10
OUTBOUND_QUEUE_SIZE = 256
SYNC_PAGE_SIZE = 200
SNAPSHOT_HISTORY_LIMIT = 50
HELLO_TIMEOUT_SECONDS = 15.0
