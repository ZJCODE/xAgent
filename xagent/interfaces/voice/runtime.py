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

from .aggregator import iter_aggregated_utterances
from .barge_in import BargeInConfig, BargeInEvaluator
from .config import SONIOX_TTS_CHANNELS, SONIOX_TTS_SAMPLE_RATE, VoiceChannelConfig
from .floor import ConversationFloor, FloorCommandKind, FloorEvent, FloorEventKind, FloorState
from .types import VoiceUtterance
from .speech_text import (
    ConversationLanguageTracker,
    StreamingTTSSanitizer,
    VOICE_CHANNEL_INSTRUCTIONS,
    sanitize_spoken_text,
)
from .notices import VoiceNoticeCache, VoiceNoticeCatalog, VoiceNoticeSpeaker
from .spoken_ledger import SpokenLedger
from .turn_metrics import VoiceTurnMetrics, VoiceTurnMetricsWriter

_PLAYBACK_MICROPHONE_COOLDOWN_SECONDS = 0.5


@dataclass(frozen=True)
class VoiceRuntimeOptions:
    """Runtime options controlled by the CLI command."""

    user_id: str = "local_voice"
    stream: bool = True
    tasks_dir: Optional[Path | str] = None
    metrics_path: Optional[Path | str] = None


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
        self._language_tracker = ConversationLanguageTracker(
            fallback=self.config.fallback_language,
            hints=list(self.config.language_hints),
        )
        metrics_path = self.options.metrics_path
        if metrics_path is None and self.options.tasks_dir is not None:
            metrics_path = Path(self.options.tasks_dir).parent / "voice_turn_metrics.jsonl"
        self._metrics_writer = VoiceTurnMetricsWriter(metrics_path)
        notice_cache_root = None
        if self.options.tasks_dir is not None:
            notice_cache_root = Path(self.options.tasks_dir).parent / "voice_notice_cache"
        self._notice_speaker = VoiceNoticeSpeaker(
            catalog=VoiceNoticeCatalog.default(),
            cache=VoiceNoticeCache(notice_cache_root),
            synthesize=self._synthesize_notice_text,
            play=self._play_notice_audio,
            language_for=self._language_tracker.language_before_turn,
        )
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._floor = ConversationFloor()
        self._utterance_queue: asyncio.Queue[VoiceUtterance | None] | None = None
        self._steer_result: asyncio.Future[str] | None = None
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
        self._event_loop = asyncio.get_running_loop()
        self.output("xAgent voice ready. Speak to the microphone; press Ctrl+C to stop.")
        audio_chunks = self.microphone.iter_chunks(
            pause_event=self.pause_event,
            stop_event=self.stop_event,
        )
        raw_utterances = self.recognizer.iter_utterances(
            audio_chunks,
            pause_event=self.pause_event,
            stop_event=self.stop_event,
        )
        if self.config.aggregate_utterances:
            utterances = iter_aggregated_utterances(
                raw_utterances,
                stop_event=self.stop_event,
            )
        else:
            utterances = raw_utterances
        self._utterance_queue = asyncio.Queue()
        utterance_worker = asyncio.create_task(self._utterance_worker(utterances))
        try:
            if self.task_scheduler is not None:
                await self.task_scheduler.start()
            while not self.stop_event.is_set():
                utterance = await self._utterance_queue.get()
                if utterance is None:
                    break
                transcript = utterance.text.strip()
                if not transcript:
                    await self._speak_notice("not_understood")
                    continue
                endpoint_at = time.monotonic()
                self.output(f"User: {transcript}")
                try:
                    await self._reply_to_utterance(utterance, endpoint_at=endpoint_at)
                except Exception as exc:
                    self.logger.exception("Voice turn failed")
                    self.output(f"Voice turn failed: {exc}")
                    await self._speak_notice("error")
        finally:
            self.stop_event.set()
            self.pause_event.clear()
            self.synthesizer.cancel()
            close = getattr(self.synthesizer, "close", None)
            if callable(close):
                close()
            if self.task_scheduler is not None:
                await self.task_scheduler.stop()
            utterance_worker.cancel()
            await asyncio.gather(utterance_worker, return_exceptions=True)

    async def _utterance_worker(self, utterances: Iterable[VoiceUtterance]) -> None:
        assert self._utterance_queue is not None
        while not self.stop_event.is_set():
            utterance = await asyncio.to_thread(_next_or_none, utterances)
            await self._utterance_queue.put(utterance)
            if utterance is None:
                return

    @property
    def _duplex_capture_enabled(self) -> bool:
        return bool(self.config.enable_interruptions)

    async def _execute_floor_commands(self, commands) -> None:
        for command in commands:
            if command.kind is FloorCommandKind.MIC_MUTE:
                self.pause_event.set()
            elif command.kind is FloorCommandKind.MIC_OPEN:
                self.pause_event.clear()
            elif command.kind is FloorCommandKind.CANCEL_AGENT:
                abort = getattr(self.agent, "abort", None)
                if callable(abort):
                    abort()
            elif command.kind is FloorCommandKind.CANCEL_TTS:
                self.synthesizer.cancel()
            elif command.kind is FloorCommandKind.EMIT_NOTICE:
                if command.notice_category:
                    await self._speak_notice(command.notice_category)

    async def _reply_to_utterance(
        self,
        utterance: VoiceUtterance,
        *,
        endpoint_at: float,
    ) -> None:
        transcript = utterance.text.strip()
        self._floor.allow_duplex_capture = self._duplex_capture_enabled
        while transcript:
            timing = _TurnTiming(endpoint_at=endpoint_at)
            metrics = VoiceTurnMetrics(
                transcript=transcript,
                tts_language=self._language_tracker.language_before_turn(),
                endpoint_at=endpoint_at,
            )
            reply_buffer: list[str] = []
            speak_language = {"value": self._language_tracker.language_before_turn()}
            spoken_ledger = SpokenLedger(
                sample_rate=SONIOX_TTS_SAMPLE_RATE,
                channels=SONIOX_TTS_CHANNELS,
            )
            await self._execute_floor_commands(
                self._floor.handle(FloorEvent(FloorEventKind.STT_ENDPOINT, text=transcript))
            )
            steer_event = asyncio.Event()
            self._active_steer_event = steer_event
            self._steer_result = asyncio.get_running_loop().create_future()
            steer_task: asyncio.Task[None] | None = None
            if self._duplex_capture_enabled:
                steer_task = asyncio.create_task(self._steer_during_think(transcript, steer_event))
            still_working = asyncio.create_task(self._still_working_guard(timing))
            interrupted = {"value": False}
            try:
                await self._speak(
                    self._agent_text_chunks(transcript, reply_buffer=reply_buffer),
                    speak_language=speak_language,
                    reply_buffer=reply_buffer,
                    timing=timing,
                    metrics=metrics,
                    spoken_ledger=spoken_ledger,
                    interrupted_flag=interrupted,
                )
            except Exception as exc:
                metrics.error_class = type(exc).__name__
                raise
            finally:
                steer_event.set()
                self._active_steer_event = None
                if steer_task is not None:
                    await asyncio.gather(steer_task, return_exceptions=True)
                still_working.cancel()
                await asyncio.gather(still_working, return_exceptions=True)
                metrics.turn_end_at = time.monotonic()
                metrics.reply_char_count = sum(len(part) for part in reply_buffer)
                metrics.tts_language = speak_language["value"]
                metrics.interrupted = bool(interrupted["value"])
                self._metrics_writer.write(metrics)
                await self._execute_floor_commands(
                    self._floor.handle(FloorEvent(FloorEventKind.PLAYBACK_STOPPED))
                )
                self.logger.info(
                    "Voice latency turn_total_ms=%.1f",
                    (metrics.turn_end_at - endpoint_at) * 1000,
                )
            if self._steer_result is not None and self._steer_result.done():
                transcript = self._steer_result.result().strip()
                endpoint_at = time.monotonic()
                continue
            break

    async def _steer_during_think(self, base_transcript: str, stop_event: asyncio.Event) -> None:
        assert self._utterance_queue is not None
        assert self._steer_result is not None
        merged = base_transcript
        while not stop_event.is_set():
            try:
                nxt = await asyncio.wait_for(self._utterance_queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            if nxt is None:
                await self._utterance_queue.put(None)
                return
            addition = nxt.text.strip()
            if not addition:
                continue
            merged = f"{merged} {addition}".strip()
            await self._execute_floor_commands(
                self._floor.handle(FloorEvent(FloorEventKind.STT_ENDPOINT, text=merged))
            )
            if not self._steer_result.done():
                self._steer_result.set_result(merged)
            return

    async def _monitor_barge_in(
        self,
        *,
        playback_stop_event: threading.Event,
        spoken_ledger: SpokenLedger,
        reply_buffer: list[str],
        interrupted_flag: dict[str, bool] | None = None,
    ) -> None:
        relay = getattr(self.recognizer, "partial_relay", None)
        if relay is None:
            return
        cfg = self.config.interruption
        evaluator = BargeInEvaluator(
            BargeInConfig(min_speech_ms=cfg.min_speech_ms, min_words=cfg.min_words)
        )
        partial_since: float | None = None
        while not playback_stop_event.is_set() and not self.stop_event.is_set():
            partial = relay.snapshot().strip()
            if partial:
                if partial_since is None:
                    partial_since = time.monotonic()
                elif (time.monotonic() - partial_since) * 1000 >= cfg.min_speech_ms:
                    evaluator._state.armed = True
                verdict = evaluator.evaluate_partial(partial)
                if verdict == "interrupt":
                    if interrupted_flag is not None:
                        interrupted_flag["value"] = True
                    await self._execute_floor_commands(
                        self._floor.handle(
                            FloorEvent(FloorEventKind.BARGE_IN_CONFIRMED, text=partial)
                        )
                    )
                    playback_stop_event.set()
                    full_text = sanitize_spoken_text("".join(reply_buffer))
                    await self._annotate_spoken_metadata(full_text, spoken_ledger)
                    return
                if verdict == "backchannel":
                    partial_since = None
                    evaluator.reset()
            else:
                partial_since = None
                evaluator.reset()
            await asyncio.sleep(0.05)

    async def _annotate_spoken_metadata(self, full_text: str, spoken_ledger: SpokenLedger) -> None:
        if not full_text:
            return
        metadata = spoken_ledger.spoken_through_metadata(full_text)
        handler = getattr(self.agent, "message_handler", None)
        patch = getattr(handler, "patch_latest_assistant_metadata", None)
        if not callable(patch):
            return
        try:
            await patch(
                channel="voice",
                recipient_id=self.options.user_id,
                metadata=metadata,
            )
        except Exception:
            self.logger.debug("Failed to patch spoken-through metadata", exc_info=True)

    async def _speak(
        self,
        text_chunks: AsyncIterator[str],
        *,
        language: str | None = None,
        speak_language: dict[str, str] | None = None,
        reply_buffer: list[str] | None = None,
        timing: _TurnTiming | None = None,
        metrics: VoiceTurnMetrics | None = None,
        spoken_ledger: SpokenLedger | None = None,
        interrupted_flag: dict[str, bool] | None = None,
    ) -> None:
        """Speak one stream while capture remains paused, then apply echo cooldown."""
        if speak_language is None:
            speak_language = {
                "value": language or self._language_tracker.language_before_turn(),
            }
        async with self._playback_lock:
            if not self._duplex_capture_enabled:
                self.pause_event.set()
            text_queue = _TextChunkQueue()
            playback_stop_event = threading.Event()
            first_text = asyncio.get_running_loop().create_future()
            playback_task: asyncio.Task[None] | None = None
            barge_task: asyncio.Task[None] | None = None
            producer_task = asyncio.create_task(
                self._feed_text_stream(
                    text_chunks,
                    text_queue,
                    first_text,
                    timing,
                    speak_language=speak_language,
                    reply_buffer=reply_buffer,
                )
            )
            failed = False
            try:
                done, _pending = await asyncio.wait(
                    {producer_task, first_text},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if first_text in done:
                    await self._execute_floor_commands(
                        self._floor.handle(FloorEvent(FloorEventKind.AGENT_FIRST_TEXT))
                    )
                    if self._duplex_capture_enabled and spoken_ledger is not None:
                        barge_task = asyncio.create_task(
                            self._monitor_barge_in(
                                playback_stop_event=playback_stop_event,
                                spoken_ledger=spoken_ledger,
                                reply_buffer=reply_buffer or [],
                                interrupted_flag=interrupted_flag,
                            )
                        )
                    playback_task = asyncio.create_task(
                        asyncio.to_thread(
                            self._play_text_queue,
                            text_queue,
                            speak_language,
                            playback_stop_event,
                            timing,
                            metrics,
                            spoken_ledger,
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
                if barge_task is not None:
                    barge_task.cancel()
                    await asyncio.gather(barge_task, return_exceptions=True)
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
        *,
        speak_language: dict[str, str],
        reply_buffer: list[str] | None,
    ) -> None:
        try:
            async for text in source:
                if not text:
                    continue
                if not first_text.done():
                    if reply_buffer:
                        speak_language["value"] = self._language_tracker.language_for_streaming(
                            "".join(reply_buffer)
                        )
                    if timing is not None:
                        timing.mark_first_text(self.logger)
                    first_text.set_result(None)
                    if self._active_steer_event is not None:
                        self._active_steer_event.set()
                text_queue.put(text)
        finally:
            text_queue.close()

    def _play_text_queue(
        self,
        text_queue: "_TextChunkQueue",
        speak_language: dict[str, str],
        playback_stop_event: threading.Event,
        timing: _TurnTiming | None,
        metrics: VoiceTurnMetrics | None,
        spoken_ledger: SpokenLedger | None,
    ) -> None:
        language = speak_language["value"]
        synth_kwargs: dict[str, object] = {}
        if spoken_ledger is not None:
            synth_kwargs["spoken_ledger"] = spoken_ledger
        audio_chunks = self.synthesizer.synthesize_chunks(
            text_queue,
            language=language,
            stop_event=playback_stop_event,
            **synth_kwargs,
        )

        def timed_audio() -> Iterator[bytes]:
            first = True
            for chunk in audio_chunks:
                if first and chunk:
                    first = False
                    if timing is not None:
                        timing.mark_first_audio(self.logger)
                if metrics is not None and chunk:
                    metrics.playback_audio_bytes += len(chunk)
                if spoken_ledger is not None and chunk:
                    spoken_ledger.note_playback_bytes(len(chunk))
                yield chunk

        self.player.play_chunks(timed_audio(), stop_event=playback_stop_event)

    async def _release_microphone_after_playback(self) -> None:
        """Keep capture paused so the speaker tail cannot reach STT."""
        if self._duplex_capture_enabled:
            return
        self.pause_event.set()
        try:
            if not self.stop_event.is_set():
                await asyncio.sleep(_PLAYBACK_MICROPHONE_COOLDOWN_SECONDS)
        finally:
            self.pause_event.clear()

    async def _agent_text_chunks(
        self,
        transcript: str,
        *,
        reply_buffer: list[str] | None = None,
    ) -> AsyncIterator[str]:
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
        sanitizer = StreamingTTSSanitizer()
        raw_reply_parts: list[str] = []
        with scheduled_delivery_context(self._delivery_context()):
            async for event in self.agent.chat_events(
                user_message=transcript,
                user_id=self.options.user_id,
                stream=self.options.stream,
                channel="voice",
                inbox_kind="user_turn",
                channel_instructions=VOICE_CHANNEL_INSTRUCTIONS,
            ):
                event_type = event.get("type")
                message_id = str(event.get("message_id") or uuid.uuid4().hex)
                if event_type == "message_delta":
                    delta = str(event.get("delta") or "")
                    if not delta:
                        continue
                    message_delta_seen.add(message_id)
                    raw_reply_parts.append(delta)
                    if reply_buffer is not None:
                        reply_buffer.append(delta)
                    self.output(delta, end="")
                    started = True
                    for spoken in sanitizer.feed(delta):
                        yield spoken
                elif event_type == "message_done":
                    content = str(event.get("content") or "")
                    if content and message_id not in message_delta_seen:
                        raw_reply_parts.append(content)
                        if reply_buffer is not None:
                            reply_buffer.append(content)
                        self.output(content, end="")
                        started = True
                        for spoken in sanitizer.feed(content):
                            yield spoken
                elif event_type == "error":
                    error = str(event.get("error") or "Agent processing error.")
                    if started:
                        self.output("")
                    raise RuntimeError(f"Agent error: {error}")
        for spoken in sanitizer.flush():
            yield spoken
        if raw_reply_parts:
            self._language_tracker.observe_reply("".join(raw_reply_parts))
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
        spoken = sanitize_spoken_text(text)
        self._language_tracker.observe_reply(spoken)
        await self._speak(
            _single_text_stream(spoken),
            language=self._language_tracker.language_before_turn(),
        )

    async def deliver_subconscious_message(self, delivery: SubconsciousDelivery) -> None:
        if delivery.recipient.channel != "voice":
            raise ValueError(f"Voice runtime cannot deliver subconscious channel {delivery.recipient.channel!r}")
        text = str(delivery.content or "").strip()
        if not text:
            raise ValueError("subconscious voice delivery produced no content")
        self.output("\nSubconscious message")
        spoken = sanitize_spoken_text(text)
        self._language_tracker.observe_reply(spoken)
        await self._speak(
            _single_text_stream(spoken),
            language=self._language_tracker.language_before_turn(),
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

    def _schedule_notice(self, category: str) -> None:
        loop = self._event_loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._speak_notice(category), loop)

    async def _speak_notice(self, category: str) -> None:
        if self.stop_event.is_set():
            return
        async with self._playback_lock:
            previous_pause = self.pause_event.is_set()
            self.pause_event.set()
            playback_stop = threading.Event()
            try:
                await asyncio.to_thread(
                    self._notice_speaker.speak,
                    category,
                )
            finally:
                if not previous_pause:
                    self.pause_event.clear()
                playback_stop.set()

    def _synthesize_notice_text(self, text: str, language: str) -> Iterator[bytes]:
        stop = threading.Event()
        return self.synthesizer.synthesize_chunks([text], language=language, stop_event=stop)

    def _play_notice_audio(self, chunks: Iterable[bytes]) -> None:
        stop = threading.Event()
        self.player.play_chunks(iter(chunks), stop_event=stop)

    async def _still_working_guard(self, timing: _TurnTiming) -> None:
        try:
            await asyncio.sleep(25.0)
            while timing.first_text_at is None and not self.stop_event.is_set():
                await self._speak_notice("still_working")
                await asyncio.sleep(25.0)
        except asyncio.CancelledError:
            return

    async def _scheduled_task_text(self, task: ScheduledTaskRecord) -> str:
        if task.task_type == "message":
            return task.content.strip()
        if task.task_type != "agent":
            raise ValueError(f"unsupported scheduled voice task type: {task.task_type}")

        prompt = str(task.content or "").strip()
        with scheduled_delivery_context(self._delivery_context(task=task)):
            parts: list[str] = []
            message_delta_seen: set[str] = set()
            async for event in self.agent.chat_events(
                user_message=prompt,
                user_id=task.delivery_user_id or self.options.user_id or AgentConfig.DEFAULT_USER_ID,
                stream=self.options.stream,
                channel="voice",
                inbox_kind="scheduled_turn",
                channel_instructions=VOICE_CHANNEL_INSTRUCTIONS,
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
