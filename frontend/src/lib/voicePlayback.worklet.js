import {AudioRing} from './audioDsp.js';

class VoicePlayback extends AudioWorkletProcessor {
  constructor() {
    super();
    this.stream = 0;
    this.active = false;
    this.port.onmessage = ({data}) => {
      if (data.type === 'start') {
        this.stream = data.stream;
        this.rate = data.rate;
        this.ring = new AudioRing(this.rate, sampleRate);
        this.active = true;
        this.buffering = true;
        this.clock = 0;
        this.underruns = 0;
      } else if (data.stream === this.stream && this.active) {
        try {
          if (data.type === 'audio') {
            this.ring.push(new Float32Array(data.buffer));
            if (this.ring.pending > this.rate / 2) throw new Error('Playback audio queue exceeded 500 ms');
          }
          if (data.type === 'end') this.ring.final = true;
          if (data.type === 'clear') this.active = false;
        } catch (error) { this.active = false; this.port.postMessage({type: 'error', stream: this.stream, message: error.message}); }
      }
    };
  }
  progress(finished = false) {
    this.port.postMessage({type: finished ? 'finished' : 'progress', stream: this.stream,
      samples: finished ? this.ring.written : this.ring.consumed, underruns: this.underruns, queueSamples: this.ring.pending});
  }
  process(_inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!output) return true;
    output.fill(0);
    if (!this.active) return true;
    if (this.buffering && (this.ring.pending >= this.rate * .06 || this.ring.final)) this.buffering = false;
    if (!this.buffering) {
      const count = this.ring.read(output);
      if (this.ring.complete) {
        this.active = false;
        this.progress(true);
      } else if (count < output.length) {
        this.underruns++;
        this.buffering = true;
      }
    }
    this.clock += output.length;
    if (this.active && this.clock >= sampleRate * .05) { this.clock = 0; this.progress(); }
    return true;
  }
}
registerProcessor('voice-playback', VoicePlayback);
