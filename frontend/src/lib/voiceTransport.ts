import {floatToPcm16, pcm16ToChannelPlanes, pcmRms, voiceHello, type VoiceReadyFrame} from './deviceVoice';
import {OpusInput, OpusOutput, VoiceCodecFailure, VoiceAudioBackpressure, selectAudioCodec, packAudio, unpackAudio, type AudioCodec} from './voiceAudio';

export interface VoiceCommand {id: number; type: string; [key: string]: unknown}
type Input = {stream: number; start: number; end?: number; sequence: number; samples: number; encoder?: OpusInput};
type Playback = {stream: number; id: string; sequence: number; samples: number; decoder?: OpusOutput; ending: boolean; timer?: ReturnType<typeof setTimeout>};

/** Sole owner of WebSocket and codecs; runs in the dedicated audio worker. */
export class VoiceTransport {
  private socket: WebSocket | null = null;
  private capture!: MessagePort;
  private playback!: MessagePort;
  private codec: AudioCodec = 'pcm_s16le';
  private inputRate = 16000;
  private outputRate = 24000;
  private outputDelay = 0;
  private input: Input | null = null;
  private output: Playback | null = null;
  private nextInput = 0;
  private lastOutput = 0;
  private initRequest: number | null = null;
  private ready = false;
  private closed = false;
  private handshake?: ReturnType<typeof setTimeout>;
  private ping?: ReturnType<typeof setInterval>;
  private upload: {bytes: number; seconds: number}[] = [];

  constructor(private post: (message: Record<string, unknown>) => void) {}
  private response(id: number, result?: unknown, error?: string, fatal = true) { this.post({type: 'response', id, result, error, fatal}); }
  private event(name: string, value?: unknown) { this.post({type: 'event', name, value}); }
  private send(message: unknown) { if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(message)); }

  async command(command: VoiceCommand): Promise<void> {
    try {
      if (command.type === 'init') { await this.init(command); return; }
      if (command.type === 'close') { this.close(); this.response(command.id); return; }
      if (!this.ready) throw new Error('Voice is not connected.');
      if (command.type === 'capture_start') {
        this.abortInput();
        const input: Input = {stream: ++this.nextInput, start: command.id, sequence: 0, samples: 0};
        this.input = input;
        if (this.codec === 'opus') input.encoder = new OpusInput(input.stream, (packet) => this.sendAudio(packet), (error) => {if (this.input === input) this.fail(error, true);});
        this.send({type: 'input_start', stream_id: input.stream});
        this.capture.postMessage({type: 'start', stream: input.stream, rate: this.inputRate});
      } else if (command.type === 'capture_end') {
        if (!this.input) { this.response(command.id); return; }
        this.input.end = command.id;
        this.capture.postMessage({type: 'flush', stream: this.input.stream});
      } else if (command.type === 'capture_abort') { this.abortInput(); this.response(command.id); }
      else throw new Error('Unknown audio command.');
    } catch (error) { this.response(command.id, undefined, String(error)); this.fail(error, true); }
  }

  private async init(command: VoiceCommand): Promise<void> {
    if (this.socket || this.closed) throw new Error('Audio worker already initialized.');
    this.initRequest = command.id;
    this.capture = command.capture as MessagePort;
    this.playback = command.playback as MessagePort;
    this.capture.onmessage = (event) => { void this.captureMessage(event.data).catch((error) => this.fail(error, true)); };
    this.playback.onmessage = (event) => this.playbackMessage(event.data);
    this.capture.start(); this.playback.start();
    this.outputDelay = Number(command.outputDelayMs);
    if (!Number.isFinite(this.outputDelay) || this.outputDelay < 0 || this.outputDelay > 5000) throw new Error('Invalid output latency.');
    this.codec = await selectAudioCodec();
    if (this.closed) return;
    this.inputRate = this.codec === 'opus' ? 48000 : 16000;
    this.outputRate = this.codec === 'opus' ? 48000 : 24000;
    const socket = new WebSocket(String(command.socketUrl));
    socket.binaryType = 'arraybuffer';
    this.socket = socket;
    this.handshake = setTimeout(() => this.fail(new Error('Voice handshake timed out.'), false), 8000);
    socket.onopen = () => this.send(voiceHello(this.codec));
    socket.onmessage = ({data}) => {
      try {
        if (data instanceof ArrayBuffer) { if (!this.ready) throw new Error('Audio arrived before ready.'); this.receiveAudio(data); return; }
        const frame = JSON.parse(data);
        if (frame.type === 'error') { this.fail(new VoiceCodecFailure(String(frame.message)), true); return; }
        if (!this.ready) {
          if (frame.type !== 'ready' || frame.audio_protocol !== 2 || frame.codec !== this.codec || frame.input_sample_rate !== this.inputRate || frame.output_sample_rate !== this.outputRate || frame.input_channels !== 1 || frame.output_channels !== 1) throw new Error('Voice protocol mismatch. Update the client and Agent together.');
          clearTimeout(this.handshake);
          this.ready = true;
          this.ping = setInterval(() => this.send({type: 'ping'}), 20000);
          const id = this.initRequest!; this.initRequest = null;
          this.response(id, frame as VoiceReadyFrame);
        } else this.frame(frame);
      } catch (error) { this.fail(error, true); }
    };
    socket.onerror = () => undefined;
    socket.onclose = ({code, reason}) => this.fail(new Error(reason || `Voice connection closed (${code}).`), code === 1008);
  }

  private sendAudio(packet: Parameters<typeof packAudio>[0]): void {
    if (!this.input || packet.stream !== this.input.stream || !this.ready || !this.socket) return;
    // Account for queued media duration, including low-bitrate/silence packets.
    let drain = Math.max(0, this.upload.reduce((n, item) => n + item.bytes, 0) - this.socket.bufferedAmount);
    while (drain > 0 && this.upload.length) {
      const first = this.upload[0], bytes = Math.min(first.bytes, drain);
      first.seconds *= (first.bytes - bytes) / first.bytes; first.bytes -= bytes; drain -= bytes;
      if (!first.bytes) this.upload.shift();
    }
    const duration = packet.samples / this.inputRate;
    if (this.upload.reduce((n, item) => n + item.seconds, 0) + duration > .5) throw new VoiceAudioBackpressure('Audio upload queue exceeded 500 ms.');
    const wire = packAudio(packet);
    this.socket.send(wire);
    this.upload.push({bytes: wire.byteLength, seconds: duration});
  }

  private async captureMessage(data: {type: string; stream?: number; buffer?: ArrayBuffer; samples?: number; message?: string}): Promise<void> {
    const input = this.input;
    if (!input || data.stream !== input.stream || this.closed) return;
    if (data.type === 'error') throw new VoiceAudioBackpressure(data.message);
    if (data.type === 'started') { this.response(input.start); input.start = 0; }
    if (data.type === 'audio' && data.buffer) {
      try {
        const samples = new Float32Array(data.buffer, 0, data.samples);
        if (!samples.length || samples.length > this.inputRate / 50) throw new Error('Invalid capture frame.');
        if (input.encoder) input.encoder.write(samples);
        else {
          this.sendAudio({stream: input.stream, sequence: input.sequence++, timestamp: Math.floor(input.samples * 1e6 / this.inputRate), samples: samples.length, discard: 0, payload: floatToPcm16(samples)});
          input.samples += samples.length;
        }
        this.event('level', Math.min(1, pcmRms(samples) * 4));
      } finally { this.capture.postMessage({type: 'recycle', stream: input.stream, buffer: data.buffer}, [data.buffer]); }
    }
    if (data.type === 'flushed') {
      try {
        await input.encoder?.finish();
        if (this.input === input) {
          this.send({type: 'input_end', stream_id: input.stream});
          this.input = null;
          if (input.end !== undefined) this.response(input.end);
        }
      } finally { input.encoder?.close(); }
    }
  }

  private abortInput(): void {
    const input = this.input;
    this.input = null;
    if (!input) return;
    this.capture.postMessage({type: 'abort', stream: input.stream});
    input.encoder?.close();
    this.send({type: 'input_end', stream_id: input.stream});
    if (input.start) this.response(input.start, {cancelled: true});
    if (input.end !== undefined) this.response(input.end, {cancelled: true});
  }

  private frame(frame: Record<string, unknown>): void {
    if (frame.type === 'error') { this.fail(new VoiceCodecFailure(String(frame.message)), true); return; }
    if (frame.type === 'state') this.event('floor', frame.floor);
    if (frame.type === 'transcript') this.event('transcript', frame);
    if (frame.type === 'playback_start') {
      if (frame.codec !== this.codec || frame.sample_rate !== this.outputRate || frame.channels !== 1 || typeof frame.playback_id !== 'string' || !frame.playback_id || !Number.isSafeInteger(frame.stream_id) || Number(frame.stream_id) <= this.lastOutput || Number(frame.stream_id) > 0xffffffff) throw new Error('Invalid playback profile or stream.');
      this.clearPlayback();
      const output: Playback = {id: frame.playback_id, stream: Number(frame.stream_id), sequence: 0, samples: 0, ending: false};
      this.output = output; this.lastOutput = output.stream;
      this.playback.postMessage({type: 'start', stream: output.stream, rate: this.outputRate});
      if (this.codec === 'opus') output.decoder = new OpusOutput((samples) => { if (this.output === output) this.play(samples); }, (error) => { if (this.output === output) this.fail(error, true); });
      this.event('playback', 'buffering');
    }
    if (frame.type === 'clear_playback' && frame.playback_id === this.output?.id) this.clearPlayback();
    if (frame.type === 'playback_done' && frame.playback_id === this.output?.id) {
      const output = this.output!;
      output.ending = true;
      void (async () => {
        try {
          await output.decoder?.finish();
          if (this.output === output) { this.playback.postMessage({type: 'end', stream: output.stream}); this.event('playback', 'draining'); }
        } catch (error) { if (this.output === output) this.fail(error, true); }
      })();
    }
  }
  private receiveAudio(bytes: ArrayBuffer): void {
    const packet = unpackAudio(bytes), output = this.output;
    if (!output || packet.stream !== output.stream) return;
    if (output.ending || packet.sequence !== output.sequence++ || Math.abs(packet.timestamp - Math.floor(output.samples * 1e6 / this.outputRate)) > 1) throw new Error('Discontinuous playback audio.');
    output.samples += packet.samples;
    if (output.decoder) output.decoder.write(packet);
    else {
      if (packet.discard || packet.payload.byteLength !== packet.samples * 2) throw new Error('Invalid PCM frame.');
      this.play(pcm16ToChannelPlanes(packet.payload, 1)[0]);
    }
  }
  private play(samples: Float32Array): void {
    if (!this.output) return;
    const buffer = Float32Array.from(samples).buffer;
    this.playback.postMessage({type: 'audio', stream: this.output.stream, buffer}, [buffer]);
  }
  private playbackMessage(data: {type: string; stream?: number; samples?: number; message?: string}): void {
    const output = this.output;
    if (!output || output.stream !== data.stream || this.closed) return;
    if (data.type === 'error') { this.fail(new VoiceAudioBackpressure(data.message), true); return; }
    if (!Number.isSafeInteger(data.samples) || Number(data.samples) < 0 || Number(data.samples) > output.samples) { this.fail(new Error('Invalid rendered sample count.'), true); return; }
    if (data.type === 'progress') {
      this.event('playback', output.ending ? 'draining' : 'playing');
      this.send({type: 'playback_progress', playback_id: output.id, sample_rate: this.outputRate,
        rendered_samples: Number(data.samples),
        played_samples: Math.max(0, Number(data.samples) - Math.ceil(this.outputDelay * this.outputRate / 1000))});
    }
    if (data.type === 'finished') {
      if (!output.ending || data.samples !== output.samples) { this.fail(new Error('Playback ended before media delivery.'), true); return; }
      output.timer = setTimeout(() => {
        if (this.output !== output) return;
        this.send({type: 'playback_progress', playback_id: output.id, sample_rate: this.outputRate, rendered_samples: output.samples, played_samples: output.samples});
        this.send({type: 'playback_finished', playback_id: output.id});
        this.clearPlayback();
      }, this.outputDelay);
    }
  }
  private clearPlayback(): void {
    const output = this.output;
    this.output = null;
    if (output) {
      clearTimeout(output.timer); output.decoder?.close();
      this.playback.postMessage({type: 'clear', stream: output.stream});
    }
    this.event('playback', 'idle');
  }
  private fail(error: unknown, fatal: boolean): void {
    if (this.closed) return;
    const message = error instanceof Error ? error.message : String(error);
    const request = this.initRequest;
    this.initRequest = null;
    this.close();
    if (request !== null) this.response(request, undefined, message, fatal);
    else this.event(fatal ? 'error' : 'closed', message);
  }
  close(): void {
    if (this.closed) return;
    this.closed = true; this.ready = false;
    clearTimeout(this.handshake); clearInterval(this.ping);
    this.abortInput(); this.clearPlayback();
    if (this.socket) {
      this.socket.onclose = this.socket.onmessage = this.socket.onopen = this.socket.onerror = null;
      this.send({type: 'stop'}); this.socket.close(); this.socket = null;
    }
    this.capture?.close(); this.playback?.close(); this.upload = [];
  }
}
