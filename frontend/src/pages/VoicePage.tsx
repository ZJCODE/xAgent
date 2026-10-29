import { Mic, Play } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, PageShell, PageToolbar, Panel, StatusBadge } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { applyTranscript, browserVoiceSocketUrl, type TranscriptLine } from "../lib/deviceVoice";
import { PhoneVoiceClient, VoiceConnectionSuperseded } from "../lib/phoneVoiceClient";
import { connectChannel, createBrowserVoiceSession, getAgentVoiceSettings, getDevices, restartRuntime, startRuntime } from "../lib/api";

type Phase = "connecting" | "live" | "stopped" | "failed" | "unconfigured" | "restart";
const RETRY_DELAYS = [1000, 2000, 4000];

export function VoicePage() {
  const { agents, selectedAgent, refresh } = useAgentSession();
  const configuredApiUrl = agents.find((agent) => agent.name === selectedAgent)?.api_url || "";
  if (!selectedAgent) {
    return (
      <PageShell className="channels-page voice-page">
        <PageToolbar title="Voice" subtitle="Hold to talk with your Agent." />
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

  const releaseHold = useCallback(() => {
    holdingRef.current = false;
    setHolding(false);
    setLevel(0);
    void clientRef.current?.setHolding(false);
  }, []);

  useEffect(() => {
    let disposed = false;
    let suspended = false;
    let retryTimer = 0;
    let stableTimer = 0;
    let failures = 0;
    let generation = 0;
    const current = (value: number) => !disposed && !suspended && generation === value;
    const retry = (reason: string) => {
      if (disposed || suspended) return;
      window.clearTimeout(stableTimer);
      releaseHold();
      setPlaying(false);
      if (failures >= RETRY_DELAYS.length) {
        setPhase("failed");
        setError(reason || "Unable to connect. Please try again.");
        return;
      }
      setPhase("connecting");
      window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(() => void connect(), RETRY_DELAYS[failures++]);
    };
    const client = new PhoneVoiceClient({
      onLive: () => {
        stableTimer = window.setTimeout(() => { failures = 0; }, 10000);
        setPhase("live");
        setError("");
      },
      onFloor: setFloor,
      onPlaying: setPlaying,
      onLevel: setLevel,
      onTranscript: (frame) => {
        setLines((previous) => applyTranscript(previous, frame, () => transcriptId.current++));
      },
      onClose: () => retry("The voice connection closed. Please try again."),
      onNotice: (message) => { releaseHold(); setError(message); },
    });
    clientRef.current = client;
    const connect = async () => {
      const ownGeneration = ++generation;
      setPhase("connecting");
      setError("");
      setFloor("idle");
      try {
        const [voice, initialStatus] = await Promise.all([getAgentVoiceSettings(), getDevices()]);
        if (!current(ownGeneration)) return;
        let status = initialStatus;
        if (!voice.configured) {
          setPhase("unconfigured");
          return;
        }
        if (!status.runtime_available) {
          setPhase("stopped");
          return;
        }
        if (voice.restart_required) {
          setPhase("restart");
          return;
        }
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
        });
      } catch (err) {
        if (!current(ownGeneration) || err instanceof VoiceConnectionSuperseded) return;
        retry(err instanceof Error ? err.message : String(err));
      }
    };
    const suspend = () => {
      suspended = true;
      generation += 1;
      window.clearTimeout(retryTimer);
      window.clearTimeout(stableTimer);
      releaseHold();
      client.disconnect();
    };
    const resume = () => {
      if (!suspended || disposed) return;
      suspended = false;
      failures = 0;
      void connect();
    };
    window.addEventListener("pagehide", suspend);
    window.addEventListener("pageshow", resume);
    void connect();
    return () => {
      disposed = true;
      generation += 1;
      window.clearTimeout(retryTimer);
      window.clearTimeout(stableTimer);
      window.removeEventListener("pagehide", suspend);
      window.removeEventListener("pageshow", resume);
      client.disconnect();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [configuredApiUrl, attempt, releaseHold]);

  useEffect(() => {
    window.addEventListener("blur", releaseHold);
    document.addEventListener("visibilitychange", releaseHold);
    return () => {
      window.removeEventListener("blur", releaseHold);
      document.removeEventListener("visibilitychange", releaseHold);
    };
  }, [releaseHold]);

  const beginHold = () => {
    if (phase !== "live" || holdingRef.current) return;
    holdingRef.current = true;
    setHolding(true);
    setError("");
    void clientRef.current?.setHolding(true);
  };
  const live = phase === "live";
  const talkLabel = holding ? "Release to send" : playing ? "Speaking" : "Hold to talk";
  const activity = holding ? "Mic open" : playing ? "Speaking" : floor === "thinking" ? "Thinking" : "Ready";

  return (
    <PageShell className="channels-page voice-page">
      <PageToolbar title="Voice" subtitle="Hold to talk with your Agent." actions={
        <StatusBadge tone={live ? "good" : phase === "connecting" ? "info" : phase === "failed" ? "danger" : "muted"}>
          {live ? "Ready" : phase === "connecting" ? "Connecting…" : phase === "failed" ? "Disconnected" : phase === "unconfigured" ? "Needs setup" : phase === "restart" ? "Restart required" : "Agent offline"}
        </StatusBadge>
      } />
      <div className="channels-content voice-content">
        {error ? <div className="error-strip" role="alert">{error}</div> : null}
        {phase === "failed" ? <Button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</Button> : null}
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
          <div className="voice-meta">
            {live ? <StatusBadge tone={holding ? "good" : playing ? "info" : "muted"}>{activity}</StatusBadge> : null}
            <span className="wizard-hint">Use headphones if the speaker feeds back into the mic.</span>
          </div>
          <div className="talk-wrap">
            <button type="button"
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
            ><Mic size={28} /><span>{talkLabel}</span></button>
            <div className="talk-level" aria-hidden="true"><span style={{ width: `${Math.round(level * 100)}%` }} /></div>
          </div>
          <ul className="voice-transcript">
            {lines.length === 0 ? <li className="voice-transcript-empty">Transcripts appear here after you speak.</li> : null}
            {lines.map((line) => <li key={line.id}><span className="role">{line.role === "user" ? "You" : "Agent"}</span>{line.text}</li>)}
          </ul>
        </Panel>
      </div>
    </PageShell>
  );
}
