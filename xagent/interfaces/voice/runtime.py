"""Runtime orchestration for foreground voice conversations."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import unicodedata
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


_WAKE_IDLE_TIMEOUT = object()


@dataclass(frozen=True)
class VoiceUtterance:
    """A completed user utterance returned by Soniox endpoint detection."""

    text: str
    language: str = ""


@dataclass(frozen=True)
class VoiceRuntimeOptions:
    """Runtime options controlled by the CLI command."""

    user_id: str = "local_voice"
    stream: bool = True
    tasks_dir: Optional[Path | str] = None


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
    """Coordinate microphone, Soniox STT/TTS, and the text agent."""

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
        self._wake_active = False
        self._wake_last_activity_at = 0.0

    async def run_forever(self) -> None:
        """Run the foreground voice loop until interrupted or stopped."""
        self.output(self._ready_message())
        audio_chunks = self.microphone.iter_chunks(
            pause_event=self.pause_event,
            stop_event=self.stop_event,
        )
        utterances = self.recognizer.iter_utterances(
            audio_chunks,
            pause_event=self.pause_event,
            stop_event=self.stop_event,
        )
        next_utterance_task: asyncio.Future[VoiceUtterance | None] | None = None
        try:
            if self.task_scheduler is not None:
                await self.task_scheduler.start()
            next_utterance_task = self._create_next_utterance_task(utterances)
            while not self.stop_event.is_set():
                utterance_result = await self._await_next_utterance(next_utterance_task)
                if utterance_result is _WAKE_IDLE_TIMEOUT:
                    continue
                if utterance_result is None:
                    break
                utterance = self._utterance_after_wake_gate(utterance_result)
                if utterance is None:
                    next_utterance_task = self._create_next_utterance_task(utterances)
                    continue
                transcript = utterance.text.strip()
                if not transcript:
                    next_utterance_task = self._create_next_utterance_task(utterances)
                    continue
                self.output(f"User: {transcript}")
                next_utterance_task = await self._reply_to_utterance(utterance, utterances)
                self._record_wake_activity()
        finally:
            self.stop_event.set()
            if next_utterance_task is not None and not next_utterance_task.done():
                next_utterance_task.cancel()
            if self.task_scheduler is not None:
                await self.task_scheduler.stop()

    def _ready_message(self) -> str:
        if not self.config.wake.enabled:
            return "xAgent voice ready. Speak to the microphone; press Ctrl+C to stop."
        phrases = ", ".join(self.config.wake.wake_phrases)
        return f"xAgent voice ready. Waiting for wake phrase ({phrases}); press Ctrl+C to stop."

    async def _await_next_utterance(
        self,
        next_utterance_task: "asyncio.Future[VoiceUtterance | None]",
    ) -> VoiceUtterance | None | object:
        if not self.config.wake.enabled or not self._wake_active:
            return await next_utterance_task

        remaining = self.config.wake.idle_timeout_seconds - (time.monotonic() - self._wake_last_activity_at)
        if remaining <= 0:
            self._deactivate_wake(reason="timeout")
            return _WAKE_IDLE_TIMEOUT
        try:
            return await asyncio.wait_for(asyncio.shield(next_utterance_task), timeout=remaining)
        except asyncio.TimeoutError:
            self._deactivate_wake(reason="timeout")
            return _WAKE_IDLE_TIMEOUT

    def _utterance_after_wake_gate(self, utterance: VoiceUtterance) -> VoiceUtterance | None:
        if not self.config.wake.enabled:
            return utterance

        transcript = utterance.text.strip()
        if not transcript:
            return None

        if self._wake_active:
            if self._is_exit_phrase(transcript):
                self._deactivate_wake(reason="exit")
                return None
            self._record_wake_activity()
            return utterance

        remainder = self._wake_remainder(transcript)
        if remainder is None:
            return None

        self._activate_wake()
        if not remainder:
            self.output("Wake phrase detected. Listening.")
            return None
        if self._is_exit_phrase(remainder):
            self._deactivate_wake(reason="exit")
            return None
        return VoiceUtterance(text=remainder, language=utterance.language)

    def _activate_wake(self) -> None:
        self._wake_active = True
        self._record_wake_activity()

    def _deactivate_wake(self, *, reason: str) -> None:
        if not self._wake_active:
            return
        self._wake_active = False
        self._wake_last_activity_at = 0.0
        if reason == "timeout":
            self.output("Wake session timed out. Waiting for wake phrase.")
        elif reason == "exit":
            self.output("Wake session ended. Waiting for wake phrase.")

    def _record_wake_activity(self) -> None:
        if self.config.wake.enabled and self._wake_active:
            self._wake_last_activity_at = time.monotonic()

    def _wake_remainder(self, transcript: str) -> str | None:
        return self._match_phrase_remainder(transcript, self.config.wake.wake_phrases)

    def _is_exit_phrase(self, transcript: str) -> bool:
        return self._match_phrase_remainder(transcript, self.config.wake.exit_phrases) is not None

    def _match_phrase_remainder(self, transcript: str, phrases: list[str]) -> str | None:
        normalized, index_map = _normalize_voice_text_with_map(transcript)
        if not normalized:
            return None

        for phrase in phrases:
            normalized_phrase = _normalize_voice_text(phrase)
            if not normalized_phrase:
                continue
            start = 0
            if self.config.wake.match_mode == "contains":
                start = normalized.find(normalized_phrase)
                if start < 0:
                    continue
            elif not normalized.startswith(normalized_phrase):
                continue

            end = start + len(normalized_phrase)
            if end > len(index_map):
                return ""
            original_end = index_map[end - 1] + 1
            return _strip_leading_voice_separators(transcript[original_end:])
        return None

    async def _reply_to_utterance(
        self,
        utterance: VoiceUtterance,
        utterances: Iterator[VoiceUtterance],
    ) -> "asyncio.Future[VoiceUtterance | None]":
        async with self._playback_lock:
            return await self._reply_to_utterance_locked(utterance, utterances)

    async def _reply_to_utterance_locked(
        self,
        utterance: VoiceUtterance,
        utterances: Iterator[VoiceUtterance],
    ) -> "asyncio.Future[VoiceUtterance | None]":
        self.pause_event.set()
        language = self.config.tts_language_for(utterance.language)
        text_queue = _TextChunkQueue()
        playback_stop_event = threading.Event()
        player_errors: list[BaseException] = []
        playback_task: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        returned_utterance_task: asyncio.Future[VoiceUtterance | None] | None = None

        def play_worker() -> None:
            try:
                audio_chunks = self.synthesizer.synthesize_chunks(
                    text_queue,
                    language=language,
                    stop_event=playback_stop_event,
                )
                self.player.play_chunks(audio_chunks, stop_event=playback_stop_event)
            except BaseException as exc:  # noqa: BLE001 - surfaced after agent turn
                player_errors.append(exc)
            finally:
                _complete_future_threadsafe(playback_task, None)

        worker = threading.Thread(target=play_worker, daemon=True)
        worker.start()
        reply_task = asyncio.create_task(self._feed_reply_text(utterance.text, text_queue))
        interrupt_task: asyncio.Future[VoiceUtterance | None] | None = None
        if self.config.enable_interruptions:
            self.pause_event.clear()
            interrupt_task = self._create_next_utterance_task(utterances)
        try:
            if interrupt_task is None:
                await reply_task
                await playback_task
                if player_errors:
                    raise RuntimeError(f"Voice playback failed: {player_errors[0]}") from player_errors[0]
                returned_utterance_task = self._create_next_utterance_task(utterances)
                return returned_utterance_task

            done, _pending = await asyncio.wait(
                {playback_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupt_task in done:
                interrupt = interrupt_task.result()
                if interrupt is not None:
                    await self._cancel_reply(reply_task, playback_task, text_queue, playback_stop_event)
                    returned_utterance_task = self._completed_utterance_task(interrupt)
                    return returned_utterance_task
                await reply_task
                await playback_task
                if player_errors:
                    raise RuntimeError(f"Voice playback failed: {player_errors[0]}") from player_errors[0]
                returned_utterance_task = self._completed_utterance_task(None)
                return returned_utterance_task

            await reply_task
            if player_errors:
                raise RuntimeError(f"Voice playback failed: {player_errors[0]}") from player_errors[0]
            if interrupt_task.done():
                returned_utterance_task = self._completed_utterance_task(interrupt_task.result())
                return returned_utterance_task
            returned_utterance_task = interrupt_task
            return returned_utterance_task
        except asyncio.CancelledError:
            await self._cancel_reply(reply_task, playback_task, text_queue, playback_stop_event)
            raise
        except Exception:
            await self._cancel_reply(reply_task, playback_task, text_queue, playback_stop_event)
            raise
        finally:
            if (
                interrupt_task is not None
                and interrupt_task is not returned_utterance_task
                and not interrupt_task.done()
            ):
                interrupt_task.cancel()
            self.pause_event.clear()

    async def _feed_reply_text(self, transcript: str, text_queue: "_TextChunkQueue") -> None:
        try:
            async for text in self._agent_text_chunks(transcript):
                if text:
                    text_queue.put(text)
        finally:
            text_queue.close()

    async def _cancel_reply(
        self,
        reply_task: "asyncio.Task[None]",
        playback_task: "asyncio.Future[Any]",
        text_queue: "_TextChunkQueue",
        playback_stop_event: threading.Event,
    ) -> None:
        self.pause_event.set()
        playback_stop_event.set()
        self.synthesizer.cancel()
        text_queue.close()
        if not reply_task.done():
            reply_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reply_task
        if not playback_task.done():
            done, _pending = await asyncio.wait({playback_task}, timeout=1.0)
            if not done and not playback_task.done():
                playback_task.cancel()

    @staticmethod
    def _completed_utterance_task(utterance: VoiceUtterance | None) -> "asyncio.Future[VoiceUtterance | None]":
        async def _return_utterance() -> VoiceUtterance | None:
            return utterance

        return asyncio.create_task(_return_utterance())

    @staticmethod
    def _create_next_utterance_task(
        utterances: Iterator[VoiceUtterance],
    ) -> "asyncio.Future[VoiceUtterance | None]":
        loop = asyncio.get_running_loop()
        future: asyncio.Future[VoiceUtterance | None] = loop.create_future()

        def read_next() -> None:
            try:
                result = _next_or_none(utterances)
            except BaseException as exc:  # noqa: BLE001 - forwarded to the event loop
                _complete_future_threadsafe(future, exception=exc)
                return
            _complete_future_threadsafe(future, result)

        threading.Thread(target=read_next, daemon=True, name="xagent-voice-utterance").start()
        return future

    async def _agent_text_chunks(self, transcript: str) -> AsyncIterator[str]:
        # Record contact for subconscious thought routing
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
                    continue
                if event_type == "message_done":
                    content = str(event.get("content") or "")
                    if content and message_id not in message_delta_seen:
                        self.output(content, end="")
                        started = True
                        yield content
                    continue
                if event_type == "error":
                    error = str(event.get("error") or "Agent processing error.")
                    if started:
                        self.output("")
                    self.output(f"Agent error: {error}")
                    return
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
        async with self._playback_lock:
            await self._play_scheduled_text(text)

    async def _play_scheduled_text(self, text: str) -> None:
        self.pause_event.set()
        playback_stop_event = threading.Event()
        playback_task: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        player_errors: list[BaseException] = []

        def play_worker() -> None:
            text_queue = _TextChunkQueue()
            try:
                text_queue.put(text)
                text_queue.close()
                audio_chunks = self.synthesizer.synthesize_chunks(
                    text_queue,
                    language=self.config.tts_language_for(""),
                    stop_event=playback_stop_event,
                )
                self.player.play_chunks(audio_chunks, stop_event=playback_stop_event)
            except BaseException as exc:  # noqa: BLE001 - forwarded to scheduler
                player_errors.append(exc)
            finally:
                text_queue.close()
                _complete_future_threadsafe(playback_task, None)

        threading.Thread(target=play_worker, daemon=True, name="xagent-voice-scheduled-playback").start()
        try:
            await playback_task
            if player_errors:
                raise RuntimeError(f"Voice scheduled playback failed: {player_errors[0]}") from player_errors[0]
        finally:
            playback_stop_event.set()
            self.pause_event.clear()

    async def deliver_subconscious_message(self, delivery: SubconsciousDelivery) -> None:
        if delivery.recipient.channel != "voice":
            raise ValueError(f"Voice runtime cannot deliver subconscious channel {delivery.recipient.channel!r}")
        text = str(delivery.content or "").strip()
        if not text:
            raise ValueError("subconscious voice delivery produced no content")
        self.output("\nSubconscious message")
        async with self._playback_lock:
            await self._play_scheduled_text(text)
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


def _next_or_none(iterator: Iterator[VoiceUtterance]) -> VoiceUtterance | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _complete_future_threadsafe(
    future: "asyncio.Future[Any]",
    result: Any = None,
    *,
    exception: BaseException | None = None,
) -> None:
    loop = future.get_loop()

    def complete() -> None:
        if future.done():
            return
        if exception is not None:
            future.set_exception(exception)
            return
        future.set_result(result)

    try:
        loop.call_soon_threadsafe(complete)
    except RuntimeError:
        return


def _normalize_voice_text(text: str) -> str:
    normalized, _index_map = _normalize_voice_text_with_map(text)
    return normalized


def _normalize_voice_text_with_map(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text):
        if char.isspace() or unicodedata.category(char).startswith("P"):
            continue
        for folded in char.casefold():
            normalized.append(folded)
            index_map.append(index)
    return "".join(normalized), index_map


def _strip_leading_voice_separators(text: str) -> str:
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or unicodedata.category(char).startswith("P"):
            index += 1
            continue
        break
    return text[index:].strip()


class _TextChunkQueue:
    """Thread bridge for async model text with timeout-aware consumers."""

    _sentinel = object()

    def __init__(self) -> None:
        import queue

        self._queue: "queue.Queue[object]" = queue.Queue()

    def put(self, chunk: str) -> None:
        self._queue.put(chunk)

    def close(self) -> None:
        self._queue.put(self._sentinel)

    def next_item(self, timeout: float) -> str | None:
        import queue

        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is self._sentinel:
            raise StopIteration
        return str(item)

    def __iter__(self) -> Iterator[str]:
        while True:
            try:
                item = self.next_item(timeout=10**9)
            except StopIteration:
                return
            if item is None:
                continue
            yield item
