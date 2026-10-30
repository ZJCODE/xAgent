export type VoicePreviewState = 'idle' | 'loading' | 'playing';
export interface VoicePreviewSelection {voice: string; speed: number; voice_api_key?: string}
type Session = {controller: AbortController; context: AudioContext; source?: AudioBufferSourceNode};

/** A short, isolated clip; never opens the microphone or a conversation. */
export class VoicePreviewPlayer {
  private session: Session | null = null;
  constructor(
    private load: (selection: VoicePreviewSelection, signal: AbortSignal) => Promise<ArrayBuffer>,
    private state: (value: VoicePreviewState) => void,
    private failure: (message: string) => void,
  ) {}

  async play(selection: VoicePreviewSelection): Promise<void> {
    this.stop();
    this.failure('');
    let session: Session | null = null;
    try {
      // Resume synchronously from the button gesture, before awaiting the fetch.
      const context = new AudioContext();
      session = {controller: new AbortController(), context};
      this.session = session;
      this.state('loading');
      const resumed = context.resume();
      const [audio] = await Promise.all([this.load(selection, session.controller.signal), resumed]);
      if (this.session !== session) return;
      const buffer = await context.decodeAudioData(audio);
      if (this.session !== session) return;
      const source = context.createBufferSource();
      session.source = source;
      source.buffer = buffer;
      source.connect(context.destination);
      source.onended = () => { if (this.session === session) this.stop(); };
      source.start();
      this.state('playing');
    } catch (error) {
      if (session && this.session !== session) return;
      this.stop();
      this.failure(error instanceof Error ? error.message : 'Unable to play the voice preview. Please try again.');
    }
  }

  stop(): void {
    const session = this.session;
    this.session = null;
    if (session) {
      session.controller.abort();
      if (session.source) {
        session.source.onended = null;
        try { session.source.stop(); } catch { /* Already ended. */ }
        session.source.disconnect();
      }
      void session.context.close().catch(() => undefined);
    }
    this.state('idle');
  }
}
