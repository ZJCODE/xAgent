"""xAgent as a world inhabitant: hear → observe / decide; speak → speak."""

from __future__ import annotations

from .inhabitant import WorldInhabitant
from .presence import (
    mark_world_left,
    mark_world_presence,
    read_world_presence,
    write_world_presence,
)

__all__ = [
    "WorldInhabitant",
    "mark_world_left",
    "mark_world_presence",
    "read_world_presence",
    "write_world_presence",
]
