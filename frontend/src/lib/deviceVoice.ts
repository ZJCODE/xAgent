export interface VoiceReadyFrame {
  type: "ready";
  audio_protocol: 2;
  codec: "opus" | "pcm_s16le";
  input_sample_rate: 16000 | 48000;
  output_sample_rate: 24000 | 48000;
  input_channels: 1;
  output_channels: 1;
}

export function voiceHello(codec: "opus" | "pcm_s16le") {
  return {type: "hello", audio_protocol: 2, codec, name: "Browser microphone", location: "", profile: "headset", echo_managed: true};
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

export function browserVoiceSocketUrl(apiUrl: string, sessionId: string, token: string): string {
  const base = toDeviceWebSocketBase(apiUrl);
  const url = new URL(`ws/voice/${encodeURIComponent(sessionId)}`, `${base}/`);
  url.searchParams.set("token", token);
  return url.toString();
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
