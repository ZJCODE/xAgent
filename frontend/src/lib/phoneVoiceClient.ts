import {
  PHONE_OUTPUT_SAMPLE_RATE,
  assertSocketAllowedOnPage,
  clampSampleRate,
  floatToPcm16,
  pcm16ToChannelPlanes,
  pcmRms,
  resampleLinear,
  voiceHello,
  type VoiceReadyFrame,
} from "./deviceVoice";

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
  onClose: (info: { code: number; reason: string }) => void;
  onNotice: (message: string) => void;
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
  private captureStop: (() => void) | null = null;
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

  async connect(options: { socketUrl: string; pageProtocol: string }): Promise<VoiceReadyFrame> {
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
          this.handshakeReject = null;
          socket.close();
          reject(new Error("Voice handshake timed out."));
        }, 8000);
        socket.onopen = () => {
          settle(audio.resume());
          socket.send(JSON.stringify(voiceHello(audio.sampleRate)));
        };
        socket.onmessage = (event) => {
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
      this.ready = true;
      socket.onmessage = (event) => this.handleFrame(event);
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
    if (!holding) {
      this.captureToken += 1;
      this.stopCapture();
      this.listener.onLevel(0);
      return;
    }
    if (!this.live || this.captureStop) return;
    this.prime();
    const audio = this.audio;
    if (!audio) return;
    const token = this.captureToken + 1;
    this.captureToken = token;
    let stream: MediaStream | null = null;
    try {
      // Start inside the pointer gesture. prime() already resumed audio there;
      // a suspended AudioContext must not delay handling the permission result.
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
      if (token !== this.captureToken || !this.live) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      const source = audio.createMediaStreamSource(stream);
      const processor = audio.createScriptProcessor(2048, 1, 1);
      const mute = audio.createGain();
      mute.gain.value = 0;
      processor.onaudioprocess = (event) => {
        if (token !== this.captureToken || !this.socket || this.socket.readyState !== WebSocket.OPEN) return;
        const native = event.inputBuffer.getChannelData(0);
        const samples = event.inputBuffer.sampleRate === this.declaredRate
          ? native
          : resampleLinear(native, event.inputBuffer.sampleRate, this.declaredRate);
        if (this.socket.bufferedAmount < MAX_BUFFERED_BYTES) this.socket.send(floatToPcm16(samples));
        this.listener.onLevel(Math.min(1, pcmRms(samples) * 4));
      };
      source.connect(processor);
      processor.connect(mute);
      mute.connect(audio.destination);
      const activeStream = stream;
      this.captureStop = () => {
        processor.onaudioprocess = null;
        source.disconnect();
        processor.disconnect();
        mute.disconnect();
        activeStream.getTracks().forEach((track) => track.stop());
      };
    } catch (error) {
      stream?.getTracks().forEach((track) => track.stop());
      if (token !== this.captureToken || !this.live) return;
      if (token === this.captureToken) this.captureToken += 1;
      const name = error instanceof DOMException ? error.name : "";
      this.listener.onNotice(
        name === "NotAllowedError" || name === "PermissionDeniedError"
          ? "Microphone permission was denied. Allow the mic for this page and hold to talk again."
          : "This browser could not open the microphone. Use HTTPS or localhost.",
      );
    }
  }

  private closeSocket(): void {
    this.captureToken += 1;
    window.clearTimeout(this.handshakeTimer);
    window.clearInterval(this.pingTimer);
    window.clearInterval(this.progressTimer);
    this.pingTimer = 0;
    this.progressTimer = 0;
    this.ready = false;
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
      this.enqueuePcm(event.data);
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
    if (frame.type === "playback_start") {
      const rate = typeof frame.sample_rate === "number" ? frame.sample_rate : PHONE_OUTPUT_SAMPLE_RATE;
      const channels = frame.channels === 2 ? 2 : 1;
      this.beginPlayback(rate, channels);
      this.listener.onPlaying(true);
      return;
    }
    if (frame.type === "playback_done") {
      this.acceptingAudio = false;
      return;
    }
    if (frame.type === "clear_playback") {
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

  private beginPlayback(sampleRate: number, channels: number): void {
    settle(this.audio?.resume());
    this.stopPlaybackSources();
    this.acceptingAudio = true;
    this.playRate = sampleRate > 0 ? sampleRate : PHONE_OUTPUT_SAMPLE_RATE;
    this.playChannels = channels;
    this.nextTime = 0;
    this.lastReportedBytes = 0;
  }

  private enqueuePcm(bytes: ArrayBuffer): void {
    const audio = this.audio;
    if (!this.acceptingAudio || !audio) return;
    const frameBytes = 2 * this.playChannels;
    const frames = Math.floor(bytes.byteLength / frameBytes);
    if (frames <= 0) return;
    const buffer = audio.createBuffer(this.playChannels, frames, this.playRate);
    const planes = pcm16ToChannelPlanes(bytes, this.playChannels);
    for (let channel = 0; channel < this.playChannels; channel += 1) {
      const samples = new Float32Array(planes[channel].length);
      samples.set(planes[channel]);
      buffer.copyToChannel(samples, channel);
    }
    const source = audio.createBufferSource();
    source.buffer = buffer;
    source.connect(audio.destination);
    const startAt = Math.max(this.nextTime, audio.currentTime + 0.03);
    source.start(startAt);
    const duration = frames / this.playRate;
    this.sources.push(source);
    this.scheduled.push({ startAt, duration, bytes: frames * frameBytes });
    this.nextTime = startAt + duration;
    source.onended = () => {
      this.sources = this.sources.filter((item) => item !== source);
      if (!this.acceptingAudio && !this.pending(audio.currentTime)) this.listener.onPlaying(false);
    };
  }

  private reportProgress(): void {
    const audio = this.audio;
    const socket = this.socket;
    if (!audio || !socket || socket.readyState !== WebSocket.OPEN) return;
    const played = this.playedBytes(audio.currentTime);
    if (played <= this.lastReportedBytes) {
      if (!this.acceptingAudio && !this.pending(audio.currentTime)) this.listener.onPlaying(false);
      return;
    }
    this.lastReportedBytes = played;
    socket.send(JSON.stringify({ type: "playback_progress", played_bytes: played }));
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
    this.captureStop?.();
    this.captureStop = null;
  }

  private stopPlayback(): void {
    this.acceptingAudio = false;
    this.stopPlaybackSources();
  }

  private stopPlaybackSources(): void {
    for (const source of this.sources) {
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
    return new Ctor({ sampleRate: 16000 });
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
