"""Half-duplex runtime orchestration for local Soniox voice conversations."""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Iterator, Optional, Protocol

from xagent.core.config import AgentConfig
from xagent.core.runtime import (
    AsyncTaskScheduler,
    ScheduledDeliveryContext,
    ScheduledTaskRecord,
    SubconsciousDelivery,
    resolve_contacts_path,
    scheduled_delivery_context,
    upsert_contact,
)

from .config import VoiceChannelConfig

_PLAYBACK_MICROPHONE_COOLDOWN_SECONDS = 0.5


@dataclass(frozen=True)
class VoiceUtterance:
    """A completed user turn returned by Soniox endpoint detection."""

    text: str
    language: str = ""


@dataclass(frozen=True)
class VoiceRuntimeOptions:
    """Runtime options controlled by the CLI command."""

    user_id: str = "local_voice"
    stream: bool = True
    tasks_dir: Optional[Path | str] = None


@dataclass
class _TurnTiming:
    endpoint_at: float
    first_text_at: float | None = None
    first_audio_at: float | None = None

    def mark_first_text(self, logger: logging.Logger) -> None:
        if self.first_text_at is not None:
            return
        self.first_text_at = time.monotonic()
        logger.info(
            "Voice latency endpoint_to_agent_first_text_ms=%.1f",
            (self.first_text_at - self.endpoint_at) * 1000,
        )

    def mark_first_audio(self, logger: logging.Logger) -> None:
        if self.first_audio_at is not None:
            return
        self.first_audio_at = time.monotonic()
        if self.first_text_at is not None:
            logger.info(
                "Voice latency agent_first_text_to_tts_first_audio_ms=%.1f",
                (self.first_audio_at - self.first_text_at) * 1000,
            )
        logger.info(
            "Voice latency endpoint_to_first_audio_ms=%.1f",
            (self.first_audio_at - self.endpoint_at) * 1000,
        )


class VoiceMicrophone(Protocol):
    def iter_chunks(
        self,
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        """Yield raw microphone audio chunks."""


class VoiceRecognizer(Protocol):
    def iter_utterances(
        self,
        audio_chunks: Iterable[bytes],
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> Iterator[VoiceUtterance]:
        """Yield complete utterances."""


class VoiceSynthesizer(Protocol):
    def synthesize_chunks(
        self,
        text_chunks: Iterable[str],
        *,
        language: str,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        """Yield synthesized audio chunks."""

    def cancel(self) -> None:
        """Cancel current synthesis."""


class VoicePlayer(Protocol):
    def play_chunks(self, chunks: Iterator[bytes], *, stop_event: threading.Event) -> None:
        """Play audio chunks."""


class VoiceRuntime:
    """Run the single half-duplex listen, think, speak state machine."""

    channel_name = "voice"

    def __init__(
        self,
        *,
        agent: Any,
        config: VoiceChannelConfig,
        microphone: VoiceMicrophone,
        recognizer: VoiceRecognizer,
        synthesizer: VoiceSynthesizer,
        player: VoicePlayer,
        options: Optional[VoiceRuntimeOptions] = None,
        output=print,
    ) -> None:
        self.agent = agent
        self.config = config
        self.microphone = microphone
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.player = player
        self.options = options or VoiceRuntimeOptions()
        self.output = output
        self.logger = logging.getLogger(self.__class__.__name__)
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self._playback_lock = asyncio.Lock()
        self.task_scheduler: AsyncTaskScheduler | None = None
        self._contacts_file: Optional[Path] = None
        if self.options.tasks_dir is not None:
            self.task_scheduler = AsyncTaskScheduler(
                self.options.tasks_dir,
                can_handle=self._can_handle_scheduled_task,
                dispatch=self._dispatch_scheduled_task,
            )
            runtime_root = Path(self.options.tasks_dir).parent
            self._contacts_file = resolve_contacts_path(runtime_root)

    async def run_forever(self) -> None:
        """Run until stopped or a non-recoverable STT error is raised."""
        self.output("xAgent voice ready. Speak to the microphone; press Ctrl+C to stop.")
        audio_chunks = self.microphone.iter_chunks(
            pause_event=self.pause_event,
            stop_event=self.stop_event,
        )
        utterances = self.recognizer.iter_utterances(
            audio_chunks,
            pause_event=self.pause_event,
            stop_event=self.stop_event,
        )
        try:
            if self.task_scheduler is not None:
                await self.task_scheduler.start()
            while not self.stop_event.is_set():
                utterance = await asyncio.to_thread(_next_or_none, utterances)
                if utterance is None:
                    break
                transcript = utterance.text.strip()
                if not transcript:
                    continue
                endpoint_at = time.monotonic()
                self.output(f"User: {transcript}")
                try:
                    await self._reply_to_utterance(utterance, endpoint_at=endpoint_at)
                except Exception as exc:
                    self.logger.exception("Voice turn failed")
                    self.output(f"Voice turn failed: {exc}")
        finally:
            self.stop_event.set()
            self.pause_event.clear()
            self.synthesizer.cancel()
            if self.task_scheduler is not None:
                await self.task_scheduler.stop()

    async def _reply_to_utterance(
        self,
        utterance: VoiceUtterance,
        *,
        endpoint_at: float,
    ) -> None:
        timing = _TurnTiming(endpoint_at=endpoint_at)
        try:
            await self._speak(
                self._agent_text_chunks(utterance.text),
                language=self.config.tts_language_for(utterance.language),
                timing=timing,
            )
        finally:
            self.logger.info(
                "Voice latency turn_total_ms=%.1f",
                (time.monotonic() - endpoint_at) * 1000,
            )

    async def _speak(
        self,
        text_chunks: AsyncIterator[str],
        *,
        language: str,
        timing: _TurnTiming | None = None,
    ) -> None:
        """Speak one stream while capture remains paused, then apply echo cooldown."""
        async with self._playback_lock:
            self.pause_event.set()
            text_queue = _TextChunkQueue()
            playback_stop_event = threading.Event()
            first_text = asyncio.get_running_loop().create_future()
            playback_task: asyncio.Task[None] | None = None
            producer_task = asyncio.create_task(
                self._feed_text_stream(text_chunks, text_queue, first_text, timing)
            )
            failed = False
            try:
                done, _pending = await asyncio.wait(
                    {producer_task, first_text},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if first_text in done:
                    playback_task = asyncio.create_task(
                        asyncio.to_thread(
                            self._play_text_queue,
                            text_queue,
                            language,
                            playback_stop_event,
                            timing,
                        )
                    )
                    pipeline_done, _pipeline_pending = await asyncio.wait(
                        {producer_task, playback_task},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    failure = next(
                        (
                            task.exception()
                            for task in pipeline_done
                            if not task.cancelled() and task.exception() is not None
                        ),
                        None,
                    )
                    if failure is not None:
                        raise failure
                    await asyncio.gather(producer_task, playback_task)
                else:
                    await producer_task
            except BaseException:
                failed = True
                playback_stop_event.set()
                self.synthesizer.cancel()
                text_queue.close()
                if not producer_task.done():
                    producer_task.cancel()
                await asyncio.gather(producer_task, return_exceptions=True)
                if playback_task is not None and not playback_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(playback_task), timeout=1.0)
                    except Exception:
                        pass
                raise
            finally:
                if not first_text.done():
                    first_text.cancel()
                text_queue.close()
                playback_stop_event.set()
                if not failed and playback_task is not None:
                    await asyncio.gather(playback_task, return_exceptions=True)
                await self._release_microphone_after_playback()

    async def _feed_text_stream(
        self,
        source: AsyncIterator[str],
        text_queue: "_TextChunkQueue",
        first_text: "asyncio.Future[None]",
        timing: _TurnTiming | None,
    ) -> None:
        try:
            async for text in source:
                if not text:
                    continue
                if not first_text.done():
                    if timing is not None:
                        timing.mark_first_text(self.logger)
                    first_text.set_result(None)
                text_queue.put(text)
        finally:
            text_queue.close()

    def _play_text_queue(
        self,
        text_queue: "_TextChunkQueue",
        language: str,
        playback_stop_event: threading.Event,
        timing: _TurnTiming | None,
    ) -> None:
        audio_chunks = self.synthesizer.synthesize_chunks(
            text_queue,
            language=language,
            stop_event=playback_stop_event,
        )

        def timed_audio() -> Iterator[bytes]:
            first = True
            for chunk in audio_chunks:
                if first and chunk:
                    first = False
                    if timing is not None:
                        timing.mark_first_audio(self.logger)
                yield chunk

        self.player.play_chunks(timed_audio(), stop_event=playback_stop_event)

    async def _release_microphone_after_playback(self) -> None:
        """Keep capture paused so the speaker tail cannot reach STT."""
        self.pause_event.set()
        try:
            if not self.stop_event.is_set():
                await asyncio.sleep(_PLAYBACK_MICROPHONE_COOLDOWN_SECONDS)
        finally:
            self.pause_event.clear()

    async def _agent_text_chunks(self, transcript: str) -> AsyncIterator[str]:
        if self._contacts_file is not None:
            try:
                upsert_contact(
                    self._contacts_file,
                    channel="voice",
                    user_id=self.options.user_id,
                    target={"user_id": self.options.user_id},
                )
            except Exception:
                pass

        self.output("Agent: ", end="")
        started = False
        message_delta_seen: set[str] = set()
        with scheduled_delivery_context(self._delivery_context()):
            async for event in self.agent.chat_events(
                user_message=transcript,
                user_id=self.options.user_id,
                stream=self.options.stream,
                channel="voice",
                inbox_kind="user_turn",
            ):
                event_type = event.get("type")
                message_id = str(event.get("message_id") or uuid.uuid4().hex)
                if event_type == "message_delta":
                    delta = str(event.get("delta") or "")
                    if not delta:
                        continue
                    message_delta_seen.add(message_id)
                    self.output(delta, end="")
                    started = True
                    yield delta
                elif event_type == "message_done":
                    content = str(event.get("content") or "")
                    if content and message_id not in message_delta_seen:
                        self.output(content, end="")
                        started = True
                        yield content
                elif event_type == "error":
                    error = str(event.get("error") or "Agent processing error.")
                    if started:
                        self.output("")
                    raise RuntimeError(f"Agent error: {error}")
        if started:
            self.output("")

    def _delivery_context(self, *, task: ScheduledTaskRecord | None = None) -> ScheduledDeliveryContext:
        if task is None:
            return ScheduledDeliveryContext(
                channel="voice",
                user_id=self.options.user_id,
                target={"user_id": self.options.user_id},
                metadata={"source": "voice"},
            )
        return ScheduledDeliveryContext(
            channel="voice",
            user_id=task.delivery_user_id or self.options.user_id,
            target=task.delivery.get("target") if isinstance(task.delivery.get("target"), dict) else {},
            metadata={
                "source": "scheduled_task",
                "task_id": task.task_id,
                "task_name": task.name,
                "task_type": task.task_type,
            },
        )

    def _can_handle_scheduled_task(self, task: ScheduledTaskRecord) -> bool:
        return task.kind == "task" and task.delivery_channel == "voice"

    async def _dispatch_scheduled_task(self, task: ScheduledTaskRecord) -> None:
        text = await self._scheduled_task_text(task)
        if not text:
            raise ValueError("scheduled voice task produced no content")
        self.output(f"\nScheduled task: {task.title or task.task_type or 'Reminder'}")
        await self._speak(
            _single_text_stream(text),
            language=self.config.tts_language_for(""),
        )

    async def deliver_subconscious_message(self, delivery: SubconsciousDelivery) -> None:
        if delivery.recipient.channel != "voice":
            raise ValueError(f"Voice runtime cannot deliver subconscious channel {delivery.recipient.channel!r}")
        text = str(delivery.content or "").strip()
        if not text:
            raise ValueError("subconscious voice delivery produced no content")
        self.output("\nSubconscious message")
        await self._speak(
            _single_text_stream(text),
            language=self.config.tts_language_for(""),
        )
        message_handler = getattr(self.agent, "message_handler", None)
        store_model_reply = getattr(message_handler, "store_model_reply", None)
        if callable(store_model_reply):
            try:
                recipient_id = str(
                    delivery.recipient.target.get("user_id")
                    or delivery.recipient.user_id
                    or self.options.user_id
                )
                await store_model_reply(
                    text,
                    getattr(self.agent, "_assistant_sender_id", "agent"),
                    metadata={
                        "subconscious": {
                            "source": "subconscious",
                            "created_at": delivery.created_at.isoformat(sep=" "),
                            "recipient": {
                                "channel": delivery.recipient.channel,
                                "user_id": delivery.recipient.user_id,
                                "target": delivery.recipient.target,
                            },
                        }
                    },
                    channel="voice",
                    recipient_id=recipient_id,
                )
            except Exception:
                self.logger.debug("Failed to persist voice subconscious delivery", exc_info=True)

    async def _scheduled_task_text(self, task: ScheduledTaskRecord) -> str:
        if task.task_type == "message":
            return task.content.strip()
        if task.task_type != "agent":
            raise ValueError(f"unsupported scheduled voice task type: {task.task_type}")

        prompt = AgentConfig.scheduled_agent_prompt(task.content)
        with scheduled_delivery_context(self._delivery_context(task=task)):
            parts: list[str] = []
            message_delta_seen: set[str] = set()
            async for event in self.agent.chat_events(
                user_message=prompt,
                user_id=task.delivery_user_id or self.options.user_id or AgentConfig.DEFAULT_USER_ID,
                stream=self.options.stream,
                channel="voice",
                inbox_kind="scheduled_turn",
            ):
                event_type = event.get("type")
                message_id = str(event.get("message_id") or uuid.uuid4().hex)
                if event_type == "message_delta":
                    delta = str(event.get("delta") or "")
                    if delta:
                        message_delta_seen.add(message_id)
                        parts.append(delta)
                elif event_type == "message_done" and message_id not in message_delta_seen:
                    content = str(event.get("content") or "")
                    if content:
                        parts.append(content)
                elif event_type == "error":
                    raise RuntimeError(str(event.get("error") or "Agent processing error."))
            return "".join(parts).strip()


async def _single_text_stream(text: str) -> AsyncIterator[str]:
    if text:
        yield text


def _next_or_none(iterator: Iterator[VoiceUtterance]) -> VoiceUtterance | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


class _TextChunkQueue:
    """A minimal thread bridge from async LLM deltas to synchronous Soniox TTS."""

    _sentinel = object()

    def __init__(self) -> None:
        self._queue: queue.Queue[object] = queue.Queue()
        self._closed = False
        self._lock = threading.Lock()

    def put(self, chunk: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._queue.put(chunk)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(self._sentinel)

    def __iter__(self) -> Iterator[str]:
        while True:
            item = self._queue.get()
            if item is self._sentinel:
                return
            yield str(item)
