"""Independent agents environment: a world process, not an agent runtime.

This package must not import xagent.core or xagent.integrations.
"""

from __future__ import annotations

__version__ = "0.2.0"

WORLD_ACTOR_ID = "world"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DATA_ROOT = "~/.agents-env"

# Wire protocol. Bump when adding required client-visible fields.
PROTOCOL_VERSION = 1

MAX_TEXT_LENGTH = 8000
MAX_MENTIONS = 32
MAX_ID_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 64
SPEAK_RATE_PER_SEC = 10
OUTBOUND_QUEUE_SIZE = 256
SYNC_PAGE_SIZE = 200
SNAPSHOT_HISTORY_LIMIT = 50
HELLO_TIMEOUT_SECONDS = 15.0
