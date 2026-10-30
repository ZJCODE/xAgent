import { FileText, Play, RefreshCw, Square, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { getRuntime, getRuntimeLogs, restartRuntime, startRuntime, stopRuntime } from "../lib/api";
import type { AgentRuntimeStatus } from "../types";
import { Button, IconButton, Panel, StatusBadge } from "./ui";

type RuntimeAction = "start" | "stop" | "restart";

// Key this component by agent so pending requests and logs stay with their owner.
export function AgentRuntimeOverview({ restartRequired, settingsNotice, onSettingsApplied, children }: {
  restartRequired: boolean;
  settingsNotice?: string;
  children?: ReactNode;
  onSettingsApplied: () => void;
}) {
  const { selectedAgent, refresh: refreshAgents } = useAgentSession();
  const [runtime, setRuntime] = useState<AgentRuntimeStatus | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<RuntimeAction | null>(null);
  const [logs, setLogs] = useState<string | null>(null);
  const [logsBusy, setLogsBusy] = useState(false);
  const mounted = useRef(false);
  const loadSequence = useRef(0);
  const actionPending = useRef(false);
  const logsSequence = useRef(0);
  const logsLoading = useRef(false);

  const load = useCallback(async (silent = false) => {
    const sequence = ++loadSequence.current;
    try {
      const result = await getRuntime();
      if (mounted.current && sequence === loadSequence.current) {
        setRuntime(result);
        setError("");
      }
    } catch (err) {
      if (!silent && mounted.current && sequence === loadSequence.current) setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const interval = window.setInterval(() => { if (!actionPending.current) void load(); }, 5000);
    return () => { mounted.current = false; loadSequence.current += 1; logsSequence.current += 1; window.clearInterval(interval); };
  }, [load]);

  useEffect(() => { if (restartRequired) void load(); }, [restartRequired, load]);

  const runAction = async (action: RuntimeAction) => {
    if (actionPending.current) return;
    actionPending.current = true;
    loadSequence.current += 1;
    setPending(action); setError(""); setNotice("");
    try {
      const result = await (action === "start" ? startRuntime() : action === "stop" ? stopRuntime() : restartRuntime());
      if (!mounted.current) return;
      setRuntime(result);
      if (action === "restart" || action === "start") onSettingsApplied();
      setNotice([action === "stop" ? "Agent stopped." : action === "restart" ? "Agent restarted." : "Agent started.", ...(result.restart_warnings || [])].join(" "));
      await load(true);
      if (mounted.current) await refreshAgents();
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      actionPending.current = false;
      if (mounted.current) setPending(null);
    }
  };

  const loadLogs = useCallback(async () => {
    if (logsLoading.current) return;
    logsLoading.current = true;
    const sequence = ++logsSequence.current;
    setLogsBusy(true);
    try {
      const result = await getRuntimeLogs();
      if (mounted.current && sequence === logsSequence.current) setLogs(result.text);
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      logsLoading.current = false;
      if (mounted.current) setLogsBusy(false);
    }
  }, []);
  const logsOpen = logs !== null;
  useEffect(() => {
    if (!logsOpen) return;
    const interval = window.setInterval(() => void loadLogs(), 3000);
    return () => window.clearInterval(interval);
  }, [logsOpen, loadLogs]);

  const running = Boolean(runtime?.runtime_running);
  const state = runtime?.status;
  const busy = Boolean(pending) || state === "starting" || state === "stopping";
  const ready = running && runtime?.runtime_ready;
  const review = runtime?.tasks?.needs_review;
  const reviewCount = Array.isArray(review) ? review.length : review || 0;
  const status = !runtime ? error ? "Unavailable" : "Loading…" : state === "degraded" ? "Needs attention" : state === "starting" ? "Starting"
    : state === "stopping" ? "Stopping" : ready ? "Running" : running ? "Preparing" : "Offline";

  return (
    <Panel className="runtime-overview">
      <div className="runtime-heading">
        <div className="runtime-heading-copy"><h3>{selectedAgent}</h3></div>
        <StatusBadge tone={state === "degraded" ? "info" : ready ? "good" : "muted"} role="status"><span className="runtime-status-dot" aria-hidden="true" />{status}</StatusBadge>
      </div>
      <div className="runtime-controls">
        {running ? <Button type="button" disabled={busy} onClick={() => void runAction("stop")}><Square size={13} />{pending === "stop" ? "Stopping…" : "Stop Agent"}</Button>
          : <Button type="button" variant="primary" disabled={!runtime || busy} onClick={() => void runAction("start")}><Play size={13} />{pending === "start" ? "Starting…" : "Start Agent"}</Button>}
        {running ? <Button type="button" variant="ghost" disabled={busy} onClick={() => void runAction("restart")}><RefreshCw size={14} />{pending === "restart" ? "Restarting…" : "Restart"}</Button> : null}
        <Button type="button" variant="ghost" disabled={logsBusy || logsOpen} onClick={() => void loadLogs()}><FileText size={14} />{logsBusy ? "Loading logs…" : "View logs"}</Button>
      </div>
      {error ? <div className="error-strip" role="alert">{error}<Button type="button" variant="ghost" onClick={() => void load()}>Retry status</Button></div> : null}
      {notice || settingsNotice ? <div className="success-strip" role="status">{[notice, settingsNotice].filter(Boolean).join(" ")}</div> : null}
      {runtime?.needs_restart || restartRequired ? <div className="warning-strip">{running ? "Settings saved. Restart the Agent to apply them." : "Settings saved. They will apply when the Agent starts."}</div> : null}
      {runtime?.error ? <p className="task-error-copy" role="alert">{runtime.error}</p> : null}
      {reviewCount ? <div className="warning-strip"><a href="/tasks">{reviewCount} task{reviewCount === 1 ? "" : "s"} need checking.</a> Review the result before retrying.</div> : null}
      {logsOpen ? <div className="channel-log-panel">
        <div className="channel-log-header"><span>Agent logs · Auto refresh</span><IconButton title="Close logs" onClick={() => { logsSequence.current += 1; setLogs(null); }}><X size={15} /></IconButton></div>
        <pre className="channel-log-output">{logs?.trim() || "No log output yet."}</pre>
      </div> : null}
      {children}
    </Panel>
  );
}
