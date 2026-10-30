import { Mic, Play } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button, PageShell, PageToolbar, Panel, StatusBadge } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import {VoiceSessionController, initialVoiceSnapshot} from '../lib/voiceSessionController';

export function VoicePage() {
  const { agents, selectedAgent, refresh } = useAgentSession();
  const configuredApiUrl = agents.find((agent) => agent.name === selectedAgent)?.api_url || "";
  if (!selectedAgent) {
    return (
      <PageShell className="channels-page voice-page">
        <PageToolbar title="Voice" subtitle="Talk with your Agent, your way." />
        <div className="channels-content voice-content">
          <p className="wizard-hint">Create an agent to use voice.</p>
        </div>
      </PageShell>
    );
  }
  return <VoiceSession key={selectedAgent} configuredApiUrl={configuredApiUrl} refreshAgents={refresh} />;
}

function VoiceSession({ configuredApiUrl, refreshAgents }: {
  configuredApiUrl: string;
  refreshAgents: () => Promise<void>;
}) {
  const [state, setState] = useState(initialVoiceSnapshot);
  const controller = useRef<VoiceSessionController | null>(null);
  const transcriptRef = useRef<HTMLUListElement>(null);
  const followTranscript = useRef(true);
  useEffect(() => {
    const session = new VoiceSessionController(configuredApiUrl, refreshAgents);
    controller.current = session;
    const unsubscribe = session.subscribe(setState);
    const visibility = () => document.visibilityState === 'hidden' ? session.hide() : session.show();
    const hide = () => session.hide(), show = () => session.show(), blur = () => session.release();
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('pagehide', hide);
    window.addEventListener('pageshow', show);
    window.addEventListener('blur', blur);
    if (document.visibilityState === 'hidden') session.hide();
    void session.open();
    return () => {
      unsubscribe(); session.dispose();
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('pagehide', hide); window.removeEventListener('pageshow', show); window.removeEventListener('blur', blur);
      if (controller.current === session) controller.current = null;
    };
  }, [configuredApiUrl, refreshAgents]);
  const {mode, started, muted, level, lines, error, floor} = state;
  useLayoutEffect(() => {
    const transcript = transcriptRef.current;
    if (transcript && followTranscript.current) transcript.scrollTop = transcript.scrollHeight;
  }, [lines]);
  const phase = state.connection;
  const live = phase === 'live';
  const continuous = mode === 'continuous';
  const holding = !continuous && (state.capture === 'opening' || state.capture === 'recording');
  const playing = state.playback !== 'idle';
  const capturing = state.capture === 'recording';
  const talkLabel = continuous ? started ? 'End conversation' : state.resumable ? 'Continue conversation' : 'Start conversation'
    : holding ? 'Release to stop recording' : playing ? 'Speaking' : 'Hold to talk';
  const connecting = ['checking', 'connecting', 'reconnecting'].includes(phase);
  const activity = state.paused || state.resumable ? 'Paused' : phase === 'reconnecting' ? 'Reconnecting…' : connecting ? 'Connecting…'
    : continuous && !started ? 'Not started' : muted && continuous ? 'Muted'
    : playing ? 'Speaking' : floor === 'thinking' ? 'Thinking' : capturing ? 'Listening' : 'Ready';
  const canStart = !state.paused && ['idle', 'live', 'failed'].includes(phase);
  const releaseHold = () => controller.current?.release();
  const beginHold = () => controller.current?.press();
  return (
    <PageShell className="channels-page voice-page">
      <PageToolbar title="Voice" subtitle="Talk with your Agent, your way." actions={
        <StatusBadge tone={live ? "good" : connecting ? "info" : phase === "failed" ? "danger" : "muted"}>
          {state.paused || state.resumable ? "Paused" : live ? "Ready" : phase === "idle" ? "Not started" : phase === "reconnecting" ? "Reconnecting…" : connecting ? "Connecting…" : phase === "failed" ? "Disconnected" : phase === "unconfigured" ? "Needs setup" : phase === "restart" ? "Restart required" : "Agent offline"}
        </StatusBadge>
      } />
      <div className="channels-content voice-content">
        {error ? <div className="error-strip" role="alert">{error}</div> : null}
        {phase === "failed" && !continuous ? <Button type="button" onClick={() => void controller.current?.open()}>Retry</Button> : null}
        {phase === "unconfigured" ? (
          <div className="device-readiness-banner">
            <p className="wizard-hint">Configure your Agent’s voice once to start talking.</p>
            <a className="ui-button ui-button-primary" href="/agent#voice">Configure voice in Agent</a>
          </div>
        ) : null}
        {phase === "stopped" || phase === "restart" ? (
          <div className="device-readiness-banner">
            <p className="wizard-hint">{phase === "restart" ? "Restart your Agent to apply the saved settings." : "Start your Agent to use voice."}</p>
            <Button type="button" onClick={() => void controller.current?.prepareAgent()}><Play size={13} />{phase === "restart" ? "Restart Agent" : "Start Agent"}</Button>
          </div>
        ) : null}
        <Panel className="voice-panel">
          <div className="voice-mode-switch" role="group" aria-label="Conversation mode">
            <Button type="button" variant={mode === "hold" ? "primary" : "ghost"} aria-pressed={mode === "hold"}
              onClick={() => controller.current?.setMode("hold")}>Hold to talk</Button>
            <Button type="button" variant={continuous ? "primary" : "ghost"} aria-pressed={continuous}
              onClick={() => controller.current?.setMode("continuous")}>Continuous conversation</Button>
          </div>
          <p className="wizard-hint">Switching modes ends the current conversation.</p>
          <div className="voice-meta">
            <StatusBadge tone={capturing ? "good" : playing ? "info" : "muted"}>{activity}</StatusBadge>
            <span className="wizard-hint">Use headphones if the speaker feeds back into the mic.</span>
          </div>
          <div className="talk-wrap">
            {continuous ? <>
              <button type="button" className={`talk-button${started ? " holding" : ""}`}
                disabled={!started && !canStart} aria-label={talkLabel}
                onClick={() => started ? controller.current?.end() : controller.current?.start()}>
                <Mic size={28} /><span>{talkLabel}</span>
              </button>
              {started ? <Button type="button" variant="secondary" aria-pressed={muted} onClick={() => controller.current?.mute(!muted)}>{muted ? "Unmute" : "Mute"}</Button> : null}
            </> : <button type="button"
              className={holding ? "talk-button holding" : playing ? "talk-button playing" : "talk-button"}
              disabled={!live} aria-pressed={holding} aria-label={talkLabel}
              onPointerDown={(event) => {
                if (event.button !== 0) return;
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
                beginHold();
              }}
              onPointerUp={releaseHold} onPointerCancel={releaseHold} onLostPointerCapture={releaseHold}
              onContextMenu={(event) => event.preventDefault()}
              onKeyDown={(event) => {
                if (event.repeat) return;
                if (event.key === " " || event.key === "Enter") {
                  event.preventDefault();
                  beginHold();
                }
              }}
              onKeyUp={(event) => {
                if (event.key === " " || event.key === "Enter") {
                  event.preventDefault();
                  releaseHold();
                }
              }}
              onBlur={releaseHold}
            ><Mic size={28} /><span>{talkLabel}</span></button>}
            <div className="talk-level" aria-hidden="true"><span style={{ width: `${Math.round(level * 100)}%` }} /></div>
          </div>
          <p className="wizard-hint">{continuous
            ? "Start once to talk continuously. Conversation pauses when this page is hidden."
            : "Hold to talk; release to stop recording."}</p>
          <ul className="voice-transcript" ref={transcriptRef} onScroll={(event) => {
            const element = event.currentTarget;
            followTranscript.current = element.scrollHeight - element.scrollTop - element.clientHeight <= 48;
          }}>
            {lines.length === 0 ? <li className="voice-transcript-empty">Transcripts appear here after you speak.</li> : null}
            {lines.map((line) => <li key={line.id}><span className="role">{line.role === "user" ? "You" : "Agent"}</span>{line.text}</li>)}
          </ul>
        </Panel>
      </div>
    </PageShell>
  );
}
