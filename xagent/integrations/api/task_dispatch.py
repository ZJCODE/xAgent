"""Scheduled task dispatch for the api channel."""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Callable, Optional

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.delivery import DeliveryContext
from ...core.inbox import InboxItem, InboxKind
from ...core.recipients import make_recipient_key
from .chat_service import ChatService
from .constants import CHANNEL_API
from .delivery import DeliveryBus


class TaskDispatchService:
    """Execute and deliver scheduled tasks for api channel recipients."""

    def __init__(
        self,
        agent: Agent,
        *,
        chat: ChatService,
        delivery: DeliveryBus,
        open_session: Callable[[DeliveryContext], Any],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.agent = agent
        self.chat = chat
        self.delivery = delivery
        self.open_session = open_session
        self.logger = logger or logging.getLogger(self.__class__.__name__)

    def can_handle(self, task) -> bool:
        if task.kind != "task":
            return False
        return task.delivery_channel == CHANNEL_API

    async def dispatch(self, task) -> None:
        user_id = task.delivery_user_id or str(task.target.get("user_id") or AgentConfig.DEFAULT_USER_ID)
        context = DeliveryContext(
            channel=task.delivery_channel or CHANNEL_API,
            user_id=user_id,
            recipient_key=make_recipient_key(task.delivery_channel or CHANNEL_API, user_id),
            display_name=str(task.target.get("display_name") or ""),
            target=dict(task.target or {}),
            metadata={
                "source": "scheduled_task",
                "task_id": task.task_id,
                "task_name": task.name,
                "task_type": task.task_type,
                "run_at": task.run_at.isoformat(sep=" "),
                "task": task,
            },
        )
        session = self.open_session(context)
        metadata = {
            "scheduled_task": {
                "id": task.task_id,
                "name": task.name,
                "type": task.task_type,
                "run_at": task.run_at.isoformat(sep=" "),
                "delivery": task.delivery,
            }
        }
        if task.task_type == "message":
            if not str(task.content or "").strip():
                raise ValueError("scheduled task produced no content")
            deliver = getattr(self.agent, "deliver_prepared_message", None)
            if callable(deliver):
                stored = await deliver(
                    session=session,
                    content=task.content.strip(),
                    channel=CHANNEL_API,
                    recipient_id=user_id,
                    metadata=metadata,
                )
                if stored is None:
                    raise RuntimeError("api scheduled send was not accepted")
                return
            ack = await session.consume({
                "type": "message_done",
                "phase": "final",
                "content": task.content.strip(),
            })
            if not ack.accepted:
                raise RuntimeError(ack.error or "api scheduled send was not accepted")
            return
        if task.task_type != "agent":
            raise ValueError(f"unsupported scheduled task type: {task.task_type}")

        await self.chat.acquire_slot()
        try:
            submit = getattr(self.agent, "submit", None)
            if callable(submit):
                item = InboxItem(
                    kind=InboxKind.SCHEDULED_TURN,
                    content=str(task.content or "").strip(),
                    user_id=user_id,
                    channel=task.delivery_channel or CHANNEL_API,
                    delivery=context,
                    metadata={"source": "scheduled_task", "task_id": task.task_id},
                )
                deadline = time.monotonic() + self.chat._chat_timeout
                async for _event in self.chat._iterate_before_deadline(
                    submit(item, session=session),
                    deadline,
                ):
                    pass
                return
            chat_events = getattr(self.agent, "chat_events", None)
            if not callable(chat_events):
                raise RuntimeError("Agent does not support chat_events().")
            deadline = time.monotonic() + self.chat._chat_timeout
            async for event in self.chat._iterate_before_deadline(
                chat_events(
                    user_message=str(task.content or "").strip(),
                    user_id=user_id,
                    stream=False,
                    channel=task.delivery_channel or CHANNEL_API,
                    inbox_kind="scheduled_turn",
                    session=session,
                ),
                deadline,
            ):
                consume = getattr(session, "consume", None)
                if callable(consume):
                    result = consume(event)
                    if inspect.isawaitable(result):
                        await result
        finally:
            self.chat.release_slot()
