import {AudioRing} from './audioDsp.js';

class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.active = false;
    this.pool = new Array(8);
    this.port.onmessage = ({data}) => {
      try {
        if (data.type === 'start') {
          this.stream = data.stream;
          this.ring = new AudioRing(sampleRate, data.rate);
          this.frameSize = data.rate / 50;
          this.poolCount = 8;
          for (let i = 0; i < 8; i++) this.pool[i] = new Float32Array(this.frameSize);
          this.frame = null;
          this.used = 0;
          this.active = true;
          this.port.postMessage({type: 'started', stream: this.stream});
        } else if (data.stream === this.stream) {
          if (data.type === 'recycle' && this.poolCount < 8) this.pool[this.poolCount++] = new Float32Array(data.buffer);
          if (data.type === 'abort') this.active = false;
          if (data.type === 'flush' && this.active) {
            this.ring.final = true;
            this.drain();
            if (this.frame && this.used) this.emit();
            this.active = false;
            this.port.postMessage({type: 'flushed', stream: this.stream});
          }
        }
      } catch (error) { this.active = false; this.port.postMessage({type: 'error', stream: this.stream, message: error.message}); }
    };
  }
  emit() {
    const frame = this.frame;
    this.port.postMessage({type: 'audio', stream: this.stream, samples: this.used, buffer: frame.buffer}, [frame.buffer]);
    this.frame = null;
    this.used = 0;
  }
  drain() {
    while (!this.ring.complete) {
      if (!this.frame) {
        if (!this.poolCount) throw new Error('Capture audio queue exceeded 160 ms');
        this.frame = this.pool[--this.poolCount];
      }
      const count = this.ring.read(this.frame, this.used);
      this.used += count;
      if (this.used === this.frameSize) this.emit();
      if (!count) break;
    }
  }
  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    if (this.active && inputs[0]?.[0]) {
      try { this.ring.push(inputs[0][0]); this.drain(); }
      catch (error) { this.active = false; this.port.postMessage({type: 'error', stream: this.stream, message: error.message}); }
    }
    return true;
  }
}
registerProcessor('voice-capture', VoiceCapture);
