/** WS audio v2: media time, packet boundaries and optional native Opus codecs. */
export type AudioCodec = "opus" | "pcm_s16le";
export const OPUS_RATE = 48000;
export const FRAME_SAMPLES = 960;
export const AUDIO_HEADER_BYTES = 28;
export const OPUS_ENCODER_CONFIG: AudioEncoderConfig = {
  codec: "opus", sampleRate: OPUS_RATE, numberOfChannels: 1, bitrate: 24000,
  opus: { format: "opus", frameDuration: 20000, usedtx: false, useinbandfec: false },
};
export const OPUS_DECODER_CONFIG: AudioDecoderConfig = { codec: "opus", sampleRate: OPUS_RATE, numberOfChannels: 1 };

export class VoiceCodecFailure extends Error {}
export class VoiceAudioBackpressure extends Error {}
export interface AudioPacket {
  stream: number;
  sequence: number;
  timestamp: number;
  samples: number;
  discard: number;
  payload: ArrayBuffer;
}

export function packAudio(packet: AudioPacket): ArrayBuffer {
  const buffer = new ArrayBuffer(AUDIO_HEADER_BYTES + packet.payload.byteLength);
  const view = new DataView(buffer);
  view.setUint32(0, 0x58415632);
  view.setUint32(4, packet.stream);
  view.setUint32(8, packet.sequence);
  view.setBigUint64(12, BigInt(packet.timestamp));
  view.setUint32(20, packet.samples);
  view.setUint32(24, packet.discard);
  new Uint8Array(buffer, AUDIO_HEADER_BYTES).set(new Uint8Array(packet.payload));
  return buffer;
}

export function unpackAudio(buffer: ArrayBuffer): AudioPacket {
  if (buffer.byteLength <= AUDIO_HEADER_BYTES) throw new Error("Missing audio packet payload.");
  const view = new DataView(buffer);
  const samples = view.getUint32(20), discard = view.getUint32(24);
  const timestamp = Number(view.getBigUint64(12));
  if (view.getUint32(0) !== 0x58415632 || !view.getUint32(4) || !samples || samples + discard > 5760 || !Number.isSafeInteger(timestamp)) {
    throw new Error("Invalid audio packet header.");
  }
  return {stream: view.getUint32(4), sequence: view.getUint32(8), timestamp, samples, discard, payload: buffer.slice(AUDIO_HEADER_BYTES)};
}

export async function supportedCodecs(worklet: boolean, forcePcm: boolean): Promise<{input: AudioCodec[]; output: AudioCodec[]}> {
  let encode = false, decode = false;
  if (!forcePcm) {
    if (worklet && typeof AudioEncoder !== "undefined" && typeof AudioData !== "undefined") {
      try { encode = Boolean((await AudioEncoder.isConfigSupported(OPUS_ENCODER_CONFIG)).supported); } catch { /* PCM fallback */ }
    }
    if (typeof AudioDecoder !== "undefined") {
      try { decode = Boolean((await AudioDecoder.isConfigSupported(OPUS_DECODER_CONFIG)).supported); } catch { /* PCM fallback */ }
    }
  }
  return {input: encode ? ["opus", "pcm_s16le"] : ["pcm_s16le"], output: decode ? ["opus", "pcm_s16le"] : ["pcm_s16le"]};
}

/** Encoder header supplies pre-skip; wire samples exclude priming and padded tail. */
export class OpusInput {
  private encoder: AudioEncoder;
  private total = 0;
  private rawSamples = 0;
  private sequence = 0;
  private preSkip: number | null = null;
  private closed = false;
  constructor(private stream: number, private output: (packet: AudioPacket) => void, private failure: (error: Error) => void) {
    try {
      this.encoder = new AudioEncoder({output: (chunk, metadata) => {
        if (this.closed) return;
        try {
          if (metadata?.decoderConfig?.description) {
            const description = metadata.decoderConfig.description;
            const bytes = ArrayBuffer.isView(description)
              ? new Uint8Array(description.buffer, description.byteOffset, description.byteLength) : new Uint8Array(description);
            if (bytes.length < 19 || String.fromCharCode(...bytes.slice(0, 8)) !== "OpusHead" || bytes[9] !== 1) throw new Error("Invalid Opus initialization.");
            this.preSkip = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint16(10, true);
          }
          if (this.preSkip === null) throw new Error("Missing Opus initialization.");
          const payload = new ArrayBuffer(chunk.byteLength);
          chunk.copyTo(payload);
          const toc = new Uint8Array(payload);
          const config = toc[0] >> 3;
          const frameSamples = config >= 16 ? 120 << (config & 3) : config >= 12 ? 480 << (config & 1) : [480, 960, 1920, 2880][config & 3];
          const count = (toc[0] & 3) === 0 ? 1 : (toc[0] & 3) === 3 ? toc[1] & 63 : 2;
          const duration = frameSamples * count;
          if (!count || duration > 5760) throw new Error("Invalid Opus packet duration.");
          const start = this.rawSamples - this.preSkip;
          this.rawSamples += duration;
          const first = Math.max(0, start), end = Math.min(this.total, start + duration);
          if (end > first) {
            this.output({stream: this.stream, sequence: this.sequence++, timestamp: Math.floor(first * 1e6 / OPUS_RATE),
              samples: end - first, discard: Math.max(0, -start), payload});
          }
        } catch (error) { this.failure(error instanceof VoiceAudioBackpressure ? error : new VoiceCodecFailure(String(error))); }
      }, error: (error) => { if (!this.closed) this.failure(new VoiceCodecFailure(error.message)); }});
      this.encoder.configure(OPUS_ENCODER_CONFIG);
    } catch (error) { throw new VoiceCodecFailure(String(error)); }
  }
  write(samples: Float32Array): void {
    if (this.closed) return;
    if (this.encoder.encodeQueueSize > 25) throw new VoiceAudioBackpressure("Audio encoding is falling behind. Please try again.");
    const data = new AudioData({format: "f32-planar", sampleRate: OPUS_RATE, numberOfChannels: 1,
      numberOfFrames: samples.length, timestamp: Math.floor(this.total * 1e6 / OPUS_RATE), data: Float32Array.from(samples)});
    this.total += samples.length;
    try { this.encoder.encode(data); } catch (error) { throw new VoiceCodecFailure(String(error)); }
    finally { data.close(); }
  }
  async finish(): Promise<void> {
    if (!this.closed) {
      try { await this.encoder.flush(); } catch (error) { if (!this.closed) throw new VoiceCodecFailure(String(error)); }
    }
  }
  close(): void { this.closed = true; if (this.encoder.state !== "closed") this.encoder.close(); }
}

export class OpusOutput {
  private decoder: AudioDecoder;
  private pending: AudioPacket[] = [];
  private closed = false;
  constructor(private output: (samples: Float32Array) => void, private failure: (error: Error) => void) {
    try {
      this.decoder = new AudioDecoder({output: (data) => {
        try {
          if (this.closed) return;
          const packet = this.pending.shift();
          if (!packet || data.sampleRate !== OPUS_RATE || packet.discard + packet.samples > data.numberOfFrames) throw new Error("Invalid decoded audio length.");
          const samples = new Float32Array(packet.samples);
          data.copyTo(samples, {planeIndex: 0, format: "f32-planar", frameOffset: packet.discard, frameCount: packet.samples});
          this.output(samples);
        } catch (error) { if (!this.closed) this.failure(new VoiceCodecFailure(String(error))); }
        finally { data.close(); }
      }, error: (error) => { if (!this.closed) this.failure(new VoiceCodecFailure(error.message)); }});
      this.decoder.configure(OPUS_DECODER_CONFIG);
    } catch (error) { throw new VoiceCodecFailure(String(error)); }
  }
  write(packet: AudioPacket): void {
    if (this.closed) return;
    if (this.pending.length >= 25) throw new VoiceAudioBackpressure("Audio decoding is falling behind. Please try again.");
    this.pending.push(packet);
    try { this.decoder.decode(new EncodedAudioChunk({type: "key", timestamp: packet.timestamp, data: packet.payload})); }
    catch (error) { throw new VoiceCodecFailure(String(error)); }
  }
  async finish(): Promise<void> {
    if (!this.closed) {
      try { await this.decoder.flush(); } catch (error) { if (!this.closed) throw new VoiceCodecFailure(String(error)); }
      if (!this.closed && this.pending.length) throw new VoiceCodecFailure("Missing decoded audio frames.");
    }
  }
  close(): void { this.closed = true; this.pending = []; if (this.decoder.state !== "closed") this.decoder.close(); }
}

/** Stateful PCM compatibility path: preserve fractional phase across ScriptProcessor blocks. */
export class PcmResampler {
  private input: number[] = [];
  private positionTicks = 0;
  private total = 0;
  private produced = 0;
  constructor(private sourceRate: number, private targetRate: number) {}
  push(samples: Float32Array): Float32Array {
    if (this.sourceRate === this.targetRate) return samples;
    for (const sample of samples) this.input.push(sample);
    this.total += samples.length;
    return this.drain();
  }
  private drain(): Float32Array {
    const output: number[] = [];
    const limit = Math.floor(this.total * this.targetRate / this.sourceRate);
    let position = this.positionTicks / this.targetRate;
    while (position + 1 < this.input.length && this.produced < limit) {
      const index = Math.floor(position), fraction = (this.positionTicks % this.targetRate) / this.targetRate;
      output.push(this.input[index] + (this.input[index + 1] - this.input[index]) * fraction);
      this.produced++;
      this.positionTicks += this.sourceRate;
      position = this.positionTicks / this.targetRate;
    }
    const remove = Math.min(this.input.length - 1, Math.floor(position));
    if (remove > 0) { this.input.splice(0, remove); this.positionTicks -= remove * this.targetRate; }
    return Float32Array.from(output);
  }
  finish(): Float32Array {
    if (this.sourceRate === this.targetRate || !this.input.length) return new Float32Array(0);
    this.input.push(this.input[this.input.length - 1]);
    return this.drain();
  }
}
