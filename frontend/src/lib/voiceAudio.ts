import { VOICE_INPUT_RATE } from "./voiceDevice";
import { bytesPlayedAt, createPcm16Downsampler, pcm16ToMonoFloat } from "./voicePcm";

const WORKLET_SOURCE = `
class XagentVoiceCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("xagent-voice-capture", XagentVoiceCapture);
`;

const workletReady = new WeakSet<AudioContext>();

export class PcmStreamPlayer {
  private segments: Array<{ start: number; duration: number; bytes: number; node: AudioBufferSourceNode }> = [];
  private nextTime = 0;
  private active = false;
  private sampleRate = 24_000;
  private channels = 1;
  private timer = 0;

  constructor(
    private readonly ctx: AudioContext,
    private readonly onProgress: (playedBytes: number) => void,
  ) {}

  resume(): void {
    void this.ctx.resume();
  }

  begin(sampleRate: number, channels: number): void {
    this.stopSources();
    this.sampleRate = sampleRate > 0 ? sampleRate : 24_000;
    this.channels = channels > 0 ? channels : 1;
    this.nextTime = this.ctx.currentTime;
    this.active = true;
    this.restartTimer();
  }

  push(pcm: ArrayBuffer): void {
    if (!this.active || pcm.byteLength < 2) return;
    const floats = pcm16ToMonoFloat(pcm, this.channels);
    if (!floats.length) return;
    const buffer = this.ctx.createBuffer(1, floats.length, this.sampleRate);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < floats.length; index += 1) channel[index] = floats[index];
    const node = this.ctx.createBufferSource();
    node.buffer = buffer;
    node.connect(this.ctx.destination);
    const start = Math.max(this.nextTime, this.ctx.currentTime);
    node.start(start);
    const duration = buffer.duration;
    this.nextTime = start + duration;
    this.segments.push({ start, duration, bytes: pcm.byteLength - (pcm.byteLength % 2), node });
  }

  clear(): void {
    this.report();
    this.stopSources();
    this.active = false;
    this.restartTimer();
  }

  close(): void {
    this.clear();
  }

  private report(): void {
    if (!this.active && !this.segments.length) return;
    const played = bytesPlayedAt(this.segments, this.ctx.currentTime);
    if (played > 0) this.onProgress(played);
  }

  private restartTimer(): void {
    if (this.timer) window.clearInterval(this.timer);
    this.timer = 0;
    if (!this.active) return;
    this.timer = window.setInterval(() => this.report(), 100);
  }

  private stopSources(): void {
    if (this.timer) window.clearInterval(this.timer);
    this.timer = 0;
    for (const segment of this.segments) {
      try {
        segment.node.stop();
      } catch {
        /* already stopped */
      }
      segment.node.disconnect();
    }
    this.segments = [];
    this.active = false;
  }
}

export async function startMicrophoneCapture(
  ctx: AudioContext,
  onFloats: (samples: Float32Array) => void,
): Promise<() => void> {
  if (!window.isSecureContext) {
    throw new Error("Microphone needs HTTPS or localhost.");
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("This browser cannot capture microphone audio.");
  }
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: { ideal: 1 },
        echoCancellation: { ideal: true },
        noiseSuppression: { ideal: true },
        autoGainControl: { ideal: true },
      },
      video: false,
    });
  } catch (error) {
    if (error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "SecurityError")) {
      throw new Error("Microphone permission was blocked.");
    }
    throw error instanceof Error ? error : new Error("Microphone capture failed.");
  }

  const source = ctx.createMediaStreamSource(stream);
  const mute = ctx.createGain();
  mute.gain.value = 0;
  let worklet: AudioWorkletNode | null = null;
  let processor: ScriptProcessorNode | null = null;
  try {
    await ensureWorklet(ctx);
    worklet = new AudioWorkletNode(ctx, "xagent-voice-capture");
    worklet.port.onmessage = (event) => {
      const data = event.data;
      if (data instanceof Float32Array) onFloats(data);
    };
    source.connect(worklet);
    worklet.connect(mute);
  } catch {
    processor = ctx.createScriptProcessor(2048, 1, 1);
    processor.onaudioprocess = (event) => {
      onFloats(event.inputBuffer.getChannelData(0));
    };
    source.connect(processor);
    processor.connect(mute);
  }
  mute.connect(ctx.destination);

  return () => {
    worklet?.port.close();
    worklet?.disconnect();
    processor?.disconnect();
    source.disconnect();
    mute.disconnect();
    for (const track of stream.getTracks()) track.stop();
  };
}

export function capturePcm16(ctx: AudioContext, onPcm: (pcm: ArrayBuffer) => void): Promise<() => void> {
  const down = createPcm16Downsampler(ctx.sampleRate, VOICE_INPUT_RATE, onPcm);
  return startMicrophoneCapture(ctx, (samples) => down.push(samples)).then((stopMic) => () => {
    const partial = down.takePartial();
    if (partial) onPcm(partial);
    stopMic();
  });
}

async function ensureWorklet(ctx: AudioContext): Promise<void> {
  if (!ctx.audioWorklet) throw new Error("AudioWorklet is unavailable");
  if (workletReady.has(ctx)) return;
  const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
  try {
    await ctx.audioWorklet.addModule(url);
    workletReady.add(ctx);
  } finally {
    URL.revokeObjectURL(url);
  }
}
