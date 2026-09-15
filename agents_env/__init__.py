"""Independent agents environment: a world process, not an agent runtime.

This package must not import xagent.core or xagent.integrations.
"""

from __future__ import annotations

__version__ = "0.1.0"

WORLD_ACTOR_ID = "world"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DATA_ROOT = "~/.agents-env"
