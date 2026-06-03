"""Runtime orchestration for foreground voice conversations."""
from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Iterator, Optional, Protocol

from .config import VoiceChannelConfig


@dataclass(frozen=True)
class VoiceUtterance:
    """A completed user utterance returned by Soniox endpoint detection."""

    text: str
    language: str = ""


@dataclass(frozen=True)
class VoiceRuntimeOptions:
    """Runtime options controlled by the CLI command."""

    user_id: str = "local_voice"
    enable_memory: bool = True
    stream: bool = True


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
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()

    async def run_forever(self) -> None:
        """Run the foreground voice loop until interrupted or stopped."""
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
            while not self.stop_event.is_set():
                utterance = await asyncio.to_thread(_next_or_none, utterances)
                if utterance is None:
                    break
                transcript = utterance.text.strip()
                if not transcript:
                    continue
                self.output(f"User: {transcript}")
                await self._reply_to_utterance(utterance)
        finally:
            self.stop_event.set()

    async def _reply_to_utterance(self, utterance: VoiceUtterance) -> None:
        self.pause_event.set()
        language = self.config.tts_language_for(utterance.language)
        text_queue = _TextChunkQueue()
        player_errors: list[BaseException] = []

        def play_worker() -> None:
            try:
                audio_chunks = self.synthesizer.synthesize_chunks(
                    text_queue,
                    language=language,
                    stop_event=self.stop_event,
                )
                self.player.play_chunks(audio_chunks, stop_event=self.stop_event)
            except BaseException as exc:  # noqa: BLE001 - surfaced after agent turn
                player_errors.append(exc)

        worker = threading.Thread(target=play_worker, daemon=True)
        worker.start()
        try:
            async for text in self._agent_text_chunks(utterance.text):
                if text:
                    text_queue.put(text)
        except asyncio.CancelledError:
            self.synthesizer.cancel()
            raise
        except Exception:
            self.synthesizer.cancel()
            raise
        finally:
            text_queue.close()
            await asyncio.to_thread(worker.join)
            self.pause_event.clear()
        if player_errors:
            raise RuntimeError(f"Voice playback failed: {player_errors[0]}") from player_errors[0]

    async def _agent_text_chunks(self, transcript: str) -> AsyncIterator[str]:
        if not hasattr(self.agent, "chat_events"):
            response = await self.agent(
                user_message=transcript,
                user_id=self.options.user_id,
                enable_memory=self.options.enable_memory,
            )
            text = str(response or "")
            if text:
                self.output(f"Agent: {text}")
                yield text
            return

        self.output("Agent: ", end="")
        started = False
        message_delta_seen: set[str] = set()
        async for event in self.agent.chat_events(
            user_message=transcript,
            user_id=self.options.user_id,
            stream=self.options.stream,
            enable_memory=self.options.enable_memory,
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


def _next_or_none(iterator: Iterator[VoiceUtterance]) -> VoiceUtterance | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


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
