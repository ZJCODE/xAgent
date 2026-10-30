import captureUrl from './voiceCapture.worklet.js?worker&url';
import playbackUrl from './voicePlayback.worklet.js?worker&url';
import {assertSocketAllowedOnPage, type VoiceReadyFrame} from './deviceVoice';

export type CapturePhase = 'closed' | 'opening' | 'recording' | 'draining';
export type PlaybackPhase = 'idle' | 'buffering' | 'playing' | 'draining';
export class AudioSessionError extends Error {
  constructor(message: string, readonly fatal = true) { super(message); }
}
export interface VoiceAudio {
  prime(): void;
  connect(socketUrl: string, pageProtocol: string): Promise<VoiceReadyFrame>;
  setCapture(enabled: boolean): Promise<void>;
  close(): void;
}
export type AudioEvent = (name: string, value: unknown) => void;
type Capture = {stream?: MediaStream; source?: MediaStreamAudioSourceNode; phase: CapturePhase; finish?: Promise<void>};

/** Owns browser-only resources. Worker owns sockets/codecs; worklets own audio clocks. */
export class BrowserVoiceAudio implements VoiceAudio {
  private context: AudioContext | null = null;
  private worker: Worker | null = null;
  private captureNode: AudioWorkletNode | null = null;
  private playbackNode: AudioWorkletNode | null = null;
  private capture: Capture | null = null;
  private closed = false;
  private connected = false;
  private nextRequest = 1;
  private pending = new Map<number, {resolve: (value: unknown) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout>}>();
  constructor(private event: AudioEvent) {}

  prime(): void {
    if (this.closed) throw new AudioSessionError('Voice session has ended.');
    if (!globalThis.isSecureContext || typeof AudioContext === 'undefined' || typeof AudioWorkletNode === 'undefined' || typeof Worker === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      throw new AudioSessionError('Voice requires HTTPS or localhost and a current browser with AudioWorklet support.');
    }
    if (!this.context) this.context = new AudioContext({latencyHint: 'interactive'});
    void this.context.resume().catch((error) => this.event('failure', {message: `Unable to resume audio: ${String(error)}`, fatal: true}));
  }
  async connect(socketUrl: string, pageProtocol: string): Promise<VoiceReadyFrame> {
    assertSocketAllowedOnPage(socketUrl, pageProtocol);
    this.prime();
    const audio = this.context!;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([Promise.all([audio.audioWorklet.addModule(captureUrl), audio.audioWorklet.addModule(playbackUrl)]),
        new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new AudioSessionError('Audio modules did not load. Reload this page.')), 8000); })]);
      if (this.closed) throw new AudioSessionError('Voice session has ended.');
      this.captureNode = new AudioWorkletNode(audio, 'voice-capture', {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
      this.playbackNode = new AudioWorkletNode(audio, 'voice-playback', {numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1]});
      this.captureNode.connect(audio.destination); // Capture worklet outputs silence.
      this.playbackNode.connect(audio.destination);
      this.captureNode.onprocessorerror = this.playbackNode.onprocessorerror = () => this.event('failure', {message: 'The audio processor stopped. End the conversation and retry.', fatal: true});
      const worker = new Worker(new URL('./voice.worker.ts', import.meta.url), {type: 'module'});
      this.worker = worker;
      worker.onmessage = ({data}) => {
        if (this.closed) return;
        if (data.type === 'response') {
          const pending = this.pending.get(data.id);
          if (!pending) return;
          clearTimeout(pending.timer); this.pending.delete(data.id);
          if (data.error) pending.reject(new AudioSessionError(data.error, data.fatal !== false));
          else pending.resolve(data.result);
        } else if (data.type === 'event') {
          if (data.name === 'error' || data.name === 'closed') {
            this.connected = false;
            this.event('failure', {message: String(data.value), fatal: data.name === 'error'});
          } else this.event(data.name, data.value);
        }
      };
      worker.onerror = () => this.event('failure', {message: 'The audio worker stopped. Reload this page.', fatal: true});
      const outputDelayMs = Math.ceil((audio.baseLatency + audio.outputLatency + 128 / audio.sampleRate) * 1000);
      const ready = await this.request('init', {socketUrl, capture: this.captureNode.port, playback: this.playbackNode.port, outputDelayMs}, [this.captureNode.port, this.playbackNode.port], 12000) as VoiceReadyFrame;
      if (this.closed) throw new AudioSessionError('Voice session has ended.');
      this.connected = true;
      return ready;
    } finally { clearTimeout(timer); }
  }
  private request(type: string, value: Record<string, unknown> = {}, transfer: Transferable[] = [], timeout = 2000): Promise<unknown> {
    if (!this.worker || this.closed) return Promise.reject(new AudioSessionError('Voice session has ended.'));
    const id = this.nextRequest++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new AudioSessionError(`Audio ${type} timed out.`)); }, timeout);
      this.pending.set(id, {resolve, reject, timer});
      try { this.worker!.postMessage({id, type, ...value}, transfer); }
      catch (error) { clearTimeout(timer); this.pending.delete(id); reject(new AudioSessionError(String(error))); }
    });
  }
  async setCapture(enabled: boolean): Promise<void> {
    if (!enabled) {
      const capture = this.capture;
      if (!capture) return;
      if (capture.finish) return capture.finish;
      if (capture.phase === 'opening') { this.abortCapture(); return; }
      capture.phase = 'draining'; this.event('capture', 'draining');
      this.releaseMic(capture);
      capture.finish = (async () => {
        try { await this.request('capture_end'); }
        finally { if (this.capture === capture) { this.capture = null; this.event('capture', 'closed'); this.event('level', 0); } }
      })();
      return capture.finish;
    }
    if (this.closed || !this.connected) return;
    if (this.capture?.phase === 'opening' || this.capture?.phase === 'recording') return;
    this.abortCapture();
    this.prime();
    const capture: Capture = {phase: 'opening'};
    this.capture = capture; this.event('capture', 'opening');
    try {
      capture.stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}, video: false});
      if (this.closed || this.capture !== capture) { this.releaseMic(capture); return; }
      capture.source = this.context!.createMediaStreamSource(capture.stream);
      await this.request('capture_start');
      if (this.closed || this.capture !== capture) { this.releaseMic(capture); return; }
      capture.source.connect(this.captureNode!);
      capture.phase = 'recording'; this.event('capture', 'recording');
    } catch (error) {
      this.releaseMic(capture);
      if (this.capture !== capture || this.closed) return;
      this.capture = null; this.event('capture', 'closed');
      throw error instanceof AudioSessionError ? error : new AudioSessionError(error instanceof DOMException && error.name === 'NotAllowedError'
        ? 'Microphone permission was denied. Allow the microphone and try again.' : `Unable to open the microphone: ${String(error)}`);
    }
  }
  private releaseMic(capture: Capture) { capture.source?.disconnect(); capture.stream?.getTracks().forEach((track) => track.stop()); }
  private abortCapture() {
    const capture = this.capture; this.capture = null;
    if (capture) { this.releaseMic(capture); if (this.worker && this.connected) this.worker.postMessage({id: 0, type: 'capture_abort'}); }
    this.event('capture', 'closed'); this.event('level', 0);
  }
  close(): void {
    if (this.closed) return;
    this.closed = true; this.abortCapture(); this.connected = false;
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new AudioSessionError('Voice session has ended.')); }
    this.pending.clear();
    if (this.worker) { this.worker.postMessage({id: 0, type: 'close'}); this.worker.terminate(); this.worker = null; }
    this.captureNode?.disconnect(); this.playbackNode?.disconnect();
    const context = this.context; this.context = null;
    if (context) void context.close().catch(() => undefined);
  }
}
