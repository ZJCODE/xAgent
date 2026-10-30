// Bounded streaming resampler shared by capture and playback worklets.
export class AudioRing {
  constructor(sourceRate, targetRate, capacity = 65536) {
    this.sourceRate = sourceRate;
    this.targetRate = targetRate;
    this.data = new Float32Array(capacity);
    this.written = 0;
    this.produced = 0;
    this.positionTicks = 0;
    this.final = false;
    if (sourceRate === targetRate) return;
    const cutoff = Math.min(1, targetRate / sourceRate) * .9;
    this.weights = new Float32Array(256 * 32);
    for (let phase = 0; phase < 256; phase++) {
      let sum = 0;
      for (let tap = 0; tap < 32; tap++) {
        const d = tap - 15 - phase / 256;
        const sinc = Math.abs(d) < 1e-8 ? cutoff : Math.sin(Math.PI * d * cutoff) / (Math.PI * d);
        const weight = sinc * (.54 - .46 * Math.cos(2 * Math.PI * tap / 31));
        this.weights[phase * 32 + tap] = weight;
        sum += weight;
      }
      for (let tap = 0; tap < 32; tap++) this.weights[phase * 32 + tap] /= sum;
    }
  }
  get consumed() { return Math.min(this.written, Math.floor(this.positionTicks / this.targetRate)); }
  get pending() { return this.written - this.consumed; }
  get complete() { return this.final && this.produced >= Math.floor(this.written * this.targetRate / this.sourceRate); }
  push(samples) {
    const oldest = Math.max(0, Math.floor(this.positionTicks / this.targetRate) - 16);
    if (this.final || this.written - oldest + samples.length > this.data.length) throw new Error('Audio ring overflow');
    for (let i = 0; i < samples.length; i++) this.data[(this.written + i) % this.data.length] = samples[i];
    this.written += samples.length;
  }
  read(output, offset = 0, count = output.length - offset) {
    if (this.sourceRate === this.targetRate) {
      const size = Math.min(count, this.written - this.produced);
      for (let i = 0; i < size; i++) output[offset + i] = this.data[(this.produced + i) % this.data.length];
      this.produced += size;
      this.positionTicks += size * this.sourceRate;
      return size;
    }
    const limit = Math.floor(this.written * this.targetRate / this.sourceRate);
    let size = 0;
    while (size < count && this.produced < limit) {
      const index = Math.floor(this.positionTicks / this.targetRate);
      if (!this.final && index + 16 >= this.written) break;
      const phase = Math.floor((this.positionTicks % this.targetRate) * 256 / this.targetRate);
      let value = 0;
      for (let tap = 0; tap < 32; tap++) {
        const frame = index - 15 + tap;
        const sample = frame < 0 ? 0 : this.data[Math.min(frame, this.written - 1) % this.data.length];
        value += sample * this.weights[phase * 32 + tap];
      }
      output[offset + size++] = value;
      this.produced++;
      this.positionTicks += this.sourceRate;
    }
    return size;
  }
}
