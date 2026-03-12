"""State store for live sessions and persisted conversation records."""

from __future__ import annotations

import time
import uuid
from typing import Dict, Optional

from ..components.message.base_messages import MessageStorageBase
from ..schemas import Message, RoleType
from .models import ConversationRecord, JobRecord, LiveSessionState, TranscriptEntry


class ConversationStateStore:
    """In-memory live state plus durable conversation transcript helpers."""

    def __init__(self, message_storage: Optional[MessageStorageBase] = None):
        self.message_storage = message_storage
        self._live_sessions: Dict[str, LiveSessionState] = {}
        self._conversations: Dict[str, ConversationRecord] = {}
        self._jobs: Dict[str, JobRecord] = {}

    def open_live_session(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
        realtime_session_id: Optional[str] = None,
        provider_name: Optional[str] = None,
    ) -> LiveSessionState:
        conversation_id = conversation_id or f"conv_{uuid.uuid4().hex[:10]}"
        realtime_session_id = realtime_session_id or f"rt_{uuid.uuid4().hex[:10]}"
        record = self._conversations.get(conversation_id)
        if record is None:
            record = ConversationRecord(conversation_id=conversation_id, user_id=user_id)
            self._conversations[conversation_id] = record

        session = LiveSessionState(
            realtime_session_id=realtime_session_id,
            conversation_id=conversation_id,
            user_id=user_id,
            provider_name=provider_name,
        )
        self._live_sessions[realtime_session_id] = session
        self.touch_conversation(conversation_id)
        return session

    def get_live_session(self, realtime_session_id: str) -> Optional[LiveSessionState]:
        return self._live_sessions.get(realtime_session_id)

    def require_live_session(self, realtime_session_id: str) -> LiveSessionState:
        session = self.get_live_session(realtime_session_id)
        if session is None:
            raise KeyError(f"Realtime session not found: {realtime_session_id}")
        return session

    def get_or_create_conversation(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
    ) -> ConversationRecord:
        conversation_id = conversation_id or f"conv_{uuid.uuid4().hex[:10]}"
        record = self._conversations.get(conversation_id)
        if record is None:
            record = ConversationRecord(conversation_id=conversation_id, user_id=user_id)
            self._conversations[conversation_id] = record
        return record

    def get_conversation(self, conversation_id: str) -> Optional[ConversationRecord]:
        return self._conversations.get(conversation_id)

    def buffer_text(self, realtime_session_id: str, text: str) -> LiveSessionState:
        session = self.require_live_session(realtime_session_id)
        session.buffered_text = f"{session.buffered_text}{text}"
        session.updated_at = time.time()
        return session

    def buffer_audio_chunk(self, realtime_session_id: str, chunk: str) -> LiveSessionState:
        session = self.require_live_session(realtime_session_id)
        session.buffered_audio_chunks.append(chunk)
        session.updated_at = time.time()
        return session

    def buffer_frame(self, realtime_session_id: str, frame: str) -> LiveSessionState:
        session = self.require_live_session(realtime_session_id)
        session.buffered_frames.append(frame)
        session.updated_at = time.time()
        return session

    def begin_turn(self, realtime_session_id: str, turn_id: Optional[str] = None) -> LiveSessionState:
        session = self.require_live_session(realtime_session_id)
        session.active_turn_id = turn_id or f"turn_{uuid.uuid4().hex[:10]}"
        session.interrupted = False
        session.updated_at = time.time()
        return session

    def set_active_job(self, realtime_session_id: str, job_id: Optional[str]) -> LiveSessionState:
        session = self.require_live_session(realtime_session_id)
        session.active_job_id = job_id
        session.updated_at = time.time()
        return session

    def set_active_response(self, realtime_session_id: str, response_id: Optional[str]) -> LiveSessionState:
        session = self.require_live_session(realtime_session_id)
        session.active_response_id = response_id
        session.updated_at = time.time()
        return session

    def mark_interrupted(self, realtime_session_id: str, interrupted: bool = True) -> LiveSessionState:
        session = self.require_live_session(realtime_session_id)
        session.interrupted = interrupted
        session.updated_at = time.time()
        return session

    def clear_turn_buffers(self, realtime_session_id: str) -> LiveSessionState:
        session = self.require_live_session(realtime_session_id)
        session.buffered_text = ""
        session.buffered_audio_chunks.clear()
        session.buffered_frames.clear()
        session.active_turn_id = None
        session.updated_at = time.time()
        return session

    async def append_transcript(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        record = self._conversations[conversation_id]
        record.transcript.append(
            TranscriptEntry(role=role, content=content, metadata=metadata or {})
        )
        record.updated_at = time.time()

        if self.message_storage is None:
            return

        role_map = {
            "user": RoleType.USER,
            "assistant": RoleType.ASSISTANT,
            "tool": RoleType.TOOL,
            "system": RoleType.SYSTEM,
        }
        message = Message.create(content=content, role=role_map.get(role, RoleType.USER))
        await self.message_storage.add_messages(
            user_id=record.user_id,
            session_id=conversation_id,
            messages=message,
        )

    def add_task_summary(self, conversation_id: str, summary: str) -> None:
        record = self._conversations[conversation_id]
        record.task_summaries.append(summary)
        record.updated_at = time.time()

    def add_tool_summary(self, conversation_id: str, summary: str) -> None:
        record = self._conversations[conversation_id]
        record.tool_summaries.append(summary)
        record.updated_at = time.time()

    def upsert_job(self, job: JobRecord) -> JobRecord:
        job.updated_at = time.time()
        self._jobs[job.job_id] = job

        record = self._conversations[job.conversation_id]
        for index, existing in enumerate(record.jobs):
            if existing.job_id == job.job_id:
                record.jobs[index] = job
                break
        else:
            record.jobs.append(job)
        record.updated_at = time.time()
        return job

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        return self._jobs.get(job_id)

    def touch_conversation(self, conversation_id: str) -> None:
        record = self._conversations[conversation_id]
        record.updated_at = time.time()

    def close_live_session(self, realtime_session_id: str) -> Optional[LiveSessionState]:
        session = self._live_sessions.pop(realtime_session_id, None)
        if session is None:
            return None
        session.status = "closed"
        session.updated_at = time.time()
        return session
