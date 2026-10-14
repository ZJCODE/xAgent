import { ArrowUpRight, FileText, Globe2, MessageCircle, Mic, Play, RefreshCw, Square, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChannelSetupWizard } from "../components/ChannelSetupWizard";
import { Button, IconButton, PageShell, PageToolbar, Panel, StatusBadge } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { connectChannel, disconnectChannel, getRuntimeLogs, getChannels, restartRuntime, startRuntime, stopRuntime } from "../lib/api";
import type { AgentRuntimeStatus, ChannelId, ChannelStatus, ChannelsResponse, SetupChannelId } from "../types";

type ConnectionAction = "connect" | "disconnect";
type RuntimeAction = "start" | "stop" | "restart";
const SETUP_CHANNELS = new Set<ChannelId>(["voice", "feishu", "weixin"]);

function isSetupChannel(channel: ChannelId): channel is SetupChannelId {
  return SETUP_CHANNELS.has(channel);
}

function channelLabel(channel: ChannelStatus): string {
  if (channel.status === "running" || channel.status === "connected") return "Connected";
  if (["starting", "connecting"].includes(channel.status)) return "Connecting";
  if (channel.status === "disconnecting") return "Disconnecting";
  if (channel.status === "disconnected") return "Available to connect";
  if (channel.status === "reconnecting") return "Reconnecting";
  if (channel.status === "error" || channel.status === "failed") return "Needs attention";
  if (channel.status === "disabled") return "Disabled";
  return "Stopped";
}

const CHANNEL_ICONS = { api: Globe2, voice: Mic, feishu: MessageCircle, weixin: MessageCircle };
const CHANNEL_DESCRIPTIONS = {
  api: "Connect your apps over HTTP and WebSocket",
  voice: "Listen and speak on this device",
  feishu: "Private and group conversations in Feishu",
  weixin: "Conversations through your WeChat account",
};

function ChannelRow({ channel, onSetup, onConnection, pending, disabled }: {
  channel: ChannelStatus;
  onSetup: (channel: SetupChannelId) => void;
  onConnection: (channel: ChannelId, action: ConnectionAction) => void;
  pending?: ConnectionAction;
  disabled: boolean;
}) {
  const failed = channel.status === "error" || channel.status === "failed";
  const connected = channel.status === "running" || channel.status === "connected";
  const Icon = CHANNEL_ICONS[channel.id];
  return (
    <div className={`connection-row connection-row-${channel.status}`}>
      <span className="connection-icon" aria-hidden="true"><Icon size={19} strokeWidth={1.65} /></span>
      <div className="connection-copy">
        <h3>{channel.label}</h3>
        <p>{(failed || !channel.ready) && channel.detail ? channel.detail : CHANNEL_DESCRIPTIONS[channel.id]}</p>
      </div>
      <span className={`connection-state ${failed ? "is-failed" : connected ? "is-connected" : ""}`}>
        <span aria-hidden="true" />{channel.ready ? channelLabel(channel) : channel.configured ? "Check configuration" : "Not configured"}
      </span>
      <div className="connection-action">
        {channel.can_disconnect || pending === "disconnect" || channel.status === "disconnecting" ? (
          <Button type="button" variant="secondary" disabled={disabled || Boolean(pending) || channel.status === "disconnecting"}
            onClick={() => onConnection(channel.id, "disconnect")}>
            {pending === "disconnect" || channel.status === "disconnecting" ? "Disconnecting…" : "Disconnect"}
          </Button>
        ) : channel.ready ? (
          <Button type="button" variant="secondary" disabled={disabled || Boolean(pending) || !channel.can_connect}
            onClick={() => onConnection(channel.id, "connect")}>
            {pending === "connect" ? "Connecting…" : "Connect"}
          </Button>
        ) : null}
        {isSetupChannel(channel.id) ? (
          <Button type="button" variant="ghost" onClick={() => onSetup(channel.id as SetupChannelId)}>
            {channel.ready ? "Configure" : "Set up"}<ArrowUpRight size={14} />
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function RuntimeDetails({ runtime }: { runtime: AgentRuntimeStatus }) {
  const memory = runtime.memory || runtime.journal;
  const review = runtime.tasks?.needs_review;
  const taskReviewCount = Array.isArray(review) ? review.length : review || 0;
  const last = memory?.last_maintenance;
  const maintenanceLabels: Record<string, string> = {
    running: "Updating", completed: "Up to date", idle: "Waiting",
    failed: "Needs attention", needs_review: "Needs checking", interrupted: "Interrupted", prepared: "Finishing an update",
  };
  return (
    <>
      <dl className="runtime-summary-grid">
        <div><dt>Reply</dt><dd title={runtime.queue?.active_turn_id || undefined}>{runtime.queue?.active_turn_id ? "In progress" : "Idle"}</dd></div>
        <div><dt>In queue</dt><dd>{runtime.queue?.pending ?? 0}</dd></div>
        <div><dt>Diary backlog</dt><dd>{memory?.backlog ?? 0}</dd></div>
        <div><dt>Last update</dt><dd title={last?.finished_at ? new Date(last.finished_at).toLocaleString() : undefined}>{last?.status ? maintenanceLabels[last.status] || last.status : "No update yet"}{last?.finished_at ? "" : ""}</dd></div>
      </dl>
      {taskReviewCount ? <div className="warning-strip"><a href="/tasks">{taskReviewCount} task{taskReviewCount === 1 ? "" : "s"} need checking.</a> Review the result before retrying.</div> : null}
      {memory?.needs_review?.map((item, index) => <div className="warning-strip" key={index}>A diary update needs checking. {item.reason}</div>)}
      {last?.error ? <p className="task-error-copy">Diary update: {last.error}</p> : null}
    </>
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
  const [logs, setLogs] = useState<string | null>(null);
  const [logsBusy, setLogsBusy] = useState(false);
  const [setupChannelId, setSetupChannelId] = useState<SetupChannelId | null>(null);

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
    setData(null); setError(""); setNotice(""); setPending(null); setConnectionPending({}); setLogs(null); setLogsBusy(false); setSetupChannelId(null);
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
      if (action === "stop") await stopRuntime();
      if (action === "restart") restartWarnings = (await restartRuntime()).restart_warnings ?? [];
      if (agent !== currentAgent.current || epoch !== sessionEpoch.current) return;
      await load();
      if (agent !== currentAgent.current || epoch !== sessionEpoch.current) return;
      await refreshAgents();
      if (agent !== currentAgent.current || epoch !== sessionEpoch.current) return;
      const message = action === "stop" ? "Agent stopped." : action === "restart" ? "Agent restarted. Check channel status for connection progress." : "Agent started.";
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

  const loadLogs = useCallback(async () => {
    const agent = currentAgent.current;
    const epoch = sessionEpoch.current;
    setLogsBusy(true);
    try {
      const result = await getRuntimeLogs();
      if (agent === currentAgent.current && epoch === sessionEpoch.current) setLogs(result.text);
    } catch (err) { if (agent === currentAgent.current && epoch === sessionEpoch.current) setError(err instanceof Error ? err.message : String(err)); }
    finally { if (agent === currentAgent.current && epoch === sessionEpoch.current) setLogsBusy(false); }
  }, []);

  const logsOpen = logs !== null;
  useEffect(() => {
    if (!logsOpen) return;
    const interval = window.setInterval(() => void loadLogs(), 3000);
    return () => window.clearInterval(interval);
  }, [logsOpen, loadLogs]);

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
      <Panel className="runtime-overview">
        <div className="runtime-heading">
          <div className="runtime-heading-copy"><span className="runtime-eyebrow">AGENT</span><h3>{selectedAgent || "Your Agent"}</h3><p>A shared home for conversations, tasks and memory.</p></div>
          <StatusBadge tone={state === "degraded" ? "info" : running && runtime?.runtime_ready ? "good" : "muted"}>
            <span className="runtime-status-dot" aria-hidden="true" />
            {state === "degraded" ? "Needs attention" : state === "starting" ? "Starting" : state === "stopping" ? "Stopping" : running ? "Running" : "Offline"}
          </StatusBadge>
        </div>
        <div className="runtime-controls">
          {running ? <Button type="button" disabled={busy} onClick={() => void runAction("stop")}><Square size={13} />{pending === "stop" ? "Stopping…" : "Stop Agent"}</Button>
          : <Button type="button" variant="primary" disabled={!runtime || busy} onClick={() => void runAction("start")}><Play size={13} />{pending === "start" ? "Starting…" : "Start Agent"}</Button>}
          {running ? <Button type="button" variant="ghost" disabled={busy} onClick={() => void runAction("restart")}><RefreshCw size={14} />{pending === "restart" ? "Restarting…" : "Restart"}</Button> : null}
          <Button type="button" variant="ghost" className="runtime-log-toggle" title={logsOpen ? "Hide logs" : "View logs"} aria-label={logsOpen ? "Hide logs" : "View logs"} disabled={logsBusy} onClick={() => logsOpen ? setLogs(null) : void loadLogs()}><FileText size={14} /><span>{logsOpen ? "Hide logs" : "View logs"}</span></Button>
        </div>
        {runtime?.needs_restart ? <div className="warning-strip">Settings saved. Restart the Agent to apply them.</div> : null}
        {runtime?.error ? <p className="task-error-copy">{runtime.error}</p> : null}
        {running && runtime ? <RuntimeDetails runtime={runtime} /> : null}
        {logsOpen ? <div className="channel-log-panel">
          <div className="channel-log-header"><span>Agent logs · Auto refresh</span><IconButton title="Close logs" onClick={() => setLogs(null)}><X size={15} /></IconButton></div>
          <pre className="channel-log-output">{logs?.trim() || "No log output yet."}</pre>
        </div> : null}
      </Panel>
      <div className="connections-heading"><h3>Connections</h3><p>API starts by default. Connect other channels when needed.</p></div>
      <div className="connection-list">{data?.channels.map((channel) => <ChannelRow key={channel.id} channel={channel} onSetup={setSetupChannelId} onConnection={(id, action) => void runConnection(id, action)} pending={connectionPending[channel.id]} disabled={busy || !runtime?.runtime_ready} />)}</div>
      <p className="connections-footnote">Restart restores active connections, including Voice, unless disabled in the new settings. Stop then Start uses default connections. Local chat also works with the public API disconnected.</p>
      </div>
      {setupChannelId ? <ChannelSetupWizard channel={setupChannelId} open onClose={() => setSetupChannelId(null)} onComplete={() => {
        if (renderEpoch !== sessionEpoch.current) return;
        setNotice("Channel settings saved. Restart the Agent to apply them.");
        void load(); void refreshAgents();
      }} /> : null}
    </PageShell>
  );
}
