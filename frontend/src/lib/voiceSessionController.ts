import {connectChannel, createBrowserVoiceSession, getAgentVoiceSettings, getDevices, restartRuntime, startRuntime} from './api';
import {applyTranscript, browserVoiceSocketUrl, type TranscriptLine} from './deviceVoice';
import {BrowserVoiceAudio, AudioSessionError, type VoiceAudio, type AudioEvent, type CapturePhase, type PlaybackPhase} from './browserVoiceAudio';
import type {AudioCodec} from './voiceAudio';

export type VoiceMode = 'hold' | 'continuous';
export type ConnectionPhase = 'checking' | 'idle' | 'connecting' | 'reconnecting' | 'live' | 'stopped' | 'restart' | 'unconfigured' | 'failed';
export interface VoiceSnapshot {
  mode: VoiceMode; connection: ConnectionPhase; capture: CapturePhase; playback: PlaybackPhase;
  started: boolean; resumable: boolean; muted: boolean; paused: boolean; codec: AudioCodec | null;
  level: number; floor: string; lines: TranscriptLine[]; error: string;
}
export const initialVoiceSnapshot = (): VoiceSnapshot => ({mode: 'hold', connection: 'checking', capture: 'closed', playback: 'idle',
  started: false, resumable: false, muted: false, paused: false, codec: null, level: 0, floor: 'idle', lines: [], error: ''});

interface Services {
  settings: typeof getAgentVoiceSettings; devices: typeof getDevices; ticket: typeof createBrowserVoiceSession;
  connectApi: () => Promise<unknown>; startAgent: () => Promise<unknown>; restartAgent: () => Promise<unknown>;
}
const services: Services = {settings: getAgentVoiceSettings, devices: getDevices, ticket: createBrowserVoiceSession,
  connectApi: () => connectChannel('api'), startAgent: startRuntime, restartAgent: restartRuntime};

/** One source of truth for connection, capture intent, playback and resource lifetime. */
export class VoiceSessionController {
  private snapshot = initialVoiceSnapshot();
  private listeners = new Set<(snapshot: VoiceSnapshot) => void>();
  private audio: VoiceAudio | null = null;
  private epoch = 0;
  private disposed = false;
  private holding = false;
  private transcriptId = 1;
  private failures = 0;
  private retryTimer?: ReturnType<typeof setTimeout>;
  private stableTimer?: ReturnType<typeof setTimeout>;
  constructor(private apiUrl: string, private refresh: () => Promise<void>, private deps: Services = services,
    private audioFactory: (event: AudioEvent) => VoiceAudio = (event) => new BrowserVoiceAudio(event)) {}
  get state() { return this.snapshot; }
  subscribe(listener: (snapshot: VoiceSnapshot) => void) { this.listeners.add(listener); listener(this.snapshot); return () => { this.listeners.delete(listener); }; }
  private update(value: Partial<VoiceSnapshot>) {
    this.snapshot = {...this.snapshot, ...value};
    if (!this.disposed) for (const listener of this.listeners) listener(this.snapshot);
  }
  private current(epoch: number) { return !this.disposed && !this.snapshot.paused && this.epoch === epoch; }
  private clearTimers() { clearTimeout(this.retryTimer); clearTimeout(this.stableTimer); }
  private dropAudio() {
    this.epoch++; this.clearTimers();
    const audio = this.audio; this.audio = null; audio?.close(); this.holding = false;
    this.update({capture: 'closed', playback: 'idle', floor: 'idle', level: 0, lines: this.snapshot.lines.filter((line) => !line.pending)});
  }
  async open(): Promise<void> { this.failures = 0; await this.connect(); }
  private async connect(): Promise<void> {
    if (this.disposed || this.snapshot.paused) return;
    const epoch = ++this.epoch;
    this.update({connection: this.failures ? 'reconnecting' : 'checking', error: ''});
    try {
      const [settings, initialStatus] = await Promise.all([this.deps.settings(), this.deps.devices()]);
      if (!this.current(epoch)) return;
      if (!settings.configured) { this.update({connection: 'unconfigured', started: false}); return; }
      if (!initialStatus.runtime_available) { this.update({connection: 'stopped', started: false}); return; }
      if (settings.restart_required) { this.update({connection: 'restart', started: false}); return; }
      if (this.snapshot.mode === 'continuous' && !this.snapshot.started) { this.update({connection: 'idle'}); return; }
      this.update({connection: this.failures ? 'reconnecting' : 'connecting'});
      let status = initialStatus;
      if (!status.api_available) {
        await this.deps.connectApi(); if (!this.current(epoch)) return;
        status = await this.deps.devices(); if (!this.current(epoch)) return;
        if (!status.api_available) throw new Error('Voice is still starting. Please try again shortly.');
      }
      const ticket = await this.deps.ticket();
      if (!this.current(epoch)) return;
      const audio = this.audio ?? this.newAudio();
      const ready = await audio.connect(browserVoiceSocketUrl(this.apiUrl || status.api_url || '', ticket.session_id, ticket.token), window.location.protocol);
      if (!this.current(epoch)) return;
      this.update({connection: 'live', codec: ready.codec, error: ''});
      this.stableTimer = setTimeout(() => { this.failures = 0; }, 10000);
      this.syncCapture();
    } catch (error) { if (this.current(epoch)) this.failure(error); }
  }
  private newAudio(): VoiceAudio {
    const audio = this.audioFactory((name, value) => {
      if (this.disposed || this.audio !== audio || this.snapshot.paused) return;
      if (name === 'failure') { const failure = value as {message: string; fatal: boolean}; this.failure(new AudioSessionError(failure.message, failure.fatal)); }
      if (name === 'capture') this.update({capture: value as CapturePhase});
      if (name === 'playback') this.update({playback: value as PlaybackPhase});
      if (name === 'floor') this.update({floor: String(value)});
      if (name === 'level') this.update({level: this.snapshot.capture === 'recording' ? Number(value) : 0});
      if (name === 'transcript') this.update({lines: applyTranscript(this.snapshot.lines, value as Parameters<typeof applyTranscript>[1], () => this.transcriptId++)});
    });
    this.audio = audio;
    return audio;
  }
  private failure(error: unknown) {
    const fatal = error instanceof AudioSessionError && error.fatal;
    const message = error instanceof Error ? error.message : String(error);
    this.dropAudio();
    if (fatal || this.failures >= 3) { this.update({connection: 'failed', started: false, error: message}); return; }
    this.update({connection: 'reconnecting', error: ''});
    this.retryTimer = setTimeout(() => { void this.connect(); }, [1000, 2000, 4000][this.failures++]);
  }
  private syncCapture() {
    const audio = this.audio;
    if (!audio || this.snapshot.connection !== 'live' || this.snapshot.paused) return;
    const enabled = this.snapshot.mode === 'hold' ? this.holding : this.snapshot.started && !this.snapshot.muted;
    void audio.setCapture(enabled).catch((error) => { if (this.audio === audio) this.failure(error); });
  }
  setMode(mode: VoiceMode) {
    if (mode === this.snapshot.mode) return;
    this.dropAudio(); this.failures = 0;
    this.update({mode, started: false, resumable: false, muted: false, connection: 'idle', error: ''});
    void this.connect();
  }
  start() {
    if (this.disposed || this.snapshot.paused || this.snapshot.started) return;
    try { (this.audio ?? this.newAudio()).prime(); }
    catch (error) { this.failure(error); return; }
    this.update({started: true, resumable: false}); this.failures = 0; void this.connect();
  }
  end() { this.dropAudio(); this.update({started: false, resumable: false, connection: 'idle', error: ''}); }
  mute(muted: boolean) { this.update({muted}); this.syncCapture(); }
  press() { if (this.snapshot.mode !== 'hold' || this.snapshot.connection !== 'live') return; this.holding = true; this.syncCapture(); }
  release() { if (this.snapshot.mode !== 'hold') return; this.holding = false; this.syncCapture(); }
  hide() {
    if (this.snapshot.paused) return;
    this.dropAudio(); this.update({paused: true, started: false, resumable: this.snapshot.mode === 'continuous' && (this.snapshot.started || this.snapshot.resumable), connection: 'idle'});
  }
  show() {
    if (!this.snapshot.paused || this.disposed) return;
    this.update({paused: false}); this.failures = 0;
    if (this.snapshot.mode === 'hold') void this.connect();
  }
  async prepareAgent() {
    const restart = this.snapshot.connection === 'restart';
    this.dropAudio(); const epoch = this.epoch;
    this.update({connection: 'connecting', error: ''});
    try {
      if (restart) await this.deps.restartAgent(); else await this.deps.startAgent();
      if (!this.current(epoch)) return;
      await this.refresh(); if (this.current(epoch)) await this.open();
    } catch (error) { if (this.current(epoch)) this.update({connection: restart ? 'restart' : 'stopped', error: String(error)}); }
  }
  dispose() { this.disposed = true; this.dropAudio(); this.listeners.clear(); }
}
