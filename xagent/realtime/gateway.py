"""Realtime gateway for xAgent event protocol."""

from __future__ import annotations

from typing import List, Optional

from ..orchestrator import AgentOrchestrator, OrchestratorContext, TurnInput
from ..state import ConversationStateStore
from .events import RealtimeClientEvent, RealtimeServerEvent
from .interrupts import InterruptController
from .providers.openai_realtime import OpenAIRealtimeBridge
from .session import RealtimeSessionManager


class RealtimeGateway:
    """Provider-neutral realtime gateway with xAgent event protocol."""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        state_store: ConversationStateStore,
        provider_cls: type[OpenAIRealtimeBridge] = OpenAIRealtimeBridge,
    ):
        self.orchestrator = orchestrator
        self.state_store = state_store
        self.session_manager = RealtimeSessionManager(state_store=state_store)
        self.interrupt_controller = InterruptController(
            state_store=state_store,
            job_manager=orchestrator.job_manager,
        )
        self.provider_cls = provider_cls

    async def open_session(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
    ) -> RealtimeServerEvent:
        session = self.session_manager.open(
            user_id=user_id,
            conversation_id=conversation_id,
            provider_name="openai",
        )
        provider = self.provider_cls()
        self.session_manager.attach_provider(session.realtime_session_id, provider)
        connected = await provider.connect(
            event_sink=lambda event: self.session_manager.emit(
                session.realtime_session_id,
                event.model_copy(
                    update={
                        "conversation_id": session.conversation_id,
                        "realtime_session_id": session.realtime_session_id,
                    }
                ),
            )
        )
        session.provider_session["connected"] = connected
        return RealtimeServerEvent(
            type="session.state",
            conversation_id=session.conversation_id,
            realtime_session_id=session.realtime_session_id,
            payload={
                "status": session.status,
                "provider": session.provider_name,
                "provider_connected": connected,
            },
        )

    async def handle_client_event(self, event: RealtimeClientEvent) -> List[RealtimeServerEvent]:
        if event.type == "session.start":
            if not event.user_id:
                raise ValueError("session.start requires user_id")
            return [await self.open_session(event.user_id, event.conversation_id)]

        if not event.realtime_session_id:
            raise ValueError("Realtime events require realtime_session_id")

        session = self.state_store.require_live_session(event.realtime_session_id)
        responses: List[RealtimeServerEvent] = [
            RealtimeServerEvent(
                type="ack",
                conversation_id=session.conversation_id,
                realtime_session_id=session.realtime_session_id,
                turn_id=event.turn_id,
                payload={"received": event.type},
            )
        ]

        provider = self.session_manager.get_provider(session.realtime_session_id)

        if event.type == "input.text":
            text = str(event.payload.get("text", ""))
            self.state_store.buffer_text(session.realtime_session_id, text)
            return responses

        if event.type == "input.audio.chunk":
            chunk = str(event.payload.get("audio", ""))
            self.state_store.buffer_audio_chunk(session.realtime_session_id, chunk)
            if provider is not None:
                await provider.append_audio_chunk(chunk)
            return responses

        if event.type == "input.image.frame":
            frame = str(event.payload.get("frame", ""))
            self.state_store.buffer_frame(session.realtime_session_id, frame)
            return responses

        if event.type == "interrupt":
            await self.interrupt_controller.interrupt(session.realtime_session_id)
            if provider is not None:
                await provider.interrupt()
            return responses

        if event.type == "session.close":
            closed = await self.session_manager.close(session.realtime_session_id)
            responses.append(
                RealtimeServerEvent(
                    type="session.state",
                    conversation_id=event.conversation_id or (closed.conversation_id if closed else None),
                    realtime_session_id=event.realtime_session_id,
                    payload={"status": "closed"},
                )
            )
            return responses

        if event.type != "turn.commit":
            return responses

        session = self.state_store.begin_turn(session.realtime_session_id, event.turn_id)
        if provider is not None and session.buffered_audio_chunks:
            await provider.commit_audio()

        if not session.buffered_text and session.buffered_audio_chunks:
            if not session.provider_session.get("connected", False):
                responses.append(
                    RealtimeServerEvent(
                        type="turn.completed",
                        conversation_id=session.conversation_id,
                        realtime_session_id=session.realtime_session_id,
                        turn_id=session.active_turn_id or event.turn_id,
                        payload={"error": "Audio input requires an active realtime provider connection."},
                    )
                )
                self.state_store.clear_turn_buffers(session.realtime_session_id)
                return responses
            responses.append(
                RealtimeServerEvent(
                    type="turn.started",
                    conversation_id=session.conversation_id,
                    realtime_session_id=session.realtime_session_id,
                    turn_id=session.active_turn_id or event.turn_id,
                    payload={"mode": "provider_audio"},
                )
            )
            self.state_store.clear_turn_buffers(session.realtime_session_id)
            return responses

        turn = TurnInput(
            text=session.buffered_text,
            audio_chunks=list(session.buffered_audio_chunks),
            frames=list(session.buffered_frames),
            image_source=event.payload.get("image_source"),
            requested_tool=event.payload.get("tool_name"),
            metadata=event.payload.get("tool_args", {}),
        )
        context = OrchestratorContext(
            user_id=session.user_id,
            conversation_id=session.conversation_id,
            turn_id=session.active_turn_id or event.turn_id or "turn_unknown",
            realtime_session_id=session.realtime_session_id,
            history_count=int(event.payload.get("history_count", 16)),
            max_iter=int(event.payload.get("max_iter", 10)),
            max_concurrent_tools=int(event.payload.get("max_concurrent_tools", 10)),
            allow_background=True,
            enable_memory=bool(event.payload.get("enable_memory", False)),
        )
        responses.append(
            RealtimeServerEvent(
                type="turn.started",
                conversation_id=session.conversation_id,
                realtime_session_id=session.realtime_session_id,
                turn_id=context.turn_id,
                payload={},
            )
        )

        async def on_job_event(event_type: str, payload: dict) -> None:
            await self.session_manager.emit(
                session.realtime_session_id,
                RealtimeServerEvent(
                    type=event_type,
                    conversation_id=session.conversation_id,
                    realtime_session_id=session.realtime_session_id,
                    turn_id=context.turn_id,
                    payload=payload,
                ),
            )

        async def on_stream(delta: str) -> None:
            await self.session_manager.emit(
                session.realtime_session_id,
                RealtimeServerEvent(
                    type="partial_text",
                    conversation_id=session.conversation_id,
                    realtime_session_id=session.realtime_session_id,
                    turn_id=context.turn_id,
                    payload={"delta": delta},
                ),
            )

        result = await self.orchestrator.handle_turn(
            turn=turn,
            context=context,
            event_callback=on_job_event,
            stream_callback=on_stream,
        )
        if result.job_id:
            self.state_store.set_active_job(session.realtime_session_id, result.job_id)
            responses.append(
                RealtimeServerEvent(
                    type="job.started",
                    conversation_id=session.conversation_id,
                    realtime_session_id=session.realtime_session_id,
                    turn_id=context.turn_id,
                    payload={"job_id": result.job_id},
                )
            )
        else:
            responses.append(
                RealtimeServerEvent(
                    type="turn.completed",
                    conversation_id=session.conversation_id,
                    realtime_session_id=session.realtime_session_id,
                    turn_id=context.turn_id,
                    payload={
                        "response_id": result.response_id,
                        "output_text": result.output_text,
                    },
                )
            )

        self.state_store.clear_turn_buffers(session.realtime_session_id)
        return responses
