"""Server-side adapter for the api transport channel."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.runtime import AsyncJobDeliveryDispatcher, AsyncTaskScheduler, SubconsciousDelivery
from ...interfaces.server.runtime_routes import register_runtime_routes
from .chat_service import ChatService
from .config import ChatLimits
from .constants import CHANNEL_API
from .delivery import DeliveryBus
from .job_notify import JobNotifyService
from .task_dispatch import TaskDispatchService


class ApiChannelAdapter:
    """Bridge between HTTP/WebSocket routes and the Agent for the api channel."""

    CHANNEL = CHANNEL_API

    def __init__(
        self,
        agent: Agent,
        *,
        contacts_file: Path,
        tasks_dir: Path,
        jobs_dir: Path | None = None,
        workspace_dir: Path | None = None,
        limits: ChatLimits | None = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.agent = agent
        self.contacts_file = contacts_file
        self.tasks_dir = tasks_dir
        self.jobs_dir = Path(jobs_dir) if jobs_dir is not None else Path(tasks_dir).parent / AgentConfig.JOBS_DIRNAME
        self.workspace_dir = (
            Path(workspace_dir)
            if workspace_dir is not None
            else Path(getattr(agent, "workspace_dir", Path(tasks_dir).parent / AgentConfig.WORKSPACE_DIRNAME))
        )
        self.limits = limits or ChatLimits()
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.delivery = DeliveryBus(logger=self.logger)
        self.chat = ChatService(
            agent,
            contacts_file=contacts_file,
            limits=self.limits,
            logger=self.logger,
        )
        self.tasks = TaskDispatchService(
            agent,
            chat=self.chat,
            delivery=self.delivery,
            logger=self.logger,
        )
        self.jobs = JobNotifyService(
            agent,
            delivery=self.delivery,
            logger=self.logger,
        )
        self._task_scheduler: Optional[AsyncTaskScheduler] = None
        self._job_delivery: Optional[AsyncJobDeliveryDispatcher] = None

    @property
    def job_supervisor(self) -> Optional[AsyncJobDeliveryDispatcher]:
        """Compatibility alias for callers that only need wake()."""
        return self._job_delivery

    def wake_jobs(self) -> None:
        if self._job_delivery is not None:
            self._job_delivery.wake()

    async def start(self) -> None:
        scheduler = AsyncTaskScheduler(
            self.tasks_dir,
            can_handle=self.tasks.can_handle,
            dispatch=self.tasks.dispatch,
            logger_=self.logger,
        )
        self._task_scheduler = scheduler
        await scheduler.start()
        self.logger.info("Scheduled task runtime started: tasks=%s", self.tasks_dir)

        delivery = AsyncJobDeliveryDispatcher(
            self.jobs_dir,
            channels=("", "api", "local"),
            can_notify=self.jobs.can_notify,
            notify=self.jobs.notify,
            logger_=self.logger,
        )
        self._job_delivery = delivery
        await delivery.start()
        self.logger.info("Background job delivery started: jobs=%s", self.jobs_dir)

    async def stop(self) -> None:
        if self._job_delivery is not None:
            await self._job_delivery.stop()
            self._job_delivery = None
            self.logger.info("Background job delivery stopped")
        if self._task_scheduler is not None:
            await self._task_scheduler.stop()
            self._task_scheduler = None
            self.logger.info("Scheduled task runtime stopped")

    async def deliver_subconscious_message(self, delivery: SubconsciousDelivery) -> None:
        await self.delivery.deliver_subconscious(delivery, agent=self.agent)

    def register_routes(self, app: FastAPI) -> None:
        register_runtime_routes(app, self)
