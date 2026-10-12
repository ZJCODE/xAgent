"""Track channel-owned work without cancelling the shared Agent consumer."""
from __future__ import annotations

import asyncio
from typing import Coroutine, Any


class ChannelActivity:
    def __init__(self) -> None:
        self.accepting = True
        self.tasks: set[asyncio.Task] = set()

    def create_task(self, work: Coroutine[Any, Any, Any], *, accepted: bool = False) -> asyncio.Task:
        if not self.accepting and not accepted:
            work.close()
            raise RuntimeError("Channel is disconnecting")
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def quiesce(self) -> None:
        self.accepting = False
