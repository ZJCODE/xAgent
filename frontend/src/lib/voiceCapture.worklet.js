// Persistent resampling with a windowed-sinc low-pass filter and 20 ms output frames.
class VoiceCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.rate = options.processorOptions.rate;
    this.input = new Array(16).fill(0);
    this.positionTicks = 16 * this.rate;
    this.total = 0;
    this.produced = 0;
    this.frame = new Float32Array(this.rate / 50);
    this.used = 0;
    this.stopped = false;
    const cutoff = Math.min(1, this.rate / sampleRate) * 0.9;
    this.weights = Array.from({length: 256}, (_, phase) => {
      const weights = new Float32Array(32);
      let sum = 0;
      for (let tap = 0; tap < 32; tap++) {
        const d = tap - 15 - phase / 256;
        const sinc = Math.abs(d) < 1e-8 ? cutoff : Math.sin(Math.PI * d * cutoff) / (Math.PI * d);
        weights[tap] = sinc * (0.54 - 0.46 * Math.cos(2 * Math.PI * tap / 31));
        sum += weights[tap];
      }
      return weights.map((weight) => weight / sum);
    });
    this.port.onmessage = ({data}) => {
      if (data === 'flush') {
        this.input.push(...new Array(32).fill(0));
        this.drain(true);
        if (this.used) this.emit();
        this.stopped = true;
        this.port.postMessage('flushed');
      }
    };
  }
  emit() {
    const frame = this.frame.slice(0, this.used);
    this.port.postMessage(frame, [frame.buffer]);
    this.used = 0;
  }
  drain(flush = false) {
    const limit = Math.floor(this.total * this.rate / sampleRate);
    let position = this.positionTicks / this.rate;
    while (position + 16 < this.input.length && (!flush || this.produced < limit)) {
      const index = Math.floor(position);
      const weights = this.weights[Math.min(255, Math.floor((this.positionTicks % this.rate) * 256 / this.rate))];
      let value = 0;
      for (let tap = 0; tap < 32; tap++) value += this.input[index - 15 + tap] * weights[tap];
      this.frame[this.used++] = value;
      this.produced++;
      if (this.used === this.frame.length) this.emit();
      this.positionTicks += sampleRate;
      position = this.positionTicks / this.rate;
    }
    const remove = Math.max(0, Math.floor(position) - 16);
    this.input.splice(0, remove);
    this.positionTicks -= remove * this.rate;
  }
  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    if (this.stopped) return false;
    const samples = inputs[0]?.[0];
    if (samples) {
      for (const sample of samples) this.input.push(sample);
      this.total += samples.length;
      this.drain();
    }
    return true;
  }
}
registerProcessor('voice-capture', VoiceCapture);
