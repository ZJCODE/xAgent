import { ArrowUpRight, Globe2, MessageCircle, Mic, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChannelSetupWizard } from "../components/ChannelSetupWizard";
import { Button, IconButton, PageShell, PageToolbar } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { connectChannel, disconnectChannel, getChannels, restartRuntime, startRuntime } from "../lib/api";
import type { ChannelId, ChannelStatus, ChannelsResponse } from "../types";

type ConnectionAction = "connect" | "disconnect";
type RuntimeAction = "start" | "restart";
function isSetupChannel(channel: ChannelId): channel is "weixin" {
  return channel === "weixin";
}

function channelLabel(channel: ChannelStatus): string {
  if (channel.id === "voice") {
    if (["running", "connected"].includes(channel.status)) return "Running";
    if (["starting", "connecting", "reconnecting"].includes(channel.status)) return "Starting";
    if (channel.status === "disconnecting") return "Stopping";
    if (["error", "failed"].includes(channel.status)) return "Needs attention";
    return "Stopped";
  }
  if (channel.status === "running" || channel.status === "connected") return "Connected";
  if (["starting", "connecting"].includes(channel.status)) return "Connecting";
  if (channel.status === "disconnecting") return "Disconnecting";
  if (channel.status === "disconnected") return "Available to connect";
  if (channel.status === "reconnecting") return "Reconnecting";
  if (channel.status === "error" || channel.status === "failed") return "Needs attention";
  if (channel.status === "disabled") return "Disabled";
  return "Stopped";
}

const CHANNEL_ICONS = { api: Globe2, voice: Mic, weixin: MessageCircle };
const CHANNEL_DESCRIPTIONS = {
  api: "Connect your apps over HTTP and WebSocket",
  voice: "Use the microphone and speaker on the computer running your Agent",
  weixin: "Conversations through your WeChat account",
};

function ChannelRow({ channel, onSetup, onConnection, pending, disabled }: {
  channel: ChannelStatus;
  onSetup: (channel: "weixin") => void;
  onConnection: (channel: ChannelId, action: ConnectionAction) => void;
  pending?: ConnectionAction;
  disabled: boolean;
}) {
  const failed = channel.status === "error" || channel.status === "failed";
  const connected = channel.status === "running" || channel.status === "connected";
  const Icon = CHANNEL_ICONS[channel.id];
  const hostVoice = channel.id === "voice";
  return (
    <div className={`connection-row connection-row-${channel.status}`}>
      <span className="connection-icon" aria-hidden="true"><Icon size={19} strokeWidth={1.65} /></span>
      <div className="connection-copy">
        <h3>{hostVoice ? "Host voice" : channel.label}</h3>
        <p>{hostVoice && !channel.ready ? "Configure shared voice settings in Agent." : (failed || !channel.ready) && channel.detail ? channel.detail : CHANNEL_DESCRIPTIONS[channel.id]}</p>
      </div>
      <span className={`connection-state ${failed ? "is-failed" : connected ? "is-connected" : ""}`}>
        <span aria-hidden="true" />{channel.ready ? channelLabel(channel) : channel.configured ? "Check configuration" : "Not configured"}
      </span>
      <div className="connection-action">
        {channel.can_disconnect || pending === "disconnect" || channel.status === "disconnecting" ? (
          <Button type="button" variant="secondary" disabled={disabled || Boolean(pending) || channel.status === "disconnecting"}
            onClick={() => onConnection(channel.id, "disconnect")}>
            {pending === "disconnect" || channel.status === "disconnecting" ? hostVoice ? "Stopping…" : "Disconnecting…" : hostVoice ? "Stop" : "Disconnect"}
          </Button>
        ) : channel.ready ? (
          <Button type="button" variant="secondary" disabled={disabled || Boolean(pending) || !channel.can_connect}
            onClick={() => onConnection(channel.id, "connect")}>
            {pending === "connect" ? hostVoice ? "Starting…" : "Connecting…" : hostVoice ? "Start" : "Connect"}
          </Button>
        ) : null}
        {isSetupChannel(channel.id) ? (
          <Button type="button" variant="ghost" onClick={() => onSetup("weixin")}>
            {channel.ready ? "Configure" : "Set up"}<ArrowUpRight size={14} />
          </Button>
        ) : null}
        {hostVoice && !channel.ready ? <a className="chat-channel-link" href="/agent#voice">Configure in Agent<ArrowUpRight size={14} /></a> : null}
      </div>
    </div>
  );
}

export function ChannelPage() {
  const { selectedAgent, refresh: refreshAgents } = useAgentSession();
  const [data, setData] = useState<ChannelsResponse | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<RuntimeAction | null>(null);
  const [connectionPending, setConnectionPending] = useState<Partial<Record<ChannelId, ConnectionAction>>>({});
  const currentAgent = useRef(selectedAgent);
  const sessionEpoch = useRef(0);
  if (currentAgent.current !== selectedAgent) {
    currentAgent.current = selectedAgent;
    sessionEpoch.current += 1;
  }
  const loadSequence = useRef(0);
  const [setupChannelId, setSetupChannelId] = useState<"weixin" | null>(null);

  const load = useCallback(async (silent = false) => {
    const agent = currentAgent.current;
    const epoch = sessionEpoch.current;
    const sequence = ++loadSequence.current;
    if (!silent) setError("");
    try {
      const result = await getChannels();
      if (agent === currentAgent.current && epoch === sessionEpoch.current && sequence === loadSequence.current) setData(result);
    } catch (err) {
      if (!silent && agent === currentAgent.current && epoch === sessionEpoch.current && sequence === loadSequence.current) setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    setData(null); setError(""); setNotice(""); setPending(null); setConnectionPending({}); setSetupChannelId(null);
    void load();
    const interval = window.setInterval(() => void load(true), 5000);
    return () => { window.clearInterval(interval); loadSequence.current += 1; sessionEpoch.current += 1; };
  }, [load, selectedAgent]);

  const runAction = async (action: RuntimeAction) => {
    const agent = currentAgent.current;
    const epoch = sessionEpoch.current;
    loadSequence.current += 1;
    setPending(action);
    setError("");
    setNotice("");
    try {
      let restartWarnings: string[] = [];
      if (action === "start") await startRuntime();
      if (action === "restart") restartWarnings = (await restartRuntime()).restart_warnings ?? [];
      if (agent !== currentAgent.current || epoch !== sessionEpoch.current) return;
      await load();
      if (agent !== currentAgent.current || epoch !== sessionEpoch.current) return;
      await refreshAgents();
      if (agent !== currentAgent.current || epoch !== sessionEpoch.current) return;
      const message = action === "restart" ? "Agent restarted. Check channel status for connection progress." : "Agent started.";
      setNotice([message, ...restartWarnings].join(" "));
    } catch (err) { if (agent === currentAgent.current && epoch === sessionEpoch.current) setError(err instanceof Error ? err.message : String(err)); }
    finally { if (agent === currentAgent.current && epoch === sessionEpoch.current) setPending(null); }
  };

  const runConnection = async (channel: ChannelId, action: ConnectionAction) => {
    const agent = currentAgent.current;
    const epoch = sessionEpoch.current;
    loadSequence.current += 1;
    setConnectionPending((previous) => ({ ...previous, [channel]: action }));
    setError(""); setNotice("");
    try {
      if (action === "connect") await connectChannel(channel);
      else await disconnectChannel(channel);
      if (agent !== currentAgent.current || epoch !== sessionEpoch.current) return;
      await load();
    } catch (err) {
      if (agent === currentAgent.current && epoch === sessionEpoch.current) {
        setError(err instanceof Error ? err.message : String(err));
        await load(true);
      }
    } finally {
      if (agent === currentAgent.current && epoch === sessionEpoch.current) setConnectionPending((previous) => {
        const next = { ...previous }; delete next[channel]; return next;
      });
    }
  };

  const runtime = data?.runtime;
  const running = Boolean(runtime?.runtime_running);
  const state = runtime?.status || "stopped";
  const transitioning = state === "starting" || state === "stopping";
  const busy = Boolean(pending) || transitioning;
  const renderEpoch = sessionEpoch.current;

  return (
    <PageShell className="channels-page">
      <PageToolbar title="Channels" subtitle="One Agent, connected everywhere."
        actions={<IconButton type="button" onClick={() => void load()} title="Refresh status"><RefreshCw size={16} /></IconButton>} />
      <div className="channels-content">
      {error ? <div className="error-strip" role="alert">{error}</div> : null}
      {notice ? <div className="success-strip">{notice}</div> : null}
      {runtime && (!runtime.runtime_ready || runtime.needs_restart) ? (
        <div className="channel-runtime-notice">
          <p>{transitioning ? "Agent is changing state. Connections will be available when it is ready."
            : runtime.needs_restart && running ? "Settings saved. Restart the Agent to apply them."
            : running ? "Agent is not ready. Check its status before connecting channels."
            : "Start the Agent to connect your channels."}</p>
          <div className="toolbar-actions">
            {!running ? <Button type="button" disabled={busy} onClick={() => void runAction("start")}><Play size={13} />{pending === "start" ? "Starting…" : "Start Agent"}</Button>
              : runtime.needs_restart ? <Button type="button" disabled={busy} onClick={() => void runAction("restart")}><RefreshCw size={14} />{pending === "restart" ? "Restarting…" : "Restart Agent"}</Button> : null}
            <a className="runtime-detail-link" href="/agent">Manage Agent<ArrowUpRight size={14} /></a>
          </div>
        </div>
      ) : null}
      {runtime?.error ? <div className="error-strip" role="alert">{runtime.error}</div> : null}
      <div className="connections-heading"><h3>Connections</h3><p>API starts by default. Connect other channels when needed.</p></div>
      <div className="connection-list">{data?.channels.map((channel) => <ChannelRow key={channel.id} channel={channel} onSetup={setSetupChannelId} onConnection={(id, action) => void runConnection(id, action)} pending={connectionPending[channel.id]} disabled={busy || !runtime?.runtime_ready} />)}</div>
      <p className="connections-footnote">Host voice runs on the Agent’s computer. Browser voice works independently in the Voice tab. Manage all voice settings in Agent.</p>
      </div>
      {setupChannelId ? <ChannelSetupWizard channel={setupChannelId} open onClose={() => setSetupChannelId(null)} onComplete={() => {
        if (renderEpoch !== sessionEpoch.current) return;
        setNotice("Channel settings saved. Restart the Agent to apply them.");
        void load(); void refreshAgents();
      }} /> : null}
    </PageShell>
  );
}
