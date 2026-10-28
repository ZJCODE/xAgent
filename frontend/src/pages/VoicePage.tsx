import { Mic, Plug, Play } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, PageShell, PageToolbar, Panel, StatusBadge } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import {
  PHONE_DEVICE_ID,
  applyTranscript,
  describeVoiceSocket,
  readStoredCredential,
  writeStoredCredential,
  type TranscriptLine,
} from "../lib/deviceVoice";
import { PhoneVoiceClient, VoiceConnectionSuperseded } from "../lib/phoneVoiceClient";
import { cancelDevicePairing, connectChannel, createDevicePairing, getDevices, startRuntime } from "../lib/api";
import type { DeviceStatus } from "../types";

type Phase = "idle" | "connecting" | "live";

export function VoicePage() {
  const { agents, selectedAgent, refresh } = useAgentSession();
  const configuredApiUrl = agents.find((agent) => agent.name === selectedAgent)?.api_url || "";
  if (!selectedAgent) {
    return (
      <PageShell className="channels-page voice-page">
        <PageToolbar title="Voice" subtitle="Hold to talk from this browser when no speaker is nearby." />
        <div className="channels-content voice-content">
          <p className="wizard-hint">Create an agent before pairing this browser as a microphone.</p>
        </div>
      </PageShell>
    );
  }
  return (
    <VoiceSession
      key={selectedAgent}
      agentName={selectedAgent}
      configuredApiUrl={configuredApiUrl}
      refreshAgents={refresh}
    />
  );
}

function VoiceSession({
  agentName,
  configuredApiUrl,
  refreshAgents,
}: {
  agentName: string;
  configuredApiUrl: string;
  refreshAgents: () => Promise<void>;
}) {
  const [status, setStatus] = useState<DeviceStatus | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [holding, setHolding] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [floor, setFloor] = useState("idle");
  const [level, setLevel] = useState(0);
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [roomName, setRoomName] = useState("");
  const clientRef = useRef<PhoneVoiceClient | null>(null);
  const transcriptId = useRef(1);
  const autoConnectRef = useRef(false);
  const userStoppedRef = useRef(false);
  const holdingRef = useRef(false);
  const saved = useMemo(
    () => readStoredCredential(localStorage, agentName),
    [agentName, revision],
  );
  const apiUrl = configuredApiUrl || status?.api_url || "";
  const endpoint = useMemo(() => {
    if (!apiUrl) return "";
    try {
      return describeVoiceSocket(apiUrl, PHONE_DEVICE_ID);
    } catch {
      return "";
    }
  }, [apiUrl]);

  const releaseHold = useCallback(() => {
    holdingRef.current = false;
    setHolding(false);
    setLevel(0);
    void clientRef.current?.setHolding(false);
  }, []);

  const showError = useCallback((err: unknown) => {
    if (err instanceof VoiceConnectionSuperseded) return;
    setPhase("idle");
    setError(err instanceof Error ? err.message : String(err));
  }, []);

  useEffect(() => {
    const client = new PhoneVoiceClient({
      onLive: (frame) => {
        setPhase("live");
        setError("");
        setRoomName(frame.room_name || PHONE_DEVICE_ID);
        if (typeof frame.credential === "string" && frame.credential.startsWith("dvc_")) {
          writeStoredCredential(localStorage, agentName, frame.credential);
          setRevision((value) => value + 1);
        }
      },
      onFloor: setFloor,
      onPlaying: setPlaying,
      onLevel: setLevel,
      onTranscript: (frame) => {
        setLines((current) => applyTranscript(current, frame, () => transcriptId.current++));
      },
      onClose: ({ code, reason }) => {
        releaseHold();
        setPhase("idle");
        setPlaying(false);
        if (code === 1000 || code === 1001) return;
        setError(reason || (code === 1008 ? "The Agent rejected this phone credential." : "The voice connection closed."));
      },
      onNotice: setError,
    });
    clientRef.current = client;
    return () => {
      client.disconnect();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [agentName, releaseHold]);

  const loadDevices = useCallback(async () => {
    const data = await getDevices();
    setStatus(data);
    return data;
  }, []);

  useEffect(() => {
    let cancelled = false;
    void loadDevices().catch((err: unknown) => {
      if (!cancelled) showError(err);
    });
    const interval = window.setInterval(() => {
      void loadDevices().catch(() => undefined);
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [loadDevices, showError]);

  const connectWith = useCallback(async (token: string) => {
    if (!apiUrl) throw new Error("This agent has no API URL. Set web.api_url to the devices host.");
    const client = clientRef.current;
    if (!client) throw new Error("Voice client is not ready.");
    setPhase("connecting");
    setError("");
    setNotice("");
    await client.connect({ apiUrl, token, pageProtocol: window.location.protocol });
  }, [apiUrl]);

  useEffect(() => {
    if (autoConnectRef.current || userStoppedRef.current) return;
    if (!saved || !apiUrl || !status?.api_available || phase === "live" || phase === "connecting") return;
    autoConnectRef.current = true;
    void connectWith(saved).catch(showError);
  }, [apiUrl, connectWith, phase, saved, showError, status]);

  useEffect(() => {
    const stop = () => releaseHold();
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", stop);
    return () => {
      window.removeEventListener("blur", stop);
      document.removeEventListener("visibilitychange", stop);
    };
  }, [releaseHold]);

  const beginHold = () => {
    if (phase !== "live" || holdingRef.current) return;
    holdingRef.current = true;
    setHolding(true);
    void clientRef.current?.setHolding(true);
  };

  const pair = async () => {
    userStoppedRef.current = false;
    setPhase("connecting");
    setError("");
    setNotice("");
    try {
      const pairing = await createDevicePairing();
      await connectWith(pairing.pairing_code);
      setNotice("Phone paired. Hold the button to talk. Devices will show phone-web online for voice.");
      void loadDevices().catch(() => undefined);
    } catch (err) {
      await cancelDevicePairing().catch(() => undefined);
      if (err instanceof VoiceConnectionSuperseded) return;
      clientRef.current?.disconnect();
      setPhase("idle");
      const message = err instanceof Error ? err.message : String(err);
      setError(
        /reject|1008|already paired|invalid pairing/i.test(message)
          ? `${message} If phone-web is already listed on Devices, remove it and pair this browser again.`
          : message,
      );
    }
  };

  const forget = () => {
    userStoppedRef.current = true;
    autoConnectRef.current = true;
    writeStoredCredential(localStorage, agentName, "");
    setRevision((value) => value + 1);
    clientRef.current?.disconnect();
    releaseHold();
    setPhase("idle");
    setNotice("Saved credential removed from this browser. Remove phone-web on Devices to revoke it on the Agent.");
  };

  const disconnect = () => {
    userStoppedRef.current = true;
    clientRef.current?.disconnect();
    releaseHold();
    setPhase("idle");
    setPlaying(false);
    setNotice("Disconnected. The saved credential is still in this browser.");
  };

  const live = phase === "live";
  const badgeTone = live ? "good" : phase === "connecting" ? "info" : error ? "danger" : "muted";
  const badgeLabel = live ? "Live" : phase === "connecting" ? "Connecting" : !status && !error ? "Loading" : "Offline";
  const talkLabel = holding ? "Release to send" : playing ? "Speaking" : "Hold to talk";
  const activity = !live ? "Idle" : holding ? "Mic open" : playing ? "Playing" : floor === "thinking" ? "Thinking" : "Listening";

  return (
    <PageShell className="channels-page voice-page">
      <PageToolbar
        title="Voice"
        subtitle="Hold to talk from this browser when no speaker is nearby."
        actions={<StatusBadge tone={badgeTone}>{badgeLabel}</StatusBadge>}
      />
      <div className="channels-content voice-content">
        {error ? <div className="error-strip">{error}</div> : null}
        {notice ? <div className="success-strip">{notice}</div> : null}
        {status && !status.runtime_available ? (
          <div className="device-readiness-banner">
            <p className="wizard-hint">Start the Agent runtime before pairing this phone.</p>
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                void (async () => {
                  setError("");
                  try {
                    await startRuntime();
                    await refreshAgents();
                    await loadDevices();
                    setNotice("Agent started.");
                  } catch (err) {
                    showError(err);
                  }
                })();
              }}
            >
              <Play size={13} /> Start Agent
            </Button>
          </div>
        ) : null}
        {status?.runtime_available && !status.api_available ? (
          <div className="device-readiness-banner">
            <p className="wizard-hint">Connect the API channel before opening the voice socket.</p>
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                void (async () => {
                  setError("");
                  try {
                    await connectChannel("api");
                    await loadDevices();
                    setNotice("API channel connected.");
                  } catch (err) {
                    showError(err);
                  }
                })();
              }}
            >
              <Plug size={13} /> Connect API
            </Button>
          </div>
        ) : null}
        <Panel className="voice-panel">
          <p className="voice-endpoint">
            Device <code>{PHONE_DEVICE_ID}</code>
            {roomName && live ? ` · ${roomName}` : ""}
            {endpoint ? <> · <code>{endpoint}</code></> : null}
          </p>
          <p className="wizard-hint">
            The socket opens on the Agent API from web.api_url and authenticates with ?token=.
            The credential stays in this browser. ESP devices keep using Authorization: Bearer.
            While connected, Devices lists phone-web as online for voice.
          </p>
          <div className="toolbar-actions">
            {!saved && !live ? (
              <Button
                type="button"
                variant="primary"
                disabled={phase === "connecting" || !status?.api_available || !apiUrl}
                onClick={() => {
                  clientRef.current?.prime();
                  void pair();
                }}
              >
                Pair this phone
              </Button>
            ) : null}
            {saved && !live ? (
              <Button
                type="button"
                variant="primary"
                disabled={phase === "connecting" || !status?.api_available || !apiUrl}
                onClick={() => {
                  userStoppedRef.current = false;
                  clientRef.current?.prime();
                  void connectWith(saved).catch(showError);
                }}
              >
                Connect
              </Button>
            ) : null}
            {live ? <Button type="button" onClick={disconnect}>Disconnect</Button> : null}
            {saved ? <Button type="button" variant="ghost" onClick={forget}>Forget saved credential</Button> : null}
          </div>
        </Panel>
        <Panel className="voice-panel">
          <div className="voice-meta">
            <StatusBadge tone={holding ? "good" : playing ? "info" : "muted"}>{activity}</StatusBadge>
            <span className="wizard-hint">Use headphones if the speaker feeds back into the mic.</span>
          </div>
          <div className="talk-wrap">
            <button
              type="button"
              className={holding ? "talk-button holding" : playing ? "talk-button playing" : "talk-button"}
              disabled={!live}
              aria-pressed={holding}
              aria-label={talkLabel}
              onPointerDown={(event) => {
                if (event.button !== 0) return;
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
                beginHold();
              }}
              onPointerUp={releaseHold}
              onPointerCancel={releaseHold}
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
            >
              <Mic size={28} />
              <span>{talkLabel}</span>
            </button>
            <div className="talk-level" aria-hidden="true">
              <span style={{ width: `${Math.round(level * 100)}%` }} />
            </div>
          </div>
          <ul className="voice-transcript">
            {lines.length === 0 ? <li className="voice-transcript-empty">Transcripts appear here after you speak.</li> : null}
            {lines.map((line) => (
              <li key={line.id}>
                <span className="role">{line.role === "user" ? "You" : "Agent"}</span>
                {line.text}
              </li>
            ))}
          </ul>
        </Panel>
      </div>
    </PageShell>
  );
}
