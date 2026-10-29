import { Mic, Play } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, PageShell, PageToolbar, Panel, StatusBadge } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { applyTranscript, browserVoiceSocketUrl, type TranscriptLine } from "../lib/deviceVoice";
import { PhoneVoiceClient, VoiceConnectionSuperseded } from "../lib/phoneVoiceClient";
import { connectChannel, createBrowserVoiceSession, getAgentVoiceSettings, getDevices, restartRuntime, startRuntime } from "../lib/api";

type Mode = "hold" | "continuous";
type Phase = "idle" | "paused" | "connecting" | "live" | "stopped" | "failed" | "unconfigured" | "restart";
const RETRY_DELAYS = [1000, 2000, 4000];

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
  const [mode, setMode] = useState<Mode>("hold");
  const [started, setStarted] = useState(false);
  const [muted, setMuted] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const [transport, setTransport] = useState({input: "PCM16", output: "PCM16"});
  const [phase, setPhase] = useState<Phase>("connecting");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [holding, setHolding] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [floor, setFloor] = useState("idle");
  const [level, setLevel] = useState(0);
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const clientRef = useRef<PhoneVoiceClient | null>(null);
  const transcriptId = useRef(1);
  const holdingRef = useRef(false);
  const startedRef = useRef(false);
  const mutedRef = useRef(false);
  const modeRef = useRef<Mode>("hold");
  const pcmOnly = useRef(false);
  const startSession = useRef<() => void>(() => {});
  const stopSession = useRef<() => void>(() => {});

  const syncCapture = useCallback(() => {
    const enabled = modeRef.current === "hold" ? holdingRef.current : startedRef.current && !mutedRef.current;
    void clientRef.current?.setCapture(enabled);
  }, []);

  const releaseHold = useCallback(() => {
    holdingRef.current = false;
    setHolding(false);
    setLevel(0);
    if (modeRef.current === "hold") void clientRef.current?.setCapture(false);
  }, []);

  useEffect(() => {
    let disposed = false;
    let suspended = document.visibilityState === "hidden";
    let retryTimer = 0;
    let stableTimer = 0;
    let failures = 0;
    let generation = 0;
    const current = (value: number) => !disposed && !suspended && generation === value;
    const active = () => !disposed && !suspended && clientRef.current === client;
    const clearTimers = () => {
      window.clearTimeout(retryTimer);
      window.clearTimeout(stableTimer);
    };
    const stop = () => {
      generation += 1;
      clearTimers();
      startedRef.current = false;
      setStarted(false);
      releaseHold();
      client.disconnect();
      setPlaying(false);
      setCapturing(false);
      setFloor("idle");
      setLevel(0);
      setLines((previous) => previous.filter((line) => !line.pending));
    };
    const retry = (reason: string) => {
      if (!active()) return;
      window.clearTimeout(stableTimer);
      releaseHold();
      setPlaying(false);
      setCapturing(false);
      setLevel(0);
      setLines((previous) => previous.filter((line) => !line.pending));
      if (failures >= RETRY_DELAYS.length) {
        stop();
        setPhase("failed");
        setError(reason || "Unable to connect. Please try again.");
        return;
      }
      setPhase("connecting");
      window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(() => void connect(), RETRY_DELAYS[failures++]);
    };
    const client = new PhoneVoiceClient({
      onLive: (ready) => {
        if (!active()) return;
        stableTimer = window.setTimeout(() => { failures = 0; }, 10000);
        setTransport({input: ready.input_codec === "opus" ? "Opus" : "PCM16", output: ready.output_codec === "opus" ? "Opus" : "PCM16"});
        setPhase("live");
        setError("");
        syncCapture();
      },
      onFloor: (value) => { if (active()) setFloor(value); },
      onPlaying: (value) => { if (active()) setPlaying(value); },
      onCapture: (value) => { if (active()) setCapturing(value); },
      onLevel: (value) => { if (active()) setLevel(value); },
      onTranscript: (frame) => {
        if (active()) setLines((previous) => applyTranscript(previous, frame, () => transcriptId.current++));
      },
      onClose: ({ reason, codecFailure }) => {
        if (!active()) return;
        if (codecFailure) {
          if (pcmOnly.current) { stop(); setPhase("failed"); setError(reason); return; }
          pcmOnly.current = true;
          failures = 0;
        }
        retry(reason || "The voice connection closed. Please try again.");
      },
      onNotice: (message) => {
        if (!active()) return;
        stop();
        setError(message);
        setPhase("failed");
      },
    });
    clientRef.current = client;
    const connect = async () => {
      if (disposed || suspended) return;
      const ownGeneration = ++generation;
      setPhase("connecting");
      setError("");
      setFloor("idle");
      try {
        const [voice, initialStatus] = await Promise.all([getAgentVoiceSettings(), getDevices()]);
        if (!current(ownGeneration)) return;
        let status = initialStatus;
        if (!voice.configured) { setPhase("unconfigured"); return; }
        if (!status.runtime_available) { setPhase("stopped"); return; }
        if (voice.restart_required) { setPhase("restart"); return; }
        if (mode === "continuous" && !startedRef.current) { setPhase("idle"); return; }
        if (!status.api_available) {
          await connectChannel("api");
          if (!current(ownGeneration)) return;
          status = await getDevices();
          if (!current(ownGeneration)) return;
          if (!status.api_available) throw new Error("Voice is still starting. Please try again shortly.");
        }
        const apiUrl = configuredApiUrl || status.api_url;
        if (!apiUrl) throw new Error("The Agent connection is not configured.");
        const session = await createBrowserVoiceSession();
        if (!current(ownGeneration)) return;
        await client.connect({
          socketUrl: browserVoiceSocketUrl(apiUrl, session.session_id, session.token),
          pageProtocol: window.location.protocol,
          forcePcm: pcmOnly.current,
        });
      } catch (err) {
        if (!current(ownGeneration) || err instanceof VoiceConnectionSuperseded) return;
        retry(err instanceof Error ? err.message : String(err));
      }
    };
    startSession.current = () => {
      if (disposed || suspended) return;
      try { client.prime(); } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setPhase("failed");
        return;
      }
      startedRef.current = true;
      setStarted(true);
      failures = 0;
      void connect();
    };
    stopSession.current = () => { stop(); setError(""); setPhase("idle"); };
    const suspend = () => {
      if (suspended) return;
      suspended = true;
      stop();
      setPhase("paused");
    };
    const resume = () => {
      if (!suspended || disposed || document.visibilityState === "hidden") return;
      suspended = false;
      failures = 0;
      if (mode === "hold") void connect();
    };
    const visibility = () => document.visibilityState === "hidden" ? suspend() : resume();
    const blur = () => { if (mode === "hold") releaseHold(); };
    window.addEventListener("pagehide", suspend);
    window.addEventListener("pageshow", resume);
    window.addEventListener("blur", blur);
    document.addEventListener("visibilitychange", visibility);
    if (suspended) setPhase("paused");
    else void connect();
    return () => {
      disposed = true;
      generation += 1;
      clearTimers();
      window.removeEventListener("pagehide", suspend);
      window.removeEventListener("pageshow", resume);
      window.removeEventListener("blur", blur);
      document.removeEventListener("visibilitychange", visibility);
      client.disconnect();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [configuredApiUrl, attempt, mode, releaseHold, syncCapture]);

  const changeMode = (next: Mode) => {
    if (next === mode) return;
    stopSession.current();
    mutedRef.current = false;
    setMuted(false);
    modeRef.current = next;
    setMode(next);
  };

  const beginHold = () => {
    if (phase !== "live" || holdingRef.current) return;
    holdingRef.current = true;
    setHolding(true);
    setError("");
    syncCapture();
  };
  const live = phase === "live";
  const continuous = mode === "continuous";
  const talkLabel = continuous
    ? started ? "End conversation" : phase === "paused" ? "Continue conversation" : "Start conversation"
    : holding ? "Release to stop recording" : playing ? "Speaking" : "Hold to talk";
  const activity = phase === "paused" ? "Paused" : phase === "connecting" ? "Connecting…"
    : continuous && !started ? "Not started" : muted && continuous ? "Muted"
    : playing ? "Speaking" : floor === "thinking" ? "Thinking" : capturing ? "Listening" : "Ready";
  const canStart = ["idle", "paused", "live", "failed"].includes(phase);

  return (
    <PageShell className="channels-page voice-page">
      <PageToolbar title="Voice" subtitle="Talk with your Agent, your way." actions={
        <StatusBadge tone={live ? "good" : phase === "connecting" ? "info" : phase === "failed" ? "danger" : "muted"}>
          {live ? "Ready" : phase === "idle" ? "Not started" : phase === "paused" ? "Paused" : phase === "connecting" ? "Connecting…" : phase === "failed" ? "Disconnected" : phase === "unconfigured" ? "Needs setup" : phase === "restart" ? "Restart required" : "Agent offline"}
        </StatusBadge>
      } />
      <div className="channels-content voice-content">
        {error ? <div className="error-strip" role="alert">{error}</div> : null}
        {phase === "failed" && !continuous ? <Button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</Button> : null}
        {phase === "unconfigured" ? (
          <div className="device-readiness-banner">
            <p className="wizard-hint">Configure your Agent’s voice once to start talking.</p>
            <a className="ui-button ui-button-primary" href="/agent#voice">Configure voice in Agent</a>
          </div>
        ) : null}
        {phase === "stopped" || phase === "restart" ? (
          <div className="device-readiness-banner">
            <p className="wizard-hint">{phase === "restart" ? "Restart your Agent to apply the saved settings." : "Start your Agent to use voice."}</p>
            <Button type="button" onClick={() => {
              const requiresRestart = phase === "restart";
              setPhase("connecting");
              setError("");
              void (async () => {
                try {
                  if (requiresRestart) await restartRuntime();
                  else await startRuntime();
                  await refreshAgents();
                  setAttempt((value) => value + 1);
                } catch (err) {
                  setPhase(requiresRestart ? "restart" : "stopped");
                  setError(err instanceof Error ? err.message : String(err));
                }
              })();
            }}><Play size={13} />{phase === "restart" ? "Restart Agent" : "Start Agent"}</Button>
          </div>
        ) : null}
        <Panel className="voice-panel">
          <div className="voice-mode-switch" role="group" aria-label="Conversation mode">
            <Button type="button" variant={mode === "hold" ? "primary" : "ghost"} aria-pressed={mode === "hold"}
              onClick={() => changeMode("hold")}>Hold to talk</Button>
            <Button type="button" variant={continuous ? "primary" : "ghost"} aria-pressed={continuous}
              onClick={() => changeMode("continuous")}>Continuous conversation</Button>
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
                onClick={() => started ? stopSession.current() : startSession.current()}>
                <Mic size={28} /><span>{talkLabel}</span>
              </button>
              {started ? <Button type="button" variant="secondary" aria-pressed={muted} onClick={() => {
                mutedRef.current = !mutedRef.current;
                setMuted(mutedRef.current);
                syncCapture();
              }}>{muted ? "Unmute" : "Mute"}</Button> : null}
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
          {live ? <details className="voice-connection-details">
            <summary>Connection details</summary>
            <p className="wizard-hint">Microphone: {transport.input} · Playback: {transport.output} · WebSocket</p>
          </details> : null}
          <ul className="voice-transcript">
            {lines.length === 0 ? <li className="voice-transcript-empty">Transcripts appear here after you speak.</li> : null}
            {lines.map((line) => <li key={line.id}><span className="role">{line.role === "user" ? "You" : "Agent"}</span>{line.text}</li>)}
          </ul>
        </Panel>
      </div>
    </PageShell>
  );
}
