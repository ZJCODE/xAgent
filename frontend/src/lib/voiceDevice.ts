/**
 * Browser client for the existing device voice socket.
 *
 * The page speaks `/ws/devices/{id}/voice` the same way an ESP does: hello,
 * PCM16 up, TTS PCM down, and playback_progress. Browsers cannot set the
 * Authorization header, so the first frame is an auth envelope consumed by
 * the web client relay. It is not part of the agent voice protocol.
 */

export const VOICE_INPUT_RATE = 16_000;
export const VOICE_OUTPUT_RATE = 24_000;
export const VOICE_FRAME_SAMPLES = 320;
const SPEECH_RMS = 800;
const QUIET_AFTER_SPEECH_MS = 1_400;
const ENDPOINT_SILENCE_MS = 1_500;

const STORAGE_KEY = "xagent_web_voice_devices";
const DEVICE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

export interface StoredWebVoiceDevice {
  agent: string;
  deviceId: string;
  credential: string;
}

interface VoiceDeviceStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface VoiceTranscriptEvent {
  role: "user" | "assistant";
  text: string;
  final: boolean;
}

export interface VoiceTranscriptLine {
  id: string;
  role: "user" | "assistant";
  text: string;
  pending: boolean;
}

export type VoiceServerMessage =
  | {
      kind: "ready";
      deviceId: string;
      roomName: string;
      credential?: string;
      outputSampleRate: number;
      outputChannels: number;
    }
  | { kind: "state"; floor: string }
  | { kind: "transcript"; role: "user" | "assistant"; text: string; final: boolean }
  | { kind: "playback_start"; sampleRate: number; channels: number }
  | { kind: "playback_done" }
  | { kind: "clear_playback" }
  | { kind: "pong" }
  | { kind: "ignore" };

export interface PcmPlayer {
  begin(sampleRate: number, channels: number): void;
  push(pcm: ArrayBuffer): void;
  clear(): void;
  resume(): void;
}

export interface VoiceSocket {
  binaryType: BinaryType;
  readyState: number;
  send(data: string | ArrayBuffer): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror: ((event: Event) => void) | null;
  onclose: ((event: { code?: number; reason?: string }) => void) | null;
}

export interface VoiceConnectionListener {
  onPhase(phase: "connecting" | "live" | "idle"): void;
  onFloor(floor: string): void;
  onTranscript(event: VoiceTranscriptEvent): void;
  onReady(info: { deviceId: string; roomName: string; credential?: string }): void;
  onClose(info: { code: number; reason: string }): void;
  onTalking(talking: boolean): void;
  onSpeech(heard: boolean): void;
  onTurn?(): void;
  onError(message: string): void;
}

interface VoiceTimers {
  setInterval(fn: () => void, ms: number): number;
  clearInterval(id: number): void;
}

const SOCKET_OPEN = 1;

export function createWebVoiceDeviceId(): string {
  const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  let deviceId = "web";
  for (const byte of bytes) deviceId += alphabet[byte % alphabet.length];
  return deviceId;
}

export function voiceSocketPath(deviceId: string): string {
  return `/ws/devices/${encodeURIComponent(deviceId)}/voice`;
}

export function normalizeDeviceToken(raw: string): string {
  const trimmed = raw.trim();
  return /^bearer\s+/i.test(trimmed) ? trimmed.replace(/^bearer\s+/i, "").trim() : trimmed;
}

export function voiceAuthFrame(token: string): { type: "auth"; token: string } {
  return { type: "auth", token };
}

export function voiceHello(userId: string): Record<string, unknown> {
  const cleaned = userId.trim().slice(0, 128) || "web_user";
  return {
    type: "hello",
    name: "Web browser",
    location: "browser",
    input_sample_rate: VOICE_INPUT_RATE,
    input_channels: 1,
    output_sample_rate: VOICE_OUTPUT_RATE,
    output_channels: 1,
    profile: "headset",
    echo_managed: true,
    interruptions: true,
    user_id: cleaned,
  };
}

export function voiceFloorLabel(floor: string): string {
  switch (floor) {
    case "listening":
      return "Listening";
    case "thinking":
      return "Thinking";
    case "speaking":
      return "Speaking";
    default:
      return floor ? floor : "Connected";
  }
}

export function parseVoiceServerMessage(raw: string): VoiceServerMessage {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return { kind: "ignore" };
  }
  if (!data || typeof data !== "object") return { kind: "ignore" };
  const record = data as Record<string, unknown>;
  switch (record.type) {
    case "ready":
      return {
        kind: "ready",
        deviceId: typeof record.device_id === "string" ? record.device_id : "",
        roomName: typeof record.room_name === "string" ? record.room_name : "",
        credential: typeof record.credential === "string" ? record.credential : undefined,
        outputSampleRate: numberOr(record.output_sample_rate, VOICE_OUTPUT_RATE),
        outputChannels: numberOr(record.output_channels, 1),
      };
    case "state":
      return { kind: "state", floor: typeof record.floor === "string" ? record.floor : "" };
    case "transcript": {
      const role = record.role === "user" || record.role === "assistant" ? record.role : null;
      if (!role) return { kind: "ignore" };
      const text = typeof record.text === "string" ? record.text : typeof record.delta === "string" ? record.delta : "";
      return { kind: "transcript", role, text, final: record.final !== false };
    }
    case "playback_start":
      return {
        kind: "playback_start",
        sampleRate: numberOr(record.sample_rate, VOICE_OUTPUT_RATE),
        channels: numberOr(record.channels, 1),
      };
    case "playback_done":
      return { kind: "playback_done" };
    case "clear_playback":
      return { kind: "clear_playback" };
    case "pong":
      return { kind: "pong" };
    default:
      return { kind: "ignore" };
  }
}

export function applyVoiceTranscript(
  lines: VoiceTranscriptLine[],
  event: VoiceTranscriptEvent,
  makeId: () => string,
): VoiceTranscriptLine[] {
  if (!event.text && !event.final) return lines;
  const next = lines.slice();
  const last = next[next.length - 1];
  if (event.role === "assistant" && !event.final) {
    if (last?.role === "assistant" && last.pending) {
      next[next.length - 1] = { ...last, text: last.text + event.text };
    } else {
      next.push({ id: makeId(), role: "assistant", text: event.text, pending: true });
    }
  } else if (event.role === "assistant" && event.final) {
    if (last?.role === "assistant" && last.pending) {
      next[next.length - 1] = { ...last, text: event.text, pending: false };
    } else if (event.text) {
      next.push({ id: makeId(), role: "assistant", text: event.text, pending: false });
    }
  } else if (event.text) {
    next.push({ id: makeId(), role: event.role, text: event.text, pending: false });
  }
  return next.slice(-12);
}

export function loadWebVoiceDevice(storage: VoiceDeviceStore, agent: string): StoredWebVoiceDevice | null {
  const key = agent.trim();
  if (!key) return null;
  const record = readStore(storage)[key];
  if (!record || !DEVICE_ID_PATTERN.test(record.deviceId) || !isDeviceToken(record.credential)) return null;
  return { agent: key, deviceId: record.deviceId, credential: record.credential };
}

export function saveWebVoiceDevice(storage: VoiceDeviceStore, device: StoredWebVoiceDevice): void {
  const agent = device.agent.trim();
  if (!agent || !DEVICE_ID_PATTERN.test(device.deviceId) || !isDeviceToken(device.credential)) return;
  const all = readStore(storage);
  all[agent] = { deviceId: device.deviceId, credential: device.credential };
  storage.setItem(STORAGE_KEY, JSON.stringify(all));
}

export function clearWebVoiceDevice(storage: VoiceDeviceStore, agent: string): void {
  const key = agent.trim();
  if (!key) return;
  const all = readStore(storage);
  delete all[key];
  storage.setItem(STORAGE_KEY, JSON.stringify(all));
}

export class VoiceDeviceConnection {
  private socket: VoiceSocket | null = null;
  private phase: "idle" | "connecting" | "live" = "idle";
  private talking = false;
  private endingTalk = false;
  private talkGeneration = 0;
  private captureStop: (() => void) | null = null;
  private acceptingAudio = false;
  private reportingProgress = false;
  private lastProgress = 0;
  private pingTimer = 0;
  private silenceTimer = 0;
  private silenceFramesLeft = 0;
  private heardSpeech = false;
  private quietMs = 0;
  private draining = false;
  private intentional = false;

  constructor(
    private readonly deps: {
      socketUrl(path: string): string;
      createSocket(url: string): VoiceSocket;
      player: PcmPlayer;
      startCapture(onPcm: (pcm: ArrayBuffer) => void): Promise<() => void>;
      listener: VoiceConnectionListener;
      timers?: VoiceTimers;
    },
  ) {}

  connect(input: { deviceId: string; token: string; userId: string }): void {
    const token = normalizeDeviceToken(input.token);
    const deviceId = input.deviceId.trim();
    if (!DEVICE_ID_PATTERN.test(deviceId) || !isDeviceToken(token)) {
      this.deps.listener.onError("A device id and pairing code or credential are required.");
      return;
    }
    this.disconnect();
    this.intentional = false;
    this.phase = "connecting";
    this.deps.listener.onPhase("connecting");
    let socket: VoiceSocket;
    try {
      socket = this.deps.createSocket(this.deps.socketUrl(voiceSocketPath(deviceId)));
    } catch (error) {
      this.phase = "idle";
      this.deps.listener.onPhase("idle");
      this.deps.listener.onError(error instanceof Error ? error.message : "Could not open the voice socket.");
      return;
    }
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      socket.send(JSON.stringify(voiceAuthFrame(token)));
      socket.send(JSON.stringify(voiceHello(input.userId)));
    };
    socket.onmessage = (event) => {
      this.handleMessage(socket, event.data);
    };
    socket.onerror = () => {
      /* onclose carries the reason */
    };
    socket.onclose = (event) => {
      if (this.socket !== socket) return;
      this.finishSocket(false, event.code ?? 1000, event.reason || "");
    };
  }

  disconnect(): void {
    const socket = this.socket;
    this.intentional = true;
    this.socket = null;
    this.stopCapture();
    this.stopPing();
    this.stopEndpointSilence();
    this.acceptingAudio = false;
    this.reportingProgress = false;
    this.deps.player.clear();
    if (socket && socket.readyState === SOCKET_OPEN) {
      try {
        socket.send(JSON.stringify({ type: "stop" }));
      } catch {
        /* already closing */
      }
    }
    try {
      socket?.close();
    } catch {
      /* ignore */
    }
    if (this.phase !== "idle") {
      this.phase = "idle";
      this.deps.listener.onPhase("idle");
    }
  }

  async beginTalk(): Promise<void> {
    if (this.phase !== "live" || this.talking) return;
    this.stopEndpointSilence();
    this.heardSpeech = false;
    this.quietMs = 0;
    this.deps.listener.onSpeech(false);
    this.talking = true;
    this.deps.listener.onTalking(true);
    const generation = ++this.talkGeneration;
    try {
      const stop = await this.deps.startCapture((pcm) => this.sendPcm(pcm));
      if (generation !== this.talkGeneration || this.socket === null) {
        stop();
        return;
      }
      this.captureStop = stop;
    } catch (error) {
      if (generation !== this.talkGeneration) return;
      this.talking = false;
      this.deps.listener.onTalking(false);
      this.deps.listener.onError(error instanceof Error ? error.message : "Microphone capture failed.");
    }
  }

  endTalk(): void {
    if (this.endingTalk) return;
    const wasCapturing = this.talking;
    const heard = this.heardSpeech;
    this.endingTalk = true;
    this.talkGeneration += 1;
    this.heardSpeech = false;
    this.quietMs = 0;
    this.deps.listener.onSpeech(false);
    try {
      this.stopCapture();
      if (wasCapturing) {
        this.flushEndpointSilence();
        if (heard) this.deps.listener.onTurn?.();
      }
    } finally {
      this.endingTalk = false;
    }
  }

  notePlaybackProgress(playedBytes: number): void {
    const played = Math.floor(playedBytes);
    if (!Number.isFinite(played) || played <= this.lastProgress || !this.reportingProgress) return;
    this.lastProgress = played;
    this.sendControl({ type: "playback_progress", played_bytes: played });
  }

  private stopCapture(): void {
    const stop = this.captureStop;
    this.captureStop = null;
    if (this.talking) {
      this.talking = false;
      this.deps.listener.onTalking(false);
    }
    this.draining = true;
    try {
      stop?.();
    } finally {
      this.draining = false;
    }
  }

  private sendPcm(pcm: ArrayBuffer): void {
    if ((!this.talking && !this.draining) || !pcm.byteLength || this.socket?.readyState !== SOCKET_OPEN) return;
    this.socket.send(pcm);
    if (this.talking) this.observeUtterance(pcm);
  }

  private observeUtterance(pcm: ArrayBuffer): void {
    const samples = new Int16Array(pcm);
    if (!samples.length) return;
    let energy = 0;
    for (let index = 0; index < samples.length; index += 1) energy += samples[index] * samples[index];
    const rms = Math.sqrt(energy / samples.length);
    const durationMs = (samples.length / VOICE_INPUT_RATE) * 1000;
    if (rms >= SPEECH_RMS) {
      this.quietMs = 0;
      if (!this.heardSpeech) {
        this.heardSpeech = true;
        this.deps.listener.onSpeech(true);
      }
      return;
    }
    if (!this.heardSpeech) return;
    this.quietMs += durationMs;
    if (this.quietMs >= QUIET_AFTER_SPEECH_MS) this.endTalk();
  }

  private flushEndpointSilence(): void {
    this.stopEndpointSilence();
    const frameBytes = VOICE_FRAME_SAMPLES * 2;
    this.silenceFramesLeft = Math.ceil(ENDPOINT_SILENCE_MS / ((VOICE_FRAME_SAMPLES / VOICE_INPUT_RATE) * 1000));
    const timers = this.deps.timers ?? defaultTimers();
    this.silenceTimer = timers.setInterval(() => {
      if (this.talking || this.silenceFramesLeft <= 0 || this.socket?.readyState !== SOCKET_OPEN) {
        this.stopEndpointSilence();
        return;
      }
      this.silenceFramesLeft -= 1;
      this.socket.send(new ArrayBuffer(frameBytes));
      if (this.silenceFramesLeft <= 0) this.stopEndpointSilence();
    }, (VOICE_FRAME_SAMPLES / VOICE_INPUT_RATE) * 1000);
  }

  private stopEndpointSilence(): void {
    if (!this.silenceTimer) return;
    (this.deps.timers ?? defaultTimers()).clearInterval(this.silenceTimer);
    this.silenceTimer = 0;
    this.silenceFramesLeft = 0;
  }

  private sendControl(payload: { type: string; played_bytes?: number }): void {
    if (this.socket?.readyState !== SOCKET_OPEN) return;
    this.socket.send(JSON.stringify(payload));
  }

  private handleMessage(socket: VoiceSocket, data: unknown): void {
    if (this.socket !== socket) return;
    if (typeof data === "string") {
      this.handleText(data);
      return;
    }
    if (data instanceof ArrayBuffer) {
      this.pushAudio(socket, data);
      return;
    }
    if (ArrayBuffer.isView(data)) {
      const copy = new ArrayBuffer(data.byteLength);
      new Uint8Array(copy).set(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
      this.pushAudio(socket, copy);
      return;
    }
    if (typeof Blob !== "undefined" && data instanceof Blob) {
      void data.arrayBuffer().then((bytes) => this.pushAudio(socket, bytes));
    }
  }

  private pushAudio(socket: VoiceSocket, bytes: ArrayBuffer): void {
    if (this.socket !== socket || !this.acceptingAudio || bytes.byteLength < 2) return;
    this.deps.player.push(bytes);
  }

  private handleText(raw: string): void {
    const message = parseVoiceServerMessage(raw);
    switch (message.kind) {
      case "ready":
        this.phase = "live";
        this.deps.player.resume();
        this.deps.listener.onReady({
          deviceId: message.deviceId,
          roomName: message.roomName,
          credential: message.credential,
        });
        this.deps.listener.onPhase("live");
        this.deps.listener.onFloor("listening");
        this.startPing();
        return;
      case "state":
        this.deps.listener.onFloor(message.floor);
        return;
      case "transcript":
        this.deps.listener.onTranscript({ role: message.role, text: message.text, final: message.final });
        return;
      case "playback_start":
        this.lastProgress = 0;
        this.acceptingAudio = true;
        this.reportingProgress = true;
        this.deps.player.resume();
        this.deps.player.begin(message.sampleRate, message.channels);
        return;
      case "playback_done":
        this.acceptingAudio = false;
        return;
      case "clear_playback":
        this.deps.player.clear();
        this.acceptingAudio = false;
        this.reportingProgress = false;
        return;
      default:
        return;
    }
  }

  private startPing(): void {
    this.stopPing();
    const timers = this.deps.timers ?? defaultTimers();
    this.pingTimer = timers.setInterval(() => {
      this.sendControl({ type: "ping" });
    }, 15_000);
  }

  private stopPing(): void {
    if (!this.pingTimer) return;
    (this.deps.timers ?? defaultTimers()).clearInterval(this.pingTimer);
    this.pingTimer = 0;
  }

  private finishSocket(intentional: boolean, code: number, reason: string): void {
    this.socket = null;
    this.stopCapture();
    this.stopPing();
    this.stopEndpointSilence();
    this.acceptingAudio = false;
    this.reportingProgress = false;
    this.deps.player.clear();
    this.phase = "idle";
    this.deps.listener.onPhase("idle");
    if (intentional || this.intentional) return;
    this.deps.listener.onClose({ code, reason: reason || closeFallback(code) });
  }
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback;
}

function isDeviceToken(token: string): boolean {
  return token.length > 0 && token.length <= 256 && ![...token].some((ch) => ch.charCodeAt(0) < 33 || ch.charCodeAt(0) > 126);
}

function readStore(storage: VoiceDeviceStore): Record<string, { deviceId: string; credential: string }> {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as Record<string, { deviceId: string; credential: string }>;
  } catch {
    return {};
  }
}

function closeFallback(code: number): string {
  if (code === 1008) return "Voice authentication failed. Pair this browser again.";
  if (code === 1013) return "Voice is unavailable. Connect the API channel and try again.";
  return "Voice connection closed.";
}

function defaultTimers(): VoiceTimers {
  return {
    setInterval: (fn, ms) => globalThis.setInterval(fn, ms) as unknown as number,
    clearInterval: (id) => globalThis.clearInterval(id),
  };
}
