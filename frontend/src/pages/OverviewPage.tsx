import {
  Activity,
  Archive,
  Bot,
  Brain,
  Clock3,
  History,
  MessageSquareText,
  Play,
  Plus,
  RadioTower,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  Square,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { CreateAgentWizard } from "../components/CreateAgentWizard";
import {
  Button,
  EmptyState,
  IconButton,
  PageShell,
  PageToolbar,
  Panel,
  StatusBadge,
} from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import {
  getDeliveries,
  getOverview,
  getTasks,
  restartRuntime,
  startRuntime,
  stopRuntime,
} from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type {
  OverviewResponse,
  RoutePath,
  RuntimeDelivery,
  RuntimeTask,
} from "../types";

function total(values: Record<string, number> | undefined): number {
  return Object.values(values || {}).reduce((sum, value) => sum + value, 0);
}

function compactDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

function truncate(value: string, length = 96): string {
  const normalized = value.trim();
  return normalized.length > length ? `${normalized.slice(0, length)}…` : normalized;
}

export function OverviewPage({ onNavigate }: { onNavigate: (route: RoutePath) => void }) {
  const {
    agents,
    selectedAgent,
    deleteAgent,
    refresh: refreshAgents,
  } = useAgentSession();
  const currentAgent = agents.find((agent) => agent.name === selectedAgent) || agents[0];
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [tasks, setTasks] = useState<RuntimeTask[]>([]);
  const [deliveryIssues, setDeliveryIssues] = useState<RuntimeDelivery[]>([]);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<"start" | "stop" | "restart" | "">("");
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState("");

  const load = useCallback(async ({ quiet = false }: { quiet?: boolean } = {}) => {
    if (!quiet) setLoading(true);
    setError("");
    try {
      const summary = await getOverview();
      setOverview(summary);
      if (summary.runtime.running) {
        const [taskData, blocked, unknown, failed] = await Promise.all([
          getTasks("active"),
          getDeliveries("blocked"),
          getDeliveries("unknown"),
          getDeliveries("failed"),
        ]);
        setTasks(taskData.tasks.slice(0, 4));
        setDeliveryIssues([
          ...blocked.deliveries,
          ...unknown.deliveries,
          ...failed.deliveries,
        ]);
      } else {
        setTasks([]);
        setDeliveryIssues([]);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load({ quiet: true }), 5000);
    return () => window.clearInterval(timer);
  }, [load, selectedAgent]);

  const lifecycle = async (action: "start" | "stop" | "restart") => {
    setPending(action);
    setError("");
    try {
      if (action === "start") await startRuntime();
      if (action === "stop") await stopRuntime();
      if (action === "restart") await restartRuntime();
      await Promise.all([load(), refreshAgents()]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPending("");
    }
  };

  const removeAgent = async () => {
    if (!currentAgent || deleteConfirm !== currentAgent.name) return;
    try {
      await deleteAgent(currentAgent.name, deleteConfirm);
      setDeleteOpen(false);
      setDeleteConfirm("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const running = Boolean(overview?.runtime.running);
  const channels = overview?.runtime.channels || [];
  const channelSummary = useMemo(
    () => ({
      running: channels.filter((channel) => channel.state === "running").length,
      attention: channels.filter((channel) => channel.state === "degraded" || Boolean(channel.error)).length,
    }),
    [channels],
  );
  const counts = overview?.counts;

  if (!currentAgent) return <EmptyState icon={<Bot size={24} />} title="No agent selected" />;

  return (
    <PageShell className="overview-page">
      <PageToolbar
        eyebrow="Agent workspace"
        title={currentAgent.title || currentAgent.name}
        subtitle={`${currentAgent.provider || "Model provider"} · ${currentAgent.model || "Model not configured"}`}
        actions={
          <>
            <Button type="button" onClick={() => setCreateOpen(true)}>
              <Plus size={15} />New agent
            </Button>
            <IconButton type="button" aria-label="Refresh overview" title="Refresh overview" onClick={() => void load()}>
              <RefreshCw size={16} />
            </IconButton>
          </>
        }
      />

      <div className="page-body dashboard-body">
        {error ? <div className="error-strip">{error}</div> : null}

        <section className="hero-runtime-card">
          <div className="hero-runtime-copy">
            <div className={`runtime-orb ${running ? "is-running" : "is-stopped"}`}>
              <Bot size={28} />
            </div>
            <div>
              <div className="hero-runtime-heading">
                <h2>{running ? "Agent is awake" : "Agent is stopped"}</h2>
                <StatusBadge tone={running ? "good" : "muted"}>
                  {running ? `PID ${overview?.runtime.pid}` : "Offline"}
                </StatusBadge>
              </div>
              <p>
                {running
                  ? `One Runtime owns the timeline. Up for ${compactDuration(overview?.runtime.uptime_seconds || 0)}.`
                  : "Start the Runtime to chat, inspect the live timeline, and run scheduled work."}
              </p>
            </div>
          </div>
          <div className="hero-runtime-actions">
            {running ? (
              <>
                <Button type="button" onClick={() => void lifecycle("restart")} disabled={Boolean(pending)}>
                  <RotateCcw size={15} />{pending === "restart" ? "Restarting…" : "Restart"}
                </Button>
                <Button type="button" variant="danger" onClick={() => void lifecycle("stop")} disabled={Boolean(pending)}>
                  <Square size={14} />{pending === "stop" ? "Stopping…" : "Stop"}
                </Button>
              </>
            ) : (
              <Button type="button" variant="primary" onClick={() => void lifecycle("start")} disabled={Boolean(pending)}>
                <Play size={15} />{pending === "start" ? "Starting…" : "Start Runtime"}
              </Button>
            )}
          </div>
        </section>

        <section className="metric-grid" aria-label="Agent statistics">
          <button type="button" className="metric-card" onClick={() => onNavigate("/messages")}>
            <span className="metric-icon metric-blue"><MessageSquareText size={18} /></span>
            <span><strong>{counts?.messages ?? "—"}</strong><small>Messages</small></span>
            <History size={15} />
          </button>
          <button type="button" className="metric-card" onClick={() => onNavigate("/memory")}>
            <span className="metric-icon metric-violet"><Brain size={18} /></span>
            <span><strong>{counts?.memory_files ?? "—"}</strong><small>Memory files</small></span>
            <Archive size={15} />
          </button>
          <button type="button" className="metric-card" onClick={() => onNavigate("/tasks")}>
            <span className="metric-icon metric-amber"><Clock3 size={18} /></span>
            <span><strong>{total(counts?.tasks)}</strong><small>Tasks</small></span>
            <Activity size={15} />
          </button>
          <button type="button" className="metric-card" onClick={() => onNavigate("/deliveries")}>
            <span className="metric-icon metric-green"><ShieldAlert size={18} /></span>
            <span><strong>{deliveryIssues.length}</strong><small>Delivery issues</small></span>
            <ShieldAlert size={15} />
          </button>
        </section>

        <div className="dashboard-grid">
          <Panel className="dashboard-panel activity-panel">
            <div className="section-heading">
              <div>
                <span className="section-kicker">Timeline</span>
                <h3>Recent activity</h3>
              </div>
              <Button type="button" variant="ghost" onClick={() => onNavigate("/messages")}>View messages</Button>
            </div>
            {overview?.recent_events.length ? (
              <div className="activity-list">
                {overview.recent_events.map((event) => (
                  <article className="activity-row" key={event.event_id}>
                    <span className={`activity-status activity-status-${event.status}`} />
                    <div className="activity-copy">
                      <div>
                        <strong>{event.kind}</strong>
                        <span>{event.source}</span>
                        <span>#{event.sequence}</span>
                      </div>
                      <p>{truncate(event.content)}</p>
                    </div>
                    <time>{formatTimestamp(event.timestamp)}</time>
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState title={loading ? "Loading activity…" : running ? "No events yet" : "Runtime is stopped"}>
                {running ? "New experiences will appear here in strict timeline order." : "Start the Agent to inspect its live activity."}
              </EmptyState>
            )}
          </Panel>

          <div className="dashboard-side">
            <Panel className="dashboard-panel">
              <div className="section-heading">
                <div>
                  <span className="section-kicker">Connections</span>
                  <h3>Channels</h3>
                </div>
                <Button type="button" variant="ghost" onClick={() => onNavigate("/channels")}>Manage</Button>
              </div>
              <div className="channel-summary">
                <div><strong>{channelSummary.running}</strong><span>running</span></div>
                <div><strong>{channels.filter((channel) => channel.enabled).length}</strong><span>enabled</span></div>
                <div className={channelSummary.attention ? "has-warning" : ""}>
                  <strong>{channelSummary.attention}</strong><span>attention</span>
                </div>
              </div>
              <div className="mini-channel-list">
                {channels.map((channel) => (
                  <div key={channel.name}>
                    <span className={`status-dot status-${channel.state}`} />
                    <strong>{channel.name}</strong>
                    <small>{channel.state}</small>
                  </div>
                ))}
                {!channels.length ? <p className="muted-copy">Channel state is available while the Runtime is running.</p> : null}
              </div>
            </Panel>

            <Panel className="dashboard-panel">
              <div className="section-heading">
                <div>
                  <span className="section-kicker">Automation</span>
                  <h3>Next tasks</h3>
                </div>
                <Button type="button" variant="ghost" onClick={() => onNavigate("/tasks")}>All tasks</Button>
              </div>
              {tasks.length ? (
                <div className="compact-list">
                  {tasks.map((task) => (
                    <div key={task.task_id}>
                      <Clock3 size={15} />
                      <span><strong>{truncate(task.instruction, 52)}</strong><small>{task.next_run_at ? formatTimestamp(task.next_run_at) : "No next run"}</small></span>
                    </div>
                  ))}
                </div>
              ) : <p className="muted-copy">{running ? "No active tasks." : "Start the Runtime to inspect tasks."}</p>}
            </Panel>
          </div>
        </div>

        {deliveryIssues.length ? (
          <button type="button" className="attention-banner" onClick={() => onNavigate("/deliveries")}>
            <RadioTower size={18} />
            <span><strong>{deliveryIssues.length} {deliveryIssues.length === 1 ? "delivery needs" : "deliveries need"} attention</strong><small>Nothing here is retried or resent automatically.</small></span>
            <span>Review</span>
          </button>
        ) : null}

        <section className="agent-danger-row">
          <div>
            <strong>Delete {currentAgent.name}</strong>
            <span>Permanently remove this Agent and stop its Runtime.</span>
          </div>
          <Button type="button" variant="danger" onClick={() => setDeleteOpen(true)}>
            <Trash2 size={14} />Delete agent
          </Button>
        </section>
      </div>

      <CreateAgentWizard open={createOpen} onClose={() => setCreateOpen(false)} />
      <ConfirmDialog
        open={deleteOpen}
        title={`Delete ${currentAgent.name}?`}
        description={
          <p>
            This permanently removes the Agent directory, including messages, memory, tasks, and workspace files.
            Type <strong>{currentAgent.name}</strong> to confirm.
          </p>
        }
        confirmLabel="Delete agent"
        confirmDisabled={deleteConfirm !== currentAgent.name}
        onConfirm={() => void removeAgent()}
        onCancel={() => {
          setDeleteOpen(false);
          setDeleteConfirm("");
        }}
      >
        <label className="wizard-field">
          <span>Agent name</span>
          <input value={deleteConfirm} autoComplete="off" onChange={(event) => setDeleteConfirm(event.target.value)} />
        </label>
      </ConfirmDialog>
    </PageShell>
  );
}
