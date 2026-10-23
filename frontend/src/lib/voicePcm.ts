/** PCM helpers for the browser voice device. Wire format is little-endian PCM16. */

export function floatSampleToInt16(sample: number): number {
  const clamped = Math.max(-1, Math.min(1, sample));
  const scaled = clamped < 0 ? clamped * 32768 : clamped * 32767;
  return Math.max(-32768, Math.min(32767, Math.round(scaled)));
}

export function pcm16ToMonoFloat(pcm: ArrayBuffer, channels: number): Float32Array {
  const view = new DataView(pcm);
  const frameCount = Math.floor(view.byteLength / 2 / Math.max(1, channels));
  const output = new Float32Array(frameCount);
  for (let frame = 0; frame < frameCount; frame += 1) {
    let mixed = 0;
    for (let channel = 0; channel < channels; channel += 1) {
      const offset = (frame * channels + channel) * 2;
      mixed += view.getInt16(offset, true) / 32768;
    }
    output[frame] = mixed / channels;
  }
  return output;
}

/** Cumulative wire bytes heard by `now`, aligned to 16-bit frames. */
export function bytesPlayedAt(
  segments: Array<{ start: number; duration: number; bytes: number }>,
  now: number,
): number {
  let played = 0;
  for (const segment of segments) {
    if (segment.duration <= 0 || segment.bytes <= 0) continue;
    if (now >= segment.start + segment.duration) {
      played += segment.bytes;
      continue;
    }
    if (now > segment.start) {
      const raw = Math.floor(segment.bytes * ((now - segment.start) / segment.duration));
      played += raw - (raw % 2);
    }
  }
  return played;
}

export interface Pcm16Downsampler {
  push(input: Float32Array): void;
  takePartial(): ArrayBuffer | null;
}

/**
 * Linear-resample mono float audio into fixed PCM16 frames at `targetRate`.
 * `onFrame` receives a copy of each full frame (20 ms at 16 kHz by default).
 */
export function createPcm16Downsampler(
  sourceRate: number,
  targetRate: number,
  onFrame: (frame: ArrayBuffer) => void,
  frameSamples = 320,
): Pcm16Downsampler {
  if (!(sourceRate > 0) || !(targetRate > 0) || frameSamples < 1) {
    throw new Error("Invalid PCM resample settings");
  }
  const ratio = sourceRate / targetRate;
  const frame = new Int16Array(frameSamples);
  let filled = 0;
  let carry = 0;
  let lastSample = 0;
  let hasLast = false;

  const emit = (sample: number) => {
    frame[filled] = floatSampleToInt16(sample);
    filled += 1;
    if (filled === frame.length) {
      onFrame(frame.buffer.slice(0));
      filled = 0;
    }
  };

  return {
    push(input: Float32Array) {
      if (!input.length) return;
      let position = carry;
      if (!hasLast) {
        lastSample = input[0];
        hasLast = true;
      }
      const length = input.length;
      while (position < length) {
        const index = Math.floor(position);
        const fraction = position - index;
        const left = index >= 0 ? input[index] : lastSample;
        const right = index + 1 < length ? input[index + 1] : input[length - 1];
        emit(left * (1 - fraction) + right * fraction);
        position += ratio;
      }
      lastSample = input[length - 1];
      carry = position - length;
    },
    takePartial() {
      if (!filled) return null;
      const partial = frame.slice(0, filled);
      filled = 0;
      return partial.buffer.slice(partial.byteOffset, partial.byteOffset + partial.byteLength);
    },
  };
}
