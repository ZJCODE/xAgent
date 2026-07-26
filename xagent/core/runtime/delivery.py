from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .channels import ChannelDisabled, ChannelManager, ChannelUnavailable
from .state import RuntimeStateStore
from .types import (
    DELIVERY_STATUS_BLOCKED,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENDING,
    DELIVERY_STATUS_UNKNOWN,
)


logger = logging.getLogger(__name__)


class DeliveryDispatcher:
    """Deliver durable outbound records without entering the cognitive actor."""

    def __init__(
        self,
        state: RuntimeStateStore,
        channels: ChannelManager,
        *,
        poll_seconds: float = 0.5,
    ) -> None:
        self.state = state
        self.channels = channels
        self.poll_seconds = max(0.01, poll_seconds)
        self._stop = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._wakeup.set()
        self._task = asyncio.create_task(self._loop(), name="xagent-delivery-dispatcher")

    def wake(self) -> None:
        self._wakeup.set()

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._wakeup.clear()
            deliveries = await self.state.list_dispatchable_deliveries()
            for delivery in deliveries:
                await self.state.update_delivery(
                    delivery.delivery_id,
                    status=DELIVERY_STATUS_SENDING,
                    increment_attempts=True,
                )
                try:
                    await self.channels.send(delivery)
                except asyncio.CancelledError:
                    await self.state.update_delivery(
                        delivery.delivery_id,
                        status=DELIVERY_STATUS_UNKNOWN,
                        error="delivery dispatcher stopped while sending",
                    )
                    raise
                except ChannelDisabled as exc:
                    await self.state.update_delivery(
                        delivery.delivery_id,
                        status=DELIVERY_STATUS_BLOCKED,
                        error=str(exc),
                    )
                except ChannelUnavailable as exc:
                    await self.state.update_delivery(
                        delivery.delivery_id,
                        status=DELIVERY_STATUS_FAILED,
                        error=str(exc),
                    )
                except ValueError as exc:
                    await self.state.update_delivery(
                        delivery.delivery_id,
                        status=DELIVERY_STATUS_FAILED,
                        error=str(exc),
                    )
                except Exception as exc:
                    logger.exception("Delivery outcome is unknown: %s", delivery.delivery_id)
                    await self.state.update_delivery(
                        delivery.delivery_id,
                        status=DELIVERY_STATUS_UNKNOWN,
                        error=str(exc),
                    )
                else:
                    await self.state.update_delivery(
                        delivery.delivery_id,
                        status=DELIVERY_STATUS_DELIVERED,
                    )
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
