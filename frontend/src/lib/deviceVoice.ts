export const PHONE_DEVICE_ID = "phone-web";
export const PHONE_DEVICE_NAME = "Phone mic";
export const PHONE_OUTPUT_SAMPLE_RATE = 24000;

const STORAGE_PREFIX = "xagent.phone-voice.v1";

export interface VoiceHello {
  type: "hello";
  name: string;
  location: string;
  input_sample_rate: number;
  input_channels: 1;
  output_sample_rate: number;
  output_channels: 1;
  profile: "headset";
  echo_managed: true;
}

export interface VoiceReadyFrame {
  type: "ready";
  device_id?: string;
  room_name?: string;
  credential?: string;
  input_sample_rate?: number;
  output_sample_rate?: number;
  output_channels?: number;
}

export interface TranscriptLine {
  id: number;
  role: "user" | "assistant";
  text: string;
  pending: boolean;
}

export interface TranscriptFrame {
  role?: unknown;
  text?: unknown;
  delta?: unknown;
  final?: unknown;
}

type CredentialStore = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function credentialStorageKey(agentName: string): string {
  return `${STORAGE_PREFIX}:${agentName}:${PHONE_DEVICE_ID}`;
}

export function readStoredCredential(storage: Pick<Storage, "getItem">, agentName: string): string {
  if (!agentName) return "";
  try {
    const raw = storage.getItem(credentialStorageKey(agentName));
    if (!raw) return "";
    const parsed = JSON.parse(raw) as { deviceId?: unknown; credential?: unknown };
    if (parsed.deviceId !== PHONE_DEVICE_ID || typeof parsed.credential !== "string") return "";
    if (!parsed.credential.startsWith("dvc_")) return "";
    return parsed.credential;
  } catch {
    return "";
  }
}

export function writeStoredCredential(storage: CredentialStore, agentName: string, credential: string): void {
  if (!agentName) return;
  const key = credentialStorageKey(agentName);
  if (!credential) {
    storage.removeItem(key);
    return;
  }
  storage.setItem(key, JSON.stringify({ deviceId: PHONE_DEVICE_ID, credential }));
}

export function clampSampleRate(rate: number): number {
  if (!Number.isFinite(rate)) return 16000;
  const rounded = Math.round(rate);
  if (rounded < 8000) return 8000;
  if (rounded > 48000) return 48000;
  return rounded;
}

export function voiceHello(inputSampleRate: number): VoiceHello {
  return {
    type: "hello",
    name: PHONE_DEVICE_NAME,
    location: "",
    input_sample_rate: clampSampleRate(inputSampleRate),
    input_channels: 1,
    output_sample_rate: PHONE_OUTPUT_SAMPLE_RATE,
    output_channels: 1,
    profile: "headset",
    echo_managed: true,
  };
}

export function toDeviceWebSocketBase(apiUrl: string): string {
  const url = new URL(apiUrl);
  if (url.protocol === "http:") url.protocol = "ws:";
  else if (url.protocol === "https:") url.protocol = "wss:";
  else if (url.protocol !== "ws:" && url.protocol !== "wss:") {
    throw new Error("Agent API URL must use http, https, ws, or wss.");
  }
  url.username = "";
  url.password = "";
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/+$/, "");
}

export function deviceVoiceSocketUrl(apiUrl: string, deviceId: string, token: string): string {
  const base = toDeviceWebSocketBase(apiUrl);
  const url = new URL(`ws/devices/${encodeURIComponent(deviceId)}/voice`, `${base}/`);
  url.searchParams.set("token", token);
  return url.toString();
}

export function describeVoiceSocket(apiUrl: string, deviceId: string): string {
  const base = toDeviceWebSocketBase(apiUrl);
  return new URL(`ws/devices/${encodeURIComponent(deviceId)}/voice`, `${base}/`).toString();
}

export function assertSocketAllowedOnPage(socketUrl: string, pageProtocol: string): void {
  if (pageProtocol === "https:" && socketUrl.startsWith("ws:")) {
    throw new Error("This page is HTTPS, so the voice socket must use WSS. Point web.api_url at the https devices host.");
  }
}

export function floatToPcm16(samples: ArrayLike<number>): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, samples[i] || 0));
    const intSample = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
    view.setInt16(i * 2, intSample, true);
  }
  return buffer;
}

export function pcm16ToChannelPlanes(bytes: ArrayBuffer, channels = 1): Float32Array[] {
  const channelCount = channels === 2 ? 2 : 1;
  const view = new DataView(bytes);
  const frames = Math.floor(bytes.byteLength / (2 * channelCount));
  const planes = Array.from({ length: channelCount }, () => new Float32Array(frames));
  for (let frame = 0; frame < frames; frame += 1) {
    for (let channel = 0; channel < channelCount; channel += 1) {
      const sample = view.getInt16((frame * channelCount + channel) * 2, true);
      planes[channel][frame] = sample / 32768;
    }
  }
  return planes;
}

export function pcmRms(samples: ArrayLike<number>): number {
  if (!samples.length) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) {
    const sample = samples[i] || 0;
    sum += sample * sample;
  }
  return Math.sqrt(sum / samples.length);
}

export function resampleLinear(input: ArrayLike<number>, fromRate: number, toRate: number): Float32Array {
  if (!(fromRate > 0) || !(toRate > 0) || fromRate === toRate) {
    return input instanceof Float32Array ? input : Float32Array.from(input);
  }
  const outLength = Math.max(1, Math.round((input.length * toRate) / fromRate));
  const output = new Float32Array(outLength);
  const scale = fromRate / toRate;
  for (let i = 0; i < outLength; i += 1) {
    const position = i * scale;
    const index = Math.floor(position);
    const fraction = position - index;
    const start = input[index] || 0;
    const end = input[Math.min(index + 1, input.length - 1)] || start;
    output[i] = start + (end - start) * fraction;
  }
  return output;
}

export function applyTranscript(lines: TranscriptLine[], frame: TranscriptFrame, nextId: () => number): TranscriptLine[] {
  const role = frame.role === "assistant" || frame.role === "user" ? frame.role : null;
  if (!role) return lines;
  const text = typeof frame.text === "string" ? frame.text : "";
  const delta = typeof frame.delta === "string" ? frame.delta : "";
  const final = frame.final === true;
  const last = lines[lines.length - 1];
  if (role === "assistant" && !final && delta) {
    if (last && last.role === "assistant" && last.pending) {
      return [...lines.slice(0, -1), { ...last, text: last.text + delta }];
    }
    return trimLines([...lines, { id: nextId(), role, text: delta, pending: true }]);
  }
  if (final && text) {
    if (last && last.role === role && last.pending) {
      return trimLines([...lines.slice(0, -1), { ...last, text, pending: false }]);
    }
    return trimLines([...lines, { id: nextId(), role, text, pending: false }]);
  }
  return lines;
}

function trimLines(lines: TranscriptLine[]): TranscriptLine[] {
  return lines.length > 40 ? lines.slice(lines.length - 40) : lines;
}
