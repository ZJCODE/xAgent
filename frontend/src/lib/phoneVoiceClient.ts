import {
  PHONE_OUTPUT_SAMPLE_RATE,
  assertSocketAllowedOnPage,
  clampSampleRate,
  floatToPcm16,
  pcm16ToChannelPlanes,
  pcmRms,
  voiceHello,
  type VoiceReadyFrame,
} from "./deviceVoice";

import { OpusInput, OpusOutput, PcmResampler, VoiceCodecFailure, VoiceAudioBackpressure, packAudio, unpackAudio, supportedCodecs, type AudioCodec } from "./voiceAudio";

export class VoiceConnectionSuperseded extends Error {
  constructor() {
    super("Connection superseded.");
    this.name = "VoiceConnectionSuperseded";
  }
}

export interface PhoneVoiceListener {
  onLive: (frame: VoiceReadyFrame) => void;
  onFloor: (floor: string) => void;
  onPlaying: (playing: boolean) => void;
  onLevel: (level: number) => void;
  onTranscript: (frame: { role?: unknown; text?: unknown; delta?: unknown; final?: unknown }) => void;
  onClose: (info: { code: number; reason: string; codecFailure?: boolean }) => void;
  onNotice: (message: string) => void;
  onCapture?: (capturing: boolean) => void;
}

interface ScheduledChunk {
  startAt: number;
  duration: number;
  bytes: number;
}

const MAX_BUFFERED_BYTES = 256 * 1024;

export class PhoneVoiceClient {
  private listener: PhoneVoiceListener;
  private socket: WebSocket | null = null;
  private audio: AudioContext | null = null;
  private declaredRate = 16000;
  private ready = false;
  private attempt = 0;
  private captureToken = 0;
  private captureStop: ((flush: boolean) => Promise<void>) | null = null;
  private captureRequested = false;
  private captureFinishing: Promise<void> | null = null;
  private captureAbort: (() => void) | null = null;
  private pingTimer = 0;
  private progressTimer = 0;
  private handshakeTimer = 0;
  private handshakeReject: ((error: Error) => void) | null = null;
  private lastReportedBytes = 0;
  private sources: AudioBufferSourceNode[] = [];
  private scheduled: ScheduledChunk[] = [];
  private nextTime = 0;
  private playRate = PHONE_OUTPUT_SAMPLE_RATE;
  private playChannels = 1;
  private acceptingAudio = false;
  private playbackId: string | null = null;
  private protocol = 1;
  private inputCodec: AudioCodec = "pcm_s16le";
  private outputCodec: AudioCodec = "pcm_s16le";
  private inputStream = 0;
  private outputStream = 0;
  private outputSequence = 0;
  private outputSamples = 0;
  private decoder: OpusOutput | null = null;
  private wireDone = false;
  private workletContext: AudioContext | null = null;
  private failedWorkletContext: AudioContext | null = null;
  private offeredInput: AudioCodec[] = ["pcm_s16le"];
  private offeredOutput: AudioCodec[] = ["pcm_s16le"];

  constructor(listener: PhoneVoiceListener) {
    this.listener = listener;
  }

  get live(): boolean {
    return this.ready && this.socket?.readyState === WebSocket.OPEN;
  }

  prime(): void {
    if (!this.audio) this.audio = createAudioContext();
    settle(this.audio.resume());
    this.declaredRate = clampSampleRate(this.audio.sampleRate);
  }

  async connect(options: { socketUrl: string; pageProtocol: string; forcePcm?: boolean }): Promise<VoiceReadyFrame> {
    const attempt = ++this.attempt;
    this.closeSocket();
    this.prime();
    const audio = this.audio;
    if (!audio) throw new Error("This browser cannot capture or play audio.");
    const socketUrl = options.socketUrl;
    assertSocketAllowedOnPage(socketUrl, options.pageProtocol);
    const socket = new WebSocket(socketUrl);
    socket.binaryType = "arraybuffer";
    this.socket = socket;

    try {
      const readyFrame = await new Promise<VoiceReadyFrame>((resolve, reject) => {
        this.handshakeReject = reject;
        this.handshakeTimer = window.setTimeout(() => {
          if (attempt !== this.attempt || this.socket !== socket) return;
          this.handshakeReject = null;
          socket.close();
          reject(new Error("Voice handshake timed out."));
        }, 8000);
        socket.onopen = async () => {
          if (attempt !== this.attempt || this.socket !== socket) return;
          settle(audio.resume());
          const worklet = Boolean(audio.audioWorklet && typeof AudioWorkletNode !== "undefined");
          const codecs = await supportedCodecs(worklet, Boolean(options.forcePcm));
          if (attempt !== this.attempt || this.socket !== socket) return;
          this.offeredInput = codecs.input;
          this.offeredOutput = codecs.output;
          socket.send(JSON.stringify(voiceHello(audio.sampleRate, codecs)));
        };
        socket.onmessage = (event) => {
          if (attempt !== this.attempt || this.socket !== socket) return;
          if (typeof event.data !== "string") return;
          try {
            const frame = JSON.parse(event.data) as VoiceReadyFrame;
            if (frame.type !== "ready") return;
            window.clearTimeout(this.handshakeTimer);
            this.handshakeReject = null;
            resolve(frame);
          } catch {
            // Ignore non-JSON until the ready frame.
          }
        };
        socket.onerror = () => undefined;
        socket.onclose = (event) => {
          if (this.handshakeReject !== reject) return;
          window.clearTimeout(this.handshakeTimer);
          this.handshakeReject = null;
          reject(new Error(closeMessage(event.code, event.reason)));
        };
      });
      if (attempt !== this.attempt) throw new VoiceConnectionSuperseded();
      this.protocol = readyFrame.audio_protocol ?? 1;
      this.inputCodec = readyFrame.input_codec ?? "pcm_s16le";
      this.outputCodec = readyFrame.output_codec ?? "pcm_s16le";
      if (!this.offeredInput.includes(this.inputCodec) || !this.offeredOutput.includes(this.outputCodec)) throw new Error("Unsupported negotiated audio codec.");
      this.declaredRate = this.protocol === 2 ? (this.inputCodec === "opus" ? 48000 : 16000) : clampSampleRate(audio.sampleRate);
      this.outputStream = 0;
      this.ready = true;
      socket.onmessage = (event) => {
        if (this.socket === socket && attempt === this.attempt) this.handleFrame(event);
      };
      socket.onclose = (event) => {
        if (!this.ready || this.socket !== socket) return;
        this.closeSocket();
        this.stopPlayback();
        this.listener.onClose({ code: event.code, reason: event.reason });
      };
      this.pingTimer = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping" }));
      }, 20000);
      this.progressTimer = window.setInterval(() => this.reportProgress(), 200);
      this.listener.onLive(readyFrame);
      return readyFrame;
    } catch (error) {
      if (attempt === this.attempt) this.closeSocket();
      throw error;
    }
  }

  disconnect(): void {
    this.attempt += 1;
    this.captureToken += 1;
    this.closeSocket();
    this.stopPlayback();
    const audio = this.audio;
    this.audio = null;
    if (audio) settle(audio.close());
    this.listener.onLevel(0);
    this.listener.onPlaying(false);
  }

  async setHolding(holding: boolean): Promise<void> {
    return this.setCapture(holding);
  }

  async setCapture(enabled: boolean): Promise<void> {
    if (!enabled) {
      this.captureRequested = false;
      const stop = this.captureStop;
      this.captureStop = null;
      const finishing = stop ? stop(true) : this.captureFinishing;
      this.captureFinishing = finishing;
      if (finishing) await finishing;
      else this.captureToken += 1;
      if (this.captureFinishing === finishing) this.captureFinishing = null;
      if (!this.captureRequested) { this.listener.onCapture?.(false); this.listener.onLevel(0); }
      return;
    }
    if (!this.live || this.captureRequested) return;
    this.captureAbort?.();
    this.captureAbort = null;
    this.captureFinishing = null;
    this.captureRequested = true;
    this.prime();
    // prime resumes audio; the negotiated transport rate is independent of the device clock.
    this.declaredRate = this.protocol === 2 ? (this.inputCodec === "opus" ? 48000 : 16000) : clampSampleRate(this.audio!.sampleRate);
    const audio = this.audio!;
    const token = ++this.captureToken;
    let stream: MediaStream | null = null;
    let source: MediaStreamAudioSourceNode | null = null;
    let processor: AudioWorkletNode | ScriptProcessorNode | null = null;
    let mute: GainNode | null = null;
    let encoder: OpusInput | null = null;
    let resolveFlush: (() => void) | null = null;
    const resampler = new PcmResampler(audio.sampleRate, this.declaredRate);
    const current = () => token === this.captureToken && this.live && this.audio === audio;
    const cleanup = () => {
      if (processor instanceof AudioWorkletNodeSafe()) processor.port.onmessage = null;
      else if (processor) (processor as ScriptProcessorNode).onaudioprocess = null;
      source?.disconnect(); processor?.disconnect(); mute?.disconnect();
      stream?.getTracks().forEach((track) => track.stop());
    };
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false,
      });
      if (!current()) { cleanup(); return; }
      source = audio.createMediaStreamSource(stream);
      mute = audio.createGain();
      mute.gain.value = 0;
      if (audio.audioWorklet && this.failedWorkletContext !== audio && typeof AudioWorkletNode !== "undefined") {
        try {
          if (this.workletContext !== audio) {
            await audio.audioWorklet.addModule(new URL("./voiceCapture.worklet.js", import.meta.url).href);
            this.workletContext = audio;
          }
          processor = new AudioWorkletNode(audio, "voice-capture", {numberOfInputs: 1, numberOfOutputs: 1,
            outputChannelCount: [1], processorOptions: {rate: this.declaredRate}});
        } catch (error) {
          this.failedWorkletContext = audio;
          if (this.inputCodec === "opus") throw new VoiceCodecFailure(String(error));
          // Older/embedded browsers retain the existing PCM capture path.
        }
      }
      if (!current()) { cleanup(); return; }
      const inputId = ++this.inputStream;
      let sequence = 0, samplesSent = 0;
      const send = (packet: Parameters<typeof packAudio>[0]) => {
        if (!current() || !this.socket) return;
        const bytesPerSecond = this.inputCodec === "opus" ? 24000 / 8 : this.declaredRate * 2;
        if (this.socket.bufferedAmount > bytesPerSecond / 2 + 1024) throw new VoiceAudioBackpressure("Audio upload is falling behind. Please try again.");
        this.socket.send(packAudio(packet));
      };
      if (this.inputCodec === "opus") encoder = new OpusInput(inputId, send, (error) => { if (current()) this.audioFailed(error); });
      if (this.protocol === 2) this.socket!.send(JSON.stringify({type: "input_start", stream_id: inputId}));
      const write = (samples: Float32Array) => {
        if (!current() || !samples.length) return;
        try {
          if (encoder) encoder.write(samples);
          else if (this.protocol === 2) {
            send({stream: inputId, sequence: sequence++, timestamp: Math.floor(samplesSent * 1e6 / this.declaredRate),
              samples: samples.length, discard: 0, payload: floatToPcm16(samples)});
            samplesSent += samples.length;
          } else if (this.socket!.bufferedAmount < MAX_BUFFERED_BYTES) this.socket!.send(floatToPcm16(samples));
          this.listener.onLevel(Math.min(1, pcmRms(samples) * 4));
        } catch (error) { this.audioFailed(error); }
      };
      if (processor instanceof AudioWorkletNodeSafe()) {
        processor.port.onmessage = ({data}) => {
          if (data === "flushed") resolveFlush?.();
          else if (data instanceof Float32Array) write(data);
        };
        processor.onprocessorerror = () => { if (current()) this.audioFailed(this.inputCodec === "opus" ? new VoiceCodecFailure("Audio capture failed.") : new Error("Audio capture failed.")); };
      } else {
        processor = audio.createScriptProcessor(2048, 1, 1);
        processor.onaudioprocess = (event) => write(resampler.push(event.inputBuffer.getChannelData(0)));
      }
      source.connect(processor); processor.connect(mute); mute.connect(audio.destination);
      const abort = () => { resolveFlush?.(); cleanup(); encoder?.close(); };
      this.captureAbort = abort;
      this.captureStop = async (flush) => {
        source?.disconnect();
        stream?.getTracks().forEach((track) => track.stop());
        let timer = 0;
        try {
          if (flush && current()) {
            const finish = async () => {
              if (processor instanceof AudioWorkletNodeSafe()) {
                await new Promise<void>((resolve) => { resolveFlush = resolve; (processor as AudioWorkletNode).port.postMessage("flush"); });
              }
              if (!(processor instanceof AudioWorkletNodeSafe())) write(resampler.finish());
              await encoder?.finish();
              if (current() && this.protocol === 2) this.socket!.send(JSON.stringify({type: "input_end", stream_id: inputId}));
            };
            await Promise.race([finish(), new Promise<never>((_, reject) => {
              timer = window.setTimeout(() => reject(new VoiceAudioBackpressure("Audio capture did not finish in time.")), 1000);
            })]);
          }
        } catch (error) { if (current()) this.audioFailed(error); }
        finally {
          window.clearTimeout(timer);
          resolveFlush?.();
          cleanup();
          encoder?.close();
          if (this.captureAbort === abort) this.captureAbort = null;
          if (token === this.captureToken) this.captureToken += 1;
        }
      };
      this.listener.onCapture?.(true);
    } catch (error) {
      cleanup(); encoder?.close();
      if (!current()) return;
      this.captureRequested = false;
      if (error instanceof VoiceCodecFailure) { this.audioFailed(error); return; }
      this.captureToken += 1;
      const name = error instanceof DOMException ? error.name : "";
      this.listener.onNotice(name === "NotAllowedError" || name === "PermissionDeniedError"
        ? "Microphone permission was denied. Allow the mic for this page and try again."
        : "This browser could not open the microphone. Use HTTPS or localhost.");
    }
  }

  private audioFailed(error: unknown): void {
    if (!this.live) return;
    const codecFailure = error instanceof VoiceCodecFailure;
    const reason = codecFailure ? "Audio codec failed. Reconnecting with PCM." : error instanceof Error ? error.message : "Audio transport failed.";
    this.closeSocket(); this.stopPlayback();
    this.listener.onLevel(0);
    this.listener.onClose({code: 1011, reason, codecFailure});
  }

  private closeSocket(): void {
    this.captureToken += 1;
    window.clearTimeout(this.handshakeTimer);
    window.clearInterval(this.pingTimer);
    window.clearInterval(this.progressTimer);
    this.pingTimer = 0;
    this.progressTimer = 0;
    this.ready = false;
    this.captureRequested = false;
    this.stopCapture();
    const reject = this.handshakeReject;
    this.handshakeReject = null;
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      if (socket.readyState === WebSocket.OPEN) {
        try {
          socket.send(JSON.stringify({ type: "stop" }));
        } catch {
          // The socket may already be closing.
        }
      }
      if (socket.readyState < WebSocket.CLOSING) socket.close();
    }
    reject?.(new VoiceConnectionSuperseded());
  }

  private handleFrame(event: MessageEvent): void {
    if (event.data instanceof ArrayBuffer) {
      try {
        if (this.protocol === 2) this.receiveAudio(event.data);
        else this.enqueuePcm(event.data);
      } catch (error) { this.audioFailed(error); }
      return;
    }
    if (typeof event.data !== "string") return;
    let frame: Record<string, unknown>;
    try {
      const parsed = JSON.parse(event.data) as unknown;
      if (!parsed || typeof parsed !== "object") return;
      frame = parsed as Record<string, unknown>;
    } catch {
      return;
    }
    if (frame.type === "codec_error") { this.audioFailed(new VoiceCodecFailure(String(frame.message))); return; }
    if (frame.type === "playback_start") {
      const rate = typeof frame.sample_rate === "number" ? frame.sample_rate : PHONE_OUTPUT_SAMPLE_RATE;
      const channels = frame.channels === 2 ? 2 : 1;
      this.beginPlayback(rate, channels, typeof frame.playback_id === "string" ? frame.playback_id : null);
      if (this.protocol === 2) {
        if (rate !== (this.outputCodec === "opus" ? 48000 : 24000) || channels !== 1) { this.audioFailed(new Error("Invalid playback format.")); return; }
        if (typeof frame.stream_id !== "number" || !Number.isSafeInteger(frame.stream_id) || frame.stream_id > 0xffffffff || frame.stream_id <= this.outputStream) { this.audioFailed(new Error("Invalid playback stream.")); return; }
        this.outputStream = frame.stream_id;
        this.outputSequence = this.outputSamples = 0;
        const stream = this.outputStream;
        if (this.outputCodec === "opus") {
          try {
            this.decoder = new OpusOutput((samples) => {
              if (this.outputStream === stream && this.acceptingAudio) this.enqueueSamples([samples]);
            }, (error) => { if (this.outputStream === stream && this.acceptingAudio) this.audioFailed(error); });
          } catch (error) { this.audioFailed(error); return; }
        }
      }
      this.listener.onPlaying(true);
      return;
    }
    if (frame.type === "playback_done") {
      if ((frame.playback_id ?? null) !== this.playbackId) return;
      this.wireDone = true;
      const id = this.playbackId;
      if (this.decoder) {
        void this.decoder.finish().then(() => {
          if (this.playbackId === id) { this.acceptingAudio = false; this.reportProgress(); }
        }).catch((error) => { if (this.playbackId === id) this.audioFailed(error); });
      } else { this.acceptingAudio = false; this.reportProgress(); }
      return;
    }
    if (frame.type === "clear_playback") {
      if ((frame.playback_id ?? null) !== this.playbackId) return;
      this.stopPlayback();
      this.listener.onPlaying(false);
      return;
    }
    if (frame.type === "state" && typeof frame.floor === "string") {
      this.listener.onFloor(frame.floor);
      return;
    }
    if (frame.type === "transcript") this.listener.onTranscript(frame);
  }

  private beginPlayback(sampleRate: number, channels: number, playbackId: string | null): void {
    settle(this.audio?.resume());
    this.stopPlaybackSources();
    this.acceptingAudio = true;
    this.wireDone = false;
    this.playbackId = playbackId;
    this.playRate = sampleRate > 0 ? sampleRate : PHONE_OUTPUT_SAMPLE_RATE;
    this.playChannels = channels;
    this.nextTime = 0;
    this.lastReportedBytes = 0;
  }

  private receiveAudio(bytes: ArrayBuffer): void {
    const packet = unpackAudio(bytes);
    if (packet.stream !== this.outputStream || !this.acceptingAudio || this.wireDone) return;
    if (packet.sequence !== this.outputSequence++ || Math.abs(packet.timestamp - Math.floor(this.outputSamples * 1e6 / this.playRate)) > 1) throw new Error("Discontinuous playback audio.");
    this.outputSamples += packet.samples;
    if (this.decoder) this.decoder.write(packet);
    else {
      if (packet.discard || packet.payload.byteLength !== packet.samples * 2) throw new Error("Invalid PCM audio length.");
      this.enqueuePcm(packet.payload);
    }
  }

  private enqueuePcm(bytes: ArrayBuffer): void {
    if (this.acceptingAudio) this.enqueueSamples(pcm16ToChannelPlanes(bytes, this.playChannels));
  }

  private enqueueSamples(planes: Float32Array[]): void {
    const audio = this.audio;
    const frames = planes[0]?.length ?? 0;
    if (!this.acceptingAudio || !audio || !frames) return;
    if (this.nextTime - audio.currentTime > 1) { this.audioFailed(new VoiceAudioBackpressure("Audio playback is falling behind. Please try again.")); return; }
    const frameBytes = 2 * this.playChannels;
    const buffer = audio.createBuffer(this.playChannels, frames, this.playRate);
    for (let channel = 0; channel < this.playChannels; channel += 1) buffer.copyToChannel(Float32Array.from(planes[channel]), channel);
    const source = audio.createBufferSource();
    const playbackId = this.playbackId;
    source.buffer = buffer;
    source.connect(audio.destination);
    const startAt = Math.max(this.nextTime, audio.currentTime + 0.03);
    source.start(startAt);
    const duration = frames / this.playRate;
    this.sources.push(source);
    this.scheduled.push({ startAt, duration, bytes: frames * frameBytes });
    this.nextTime = startAt + duration;
    source.onended = () => {
      source.disconnect();
      if (this.playbackId !== playbackId) return;
      this.sources = this.sources.filter((item) => item !== source);
      this.reportProgress();
    };
  }

  private reportProgress(): void {
    const audio = this.audio;
    const socket = this.socket;
    if (!audio || !socket || socket.readyState !== WebSocket.OPEN) return;
    const played = this.playedBytes(audio.currentTime);
    if (played > this.lastReportedBytes) {
      this.lastReportedBytes = played;
      const progress = this.protocol === 2 ? {played_samples: played / (2 * this.playChannels), sample_rate: this.playRate} : {played_bytes: played};
      socket.send(JSON.stringify({ type: "playback_progress", ...progress, playback_id: this.playbackId }));
    }
    if (this.playbackId && !this.acceptingAudio && this.sources.length === 0 && !this.pending(audio.currentTime)) {
      socket.send(JSON.stringify({ type: "playback_finished", playback_id: this.playbackId }));
      this.playbackId = null;
      this.listener.onPlaying(false);
    } else if (!this.playbackId && !this.acceptingAudio && this.sources.length === 0) {
      this.listener.onPlaying(false);
    }
  }

  private playedBytes(now: number): number {
    let bytes = 0;
    for (const item of this.scheduled) {
      if (now >= item.startAt + item.duration) bytes += item.bytes;
      else if (now > item.startAt && item.duration > 0) {
        bytes += Math.floor(item.bytes * ((now - item.startAt) / item.duration));
      }
    }
    const frameBytes = 2 * this.playChannels;
    return bytes - (bytes % frameBytes);
  }

  private pending(now: number): boolean {
    return this.scheduled.some((item) => now < item.startAt + item.duration);
  }

  private stopCapture(): void {
    this.captureAbort?.();
    this.captureAbort = null;
    void this.captureStop?.(false);
    this.captureStop = null;
    this.captureFinishing = null;
    this.listener.onCapture?.(false);
  }

  private stopPlayback(): void {
    this.acceptingAudio = false;
    this.stopPlaybackSources();
  }

  private stopPlaybackSources(): void {
    this.decoder?.close();
    this.decoder = null;
    this.playbackId = null;
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // Already stopped.
      }
      source.disconnect();
    }
    this.sources = [];
    this.scheduled = [];
    this.nextTime = 0;
  }
}

function settle(work: Promise<unknown> | undefined): void {
  void work?.catch(() => undefined);
}

function createAudioContext(): AudioContext {
  const Ctor = window.AudioContext
    || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) throw new Error("This browser cannot capture or play audio.");
  try {
    return new Ctor({ sampleRate: 48000 });
  } catch {
    return new Ctor();
  }
}

function closeMessage(code: number, reason: string): string {
  if (reason) return reason;
  if (code === 1008) return "The voice session could not be started. Please try again.";
  if (code === 1013) return "Voice is temporarily unavailable. Please try again.";
  return "The voice connection closed.";
}

// Safe instanceof check in browsers without AudioWorklet support.
function AudioWorkletNodeSafe(): typeof AudioWorkletNode {
  return typeof AudioWorkletNode !== "undefined" ? AudioWorkletNode : class {} as typeof AudioWorkletNode;
}
