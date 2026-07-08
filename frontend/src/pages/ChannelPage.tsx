import { FileText, Play, RefreshCw, Square, Wrench, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ChannelSetupWizard } from "../components/ChannelSetupWizard";
import { Button, IconButton, PageShell, PageToolbar, Panel, StatusBadge } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import {
  getChannelLogs,
  getChannels,
  startChannel,
  stopChannel,
} from "../lib/api";
import type { ChannelId, ChannelStatus, ChannelsResponse, ChannelRuntimeStatus, SetupChannelId } from "../types";

type PendingAction = "start" | "stop" | "logs";

const SETUP_CHANNELS = new Set<ChannelId>(["voice", "feishu", "weixin"]);

function isSetupChannel(channel: ChannelId): channel is SetupChannelId {
  return SETUP_CHANNELS.has(channel);
}

function statusTone(status: ChannelRuntimeStatus): "good" | "danger" | "muted" | "info" {
  if (status === "running") return "good";
  if (status === "error") return "danger";
  if (status === "stopped") return "muted";
  return "muted";
}

function statusLabel(channel: ChannelStatus): string {
  if (channel.status === "running") return "Running";
  if (channel.status === "stopped") return "Stopped";
  if (channel.status === "error") return "Needs attention";
  return "Disabled";
}

function ChannelStatusMeta({
  channel,
  onSetup,
}: {
  channel: ChannelStatus;
  onSetup: (channel: SetupChannelId) => void;
}) {
  if (!channel.ready && isSetupChannel(channel.id)) {
    const setupId = channel.id;
    return (
      <Button
        type="button"
        variant="primary"
        className="channel-setup-button"
        onClick={() => onSetup(setupId)}
      >
        <Wrench size={13} />
        Set up
      </Button>
    );
  }

  if (!channel.ready) {
    return <StatusBadge tone="muted">Not configured</StatusBadge>;
  }

  return <StatusBadge tone={statusTone(channel.status)}>{statusLabel(channel)}</StatusBadge>;
}

function ChannelRow({
  channel,
  pending,
  logs,
  onStart,
  onStop,
  onLogs,
  onRefreshLogs,
  onCloseLogs,
  onSetup,
}: {
  channel: ChannelStatus;
  pending?: PendingAction;
  logs?: string;
  onStart: (channel: ChannelId) => void;
  onStop: (channel: ChannelId) => void;
  onLogs: (channel: ChannelId) => void;
  onRefreshLogs: (channel: ChannelId) => void;
  onCloseLogs: (channel: ChannelId) => void;
  onSetup: (channel: SetupChannelId) => void;
}) {
  const actionBusy = Boolean(pending && pending !== "logs");
  const logsBusy = pending === "logs";
  const logsOpen = logs !== undefined;
  return (
    <Panel className={`channel-row channel-row-${channel.status}`} aria-busy={Boolean(pending)}>
      <header className="channel-row-header">
        <h3 className="channel-row-title">{channel.label}</h3>
        <div className="channel-row-meta">
          <ChannelStatusMeta channel={channel} onSetup={onSetup} />
        </div>
      </header>

      <div className="channel-row-actions">
        <Button
          type="button"
          variant="primary"
          disabled={!channel.can_start || actionBusy}
          onClick={() => onStart(channel.id)}
        >
            <Play size={13} />
            {pending === "start" ? "Starting" : "Start"}
          </Button>
          <Button
            type="button"
            disabled={!channel.can_stop || actionBusy}
            onClick={() => onStop(channel.id)}
          >
            <Square size={13} />
            {pending === "stop" ? "Stopping" : "Stop"}
          </Button>
          <Button
            type="button"
            disabled={logsBusy}
            onClick={() => onLogs(channel.id)}
          >
            <FileText size={13} />
          {logsBusy ? "Loading" : "Logs"}
        </Button>
      </div>

      {logsOpen ? (
        <div className="channel-log-panel">
          <div className="channel-log-header">
            <span>Recent logs · Auto refresh</span>
            <div className="channel-log-actions">
              <IconButton
                type="button"
                disabled={logsBusy}
                onClick={() => onRefreshLogs(channel.id)}
                title="Refresh logs"
                aria-label={`Refresh ${channel.label} logs`}
              >
                <RefreshCw size={15} />
              </IconButton>
              <IconButton
                type="button"
                onClick={() => onCloseLogs(channel.id)}
                title="Close logs"
                aria-label={`Close ${channel.label} logs`}
              >
                <X size={15} />
              </IconButton>
            </div>
          </div>
          <pre className="channel-log-output">{logs.trim() || "(no log output)"}</pre>
        </div>
      ) : null}
    </Panel>
  );
}

export function ChannelPage() {
  const { selectedAgent, refresh: refreshAgents } = useAgentSession();
  const [data, setData] = useState<ChannelsResponse | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<Partial<Record<ChannelId, PendingAction>>>({});
  const [logs, setLogs] = useState<Partial<Record<ChannelId, string>>>({});
  const [setupChannelId, setSetupChannelId] = useState<SetupChannelId | null>(null);
  const logRequestsRef = useRef<Partial<Record<ChannelId, boolean>>>({});

  const channels = useMemo(() => data?.channels || [], [data]);

  const load = async () => {
    setError("");
    try {
      const next = await getChannels();
      setData(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const runAction = async (channel: ChannelId, action: Exclude<PendingAction, "logs">) => {
    setPending((current) => ({ ...current, [channel]: action }));
    setError("");
    setNotice("");
    try {
      if (action === "start") await startChannel(channel);
      if (action === "stop") await stopChannel(channel);
      await load();
      await refreshAgents();
      setNotice(`${channel} ${action} complete.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending((current) => {
        const next = { ...current };
        delete next[channel];
        return next;
      });
    }
  };

  const loadLogs = async (channel: ChannelId, options?: { silent?: boolean }) => {
    if (logRequestsRef.current[channel]) return;
    logRequestsRef.current[channel] = true;
    const silent = Boolean(options?.silent);
    if (!silent) {
      setPending((current) => ({ ...current, [channel]: "logs" }));
      setError("");
    }
    try {
      const result = await getChannelLogs(channel);
      setLogs((current) => ({ ...current, [channel]: result.text }));
    } catch (err) {
      if (!silent) setError(err instanceof Error ? err.message : String(err));
    } finally {
      delete logRequestsRef.current[channel];
      if (!silent) {
        setPending((current) => {
          const next = { ...current };
          delete next[channel];
          return next;
        });
      }
    }
  };

  const toggleLogs = async (channel: ChannelId) => {
    if (logs[channel] !== undefined) {
      setLogs((current) => {
        const next = { ...current };
        delete next[channel];
        return next;
      });
      return;
    }
    await loadLogs(channel);
  };

  const closeLogs = (channel: ChannelId) => {
    setLogs((current) => {
      const next = { ...current };
      delete next[channel];
      return next;
    });
  };

  useEffect(() => {
    const openChannels = Object.keys(logs) as ChannelId[];
    if (!openChannels.length) return undefined;

    const interval = window.setInterval(() => {
      openChannels.forEach((channel) => {
        void loadLogs(channel, { silent: true });
      });
    }, 3000);

    return () => window.clearInterval(interval);
  }, [logs]);

  return (
    <PageShell className="channels-page">
      <PageToolbar
        title="Channels"
        subtitle={selectedAgent ? `Selected agent: ${selectedAgent}` : data?.config_dir}
      />
      {error ? <div className="error-strip">{error}</div> : null}
      {notice ? <div className="success-strip">{notice}</div> : null}

      <div className="channel-list">
        {channels.map((channel) => (
          <ChannelRow
            key={channel.id}
            channel={channel}
            pending={pending[channel.id]}
            logs={logs[channel.id]}
            onStart={(id) => void runAction(id, "start")}
            onStop={(id) => void runAction(id, "stop")}
            onLogs={(id) => void toggleLogs(id)}
            onRefreshLogs={(id) => void loadLogs(id)}
            onCloseLogs={closeLogs}
            onSetup={setSetupChannelId}
          />
        ))}
      </div>

      {setupChannelId ? (
        <ChannelSetupWizard
          channel={setupChannelId}
          open={Boolean(setupChannelId)}
          onClose={() => setSetupChannelId(null)}
          onComplete={() => {
            setNotice(`${setupChannelId} setup complete.`);
            void load();
            void refreshAgents();
          }}
        />
      ) : null}
    </PageShell>
  );
}
