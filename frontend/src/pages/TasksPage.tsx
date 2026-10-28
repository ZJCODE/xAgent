import {
  CalendarClock,
  Copy,
  Eye,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { TaskEditorModal, type TaskEditorSave } from "../components/TaskEditorModal";
import { Button, EmptyState, IconButton, PageShell, PageToolbar, SearchField, StatusBadge } from "../components/ui";
import { createTask, deleteTask, duplicateTask, getTasks, pauseTask, resumeTask, updateTask, getTaskRuns, getTaskRun, taskRunAction } from "../lib/api";
import type { ScheduledTaskItem, TaskScope, TasksResponse, TaskRun } from "../types";

const PAGE_SIZE = 50;
type PageScope = Exclude<TaskScope, "current">;
type EditorMode = "create" | "edit" | "duplicate" | null;

function formatRunAt(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function taskTarget(task: ScheduledTaskItem): string {
  const target = task.target || {};
  const userId = String(task.user_id || target.user_id || "");
  const chatId = String(target.chat_id || "");
  return [...new Set([String(task.channel || ""), chatId, userId].filter(Boolean))].join(" · ") || "local";
}

function taskContent(task: ScheduledTaskItem): string {
  return String(task.content || "").trim();
}

function taskDisplayTitle(task: ScheduledTaskItem): string {
  const title = String(task.title || "").trim();
  if (title) return title;
  const content = taskContent(task);
  return content ? (content.length > 48 ? `${content.slice(0, 48)}…` : content) : "Scheduled task";
}

function taskTypeBadge(task: ScheduledTaskItem) {
  const type = task.task_type === "agent" ? "agent" : "message";
  return type === "agent" ? <StatusBadge className="task-type-agent">{type}</StatusBadge> : <StatusBadge tone="info">{type}</StatusBadge>;
}

function taskDisplayStatus(task: ScheduledTaskItem): string {
  if (task.state === "running") return "running";
  if (task.status === "needs_review") return "Needs checking";
  return task.status;
}

function taskStatusTone(status: string): "good" | "muted" | "danger" | "info" {
  if (status === "running") return "info";
  if (status === "paused" || status === "completed") return "muted";
  if (status === "failed" || status === "Needs checking") return "danger";
  return "good";
}

function formatSeconds(value: unknown): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function formatMonthlyOrdinal(nth: number): string {
  if (nth === -1) return "last";
  if (nth === 1) return "1st";
  if (nth === 2) return "2nd";
  if (nth === 3) return "3rd";
  return `${nth}th`;
}

function taskRecurrenceChips(task: ScheduledTaskItem): string[] {
  const chips: string[] = [];
  for (const rule of Array.isArray(task.recurrence) ? task.recurrence : []) {
    const kind = String(rule?.kind || "").trim();
    if (!kind) continue;
    chips.push(kind);
    if (kind === "interval") {
      const every = formatSeconds(rule?.every_seconds);
      if (every) chips.push(`every ${every}`);
      const endAt = formatRunAt(String(rule?.end_at || ""));
      if (endAt) chips.push(`until ${endAt}`);
    } else if (kind === "monthly") {
      if (rule?.day != null) {
        chips.push(Number(rule.day) === -1 ? "last day" : `day ${rule.day}`);
      } else if (rule?.weekday && rule?.nth != null) {
        chips.push(`${formatMonthlyOrdinal(Number(rule.nth))} ${rule.weekday}`);
      }
      if (rule?.time) chips.push(String(rule.time));
    } else {
      const weekdays = Array.isArray(rule?.weekdays) ? rule.weekdays.join(", ") : "";
      if (weekdays) chips.push(weekdays);
      if (rule?.time) chips.push(String(rule.time));
    }
  }
  return chips;
}

function lifecycleTime(task: ScheduledTaskItem): string {
  if (task.state === "running") return "Running now";
  if (task.blocked_on_review) return "Waiting for review before the next run";
  if (task.next_retry_at) return `Retry ${formatRunAt(task.next_retry_at)}`;
  if (task.status === "completed") return formatRunAt(task.completed_at || task.last_run_at);
  if (task.status === "failed" || task.status === "needs_review") return formatRunAt(task.failed_at || task.last_run_at || task.updated_at);
  return formatRunAt(task.next_run_at);
}

export function TasksPage() {
  const [scope, setScope] = useState<PageScope>("scheduled");
  const [data, setData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [editorMode, setEditorMode] = useState<EditorMode>(null);
  const [editingTask, setEditingTask] = useState<ScheduledTaskItem | null>(null);
  const [selectedTask, setSelectedTask] = useState<ScheduledTaskItem | null>(null);
  const [saving, setSaving] = useState(false);

  const tasks = useMemo(() => data?.tasks || [], [data]);

  const load = useCallback(
    async (append = false) => {
      if (!append) setLoading(true);
      setError("");
      try {
        const offset = append ? tasks.length : 0;
        const response = await getTasks(scope, appliedQuery, PAGE_SIZE, offset);
        setData((current) =>
          append && current
            ? { ...response, tasks: [...current.tasks, ...response.tasks], offset: 0 }
            : response,
        );
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!append) setLoading(false);
      }
    },
    [appliedQuery, scope, tasks.length],
  );

  useEffect(() => {
    void load(false);
  }, [scope, appliedQuery]);

  useEffect(() => {
    if (scope === "archive") return;
    const timer = window.setInterval(() => {
      void getTasks(scope, appliedQuery, PAGE_SIZE, 0).then((response) => { setData(response); setError(""); })
        .catch((err) => setError(`Could not refresh task status: ${err instanceof Error ? err.message : String(err)}`));
    }, 20000);
    return () => window.clearInterval(timer);
  }, [appliedQuery, scope]);

  const openEditor = (mode: Exclude<EditorMode, null>, task: ScheduledTaskItem | null = null) => {
    setError("");
    setEditorMode(mode);
    setEditingTask(task);
  };

  const closeEditor = () => {
    setEditorMode(null);
    setEditingTask(null);
  };

  const saveEditor = async (payload: TaskEditorSave) => {
    setSaving(true);
    setError("");
    try {
      if (payload.mode === "create") await createTask(payload.input);
      else if (payload.mode === "duplicate") await duplicateTask(payload.taskId, payload.input);
      else await updateTask(payload.taskId, payload.patch);
      closeEditor();
      setScope("scheduled");
      setAppliedQuery("");
      setQuery("");
      if (scope === "scheduled") await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const togglePause = async (task: ScheduledTaskItem) => {
    setError("");
    try {
      if (task.status === "paused") await resumeTask(task.task_id);
      else await pauseTask(task.task_id);
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const removeTask = async (task: ScheduledTaskItem) => {
    const archived = task.status === "completed";
    const message = archived
      ? `Permanently delete archived task “${taskDisplayTitle(task)}”? This history cannot be recovered.`
      : task.status === "failed"
        ? `Permanently delete failed task “${taskDisplayTitle(task)}”? This cannot be undone.`
        : `Permanently delete “${taskDisplayTitle(task)}” and cancel all future runs?`;
    if (!window.confirm(message)) return;
    setError("");
    try {
      await deleteTask(task.task_id);
      setSelectedTask(null);
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const renderActions = (task: ScheduledTaskItem) => {
    if (task.state === "running") {
      return (
        <div className="task-row-actions">
          <Button className="task-action-button" onClick={() => setSelectedTask(task)}><Eye size={15} />View</Button>
        </div>
      );
    }
    if (task.status === "completed") {
      return (
        <div className="task-row-actions">
          <Button className="task-action-button" onClick={() => setSelectedTask(task)}><Eye size={15} />View</Button>
          <Button className="task-action-button" onClick={() => openEditor("duplicate", task)}><Copy size={15} />Duplicate</Button>
          <IconButton variant="danger" onClick={() => void removeTask(task)} title="Delete permanently"><Trash2 size={15} /></IconButton>
        </div>
      );
    }
    if (task.status === "failed" || task.status === "needs_review") {
      return (
        <div className="task-row-actions">
          <Button className="task-action-button" onClick={() => setSelectedTask(task)}><Eye size={15} />View</Button>
          <IconButton variant="danger" onClick={() => void removeTask(task)} title="Delete permanently"><Trash2 size={15} /></IconButton>
        </div>
      );
    }
    const pauseLabel = task.status === "paused" ? "Resume" : "Pause";
    return (
      <div className="task-row-actions">
        <Button className="task-action-button" onClick={() => void togglePause(task)}>
          {task.status === "paused" ? <Play size={15} /> : <Pause size={15} />}{pauseLabel}
        </Button>
        <Button className="task-action-button" onClick={() => openEditor("edit", task)}><Pencil size={15} />Edit</Button>
        <IconButton variant="danger" onClick={() => void removeTask(task)} title="Delete task"><Trash2 size={15} /></IconButton>
      </div>
    );
  };

  return (
    <PageShell className="tasks-page">
      <PageToolbar
        title="Tasks"
        subtitle="The Agent must be running to execute tasks. Missed work is recovered within its recovery window."
        actions={
          <>
            <Button type="button" variant="primary" onClick={() => openEditor("create")}><Plus size={15} />Create task</Button>
            <SearchField placeholder={`Search ${scope}`} value={query} onChange={(event) => setQuery(event.target.value)} onSubmit={() => setAppliedQuery(query.trim())} />
            {query ? <IconButton type="button" onClick={() => { setQuery(""); setAppliedQuery(""); }} title="Clear search"><X size={16} /></IconButton> : null}
            <IconButton type="button" onClick={() => void load(false)} title="Refresh"><RefreshCw size={16} /></IconButton>
          </>
        }
      />

      <div className="tasks-layout">
        <div className="task-tab-bar" role="tablist" aria-label="Task lifecycle">
          {([
            ["scheduled", "Scheduled", data?.counts.scheduled],
            ["attention", "Needs attention", data?.counts.attention],
            ["archive", "Archive", data?.counts.archive],
          ] as Array<[PageScope, string, number | undefined]>).map(([value, label, count]) => (
            <button key={value} type="button" role="tab" aria-selected={scope === value} className={`task-tab ${scope === value ? "active" : ""}`} onClick={() => setScope(value)}>
              {label}<span>{count ?? 0}</span>
            </button>
          ))}
        </div>

        <section className="task-list-panel">
          {error && <div className="error-banner">{error}</div>}
          {loading ? <EmptyState title="Loading tasks..." /> : tasks.length ? (
            <div className="task-list">
              {tasks.map((task) => (
                <article key={task.task_id} className="task-row">
                  <div className="task-row-icon"><CalendarClock size={18} /></div>
                  <div className="task-row-main">
                    <div className="task-row-title">
                      <h3>{taskDisplayTitle(task)}</h3>
                      <div className="task-row-badges">{taskTypeBadge(task)}{task.last_run_status && <StatusBadge tone={task.last_run_status === "succeeded" ? "good" : "muted"}>Last run: {task.last_run_status}</StatusBadge>}<StatusBadge tone={taskStatusTone(taskDisplayStatus(task))}>{taskDisplayStatus(task)}</StatusBadge>{task.unresolved_runs?.length ? <StatusBadge tone="danger">{task.unresolved_runs.length} run(s) need checking</StatusBadge> : null}</div>
                    </div>
                    <p>{taskContent(task) || task.task_id}</p>
                    {task.last_error ? <p className="task-error-copy">{task.last_error}</p> : null}
                    {task.unresolved_runs?.length ? <p className="task-error-copy">A previous run has an uncertain outcome. It will not be retried automatically. Future runs follow this task's schedule. {task.last_review_error}</p> : null}
                    <div className="chip-list">
                      <span className="data-chip data-chip-wrap">{lifecycleTime(task) || "Unknown time"}</span>
                      <span className="data-chip data-chip-wrap">{taskTarget(task)}</span>
                      {task.completion_reason ? <span className="data-chip data-chip-wrap">{task.completion_reason}</span> : null}
                      {taskRecurrenceChips(task).map((label) => <span key={`${task.task_id}-${label}`} className="data-chip data-chip-wrap">{label}</span>)}
                    </div>
                  </div>
                  {renderActions(task)}
                </article>
              ))}
              {data?.has_more ? <Button type="button" onClick={() => void load(true)}>Load more</Button> : null}
            </div>
          ) : <EmptyState title={appliedQuery ? `No ${scope} tasks match “${appliedQuery}”` : scope === "scheduled" ? "No scheduled tasks" : scope === "attention" ? "No tasks need attention" : "No archived tasks"} />}
        </section>
      </div>

      <TaskEditorModal
        open={Boolean(editorMode)}
        mode={editorMode || "create"}
        task={editingTask}
        saving={saving}
        error={editorMode ? error : ""}
        onClose={closeEditor}
        onSave={(payload) => void saveEditor(payload)}
      />
      <TaskDetailsModal task={selectedTask} onClose={() => setSelectedTask(null)} onDuplicate={(task) => { setSelectedTask(null); openEditor("duplicate", task); }} onDelete={(task) => void removeTask(task)} onUpdated={() => { setSelectedTask(null); void load(false); }} />
    </PageShell>
  );
}

function TaskDetailsModal({
  task,
  onClose,
  onDuplicate,
  onDelete,
  onUpdated,
}: {
  task: ScheduledTaskItem | null;
  onClose: () => void;
  onDuplicate: (task: ScheduledTaskItem) => void;
  onDelete: (task: ScheduledTaskItem) => void;
  onUpdated: () => void;
}) {
  if (!task) return null;
  const rows = [
    ["Status", task.status],
    ["Type", task.task_type],
    ["Timezone", task.timezone || ""],
    ["Next run", formatRunAt(task.next_run_at)],
    ["Delivery", taskTarget(task)],
    ["Created", formatRunAt(task.created_at)],
    ["Updated", formatRunAt(task.updated_at)],
    [task.status === "completed" ? "Completed" : "Failed", lifecycleTime(task)],
    ["Last run", formatRunAt(task.last_run_at)],
    ["Scheduled for", formatRunAt(task.scheduled_run_at)],
    ["Failure stage", task.failure_stage || ""],
    ["Run ID", task.run_id || ""],
    ["Attempt ID", task.attempt_id || ""],
    ["Unresolved runs", task.unresolved_runs?.join(", ") || ""],
    ["Previous uncertain outcome", task.last_review_error || ""],
    ["Reason", task.completion_reason || task.last_error || task.reason || ""],
  ].filter(([, value]) => value);
  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <div className="modal-card task-details-modal" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header"><h3>{taskDisplayTitle(task)}</h3><IconButton onClick={onClose} aria-label="Close details"><X size={16} /></IconButton></div>
        <div className="modal-body">
          <p className="task-details-content">{taskContent(task)}</p>
          <TaskRunHistory taskId={task.task_id} onUpdated={onUpdated} />
          <dl className="task-details-grid">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
          {taskRecurrenceChips(task).length ? <div className="chip-list">{taskRecurrenceChips(task).map((label) => <span key={label} className="data-chip">{label}</span>)}</div> : null}
        </div>
        <div className="modal-footer"><Button variant="danger" onClick={() => onDelete(task)}><Trash2 size={15} />Delete permanently</Button>{task.status === "completed" ? <Button onClick={() => onDuplicate(task)}><Copy size={15} />Duplicate</Button> : null}</div>
      </div>
    </div>
  );
}

function TaskRunHistory({ taskId, onUpdated }: { taskId: string; onUpdated: () => void }) {
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [detail, setDetail] = useState<TaskRun | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setRuns([]); setDetail(null); setError("");
    void getTaskRuns(taskId).then((data) => { if (!cancelled) { setRuns(data.runs); setHasMore(data.has_more); } })
      .catch((err) => { if (!cancelled) setError(String(err.message || err)); });
    return () => { cancelled = true; };
  }, [taskId]);
  const inspect = async (run: TaskRun) => {
    setError("");
    try { setDetail((await getTaskRun(taskId, run.id)).run); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };
  const act = async (action: "prepare" | "deliver" | "resolve") => {
    if (!detail || busy) return;
    setBusy(true); setError("");
    try { await taskRunAction(taskId, detail.id, action); onUpdated(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  };
  return <section aria-label="Execution history">
    <h4>Execution history</h4>
    {error && <p className="error-banner">{error}</p>}
    {!runs.length && <p>No executions yet.</p>}
    {runs.map((run) => <div key={run.id} className="task-editor-notice">
      <Button type="button" variant="ghost" onClick={() => void inspect(run)}>{formatRunAt(run.due_at)} · {run.status}</Button>
      <p>{run.error}{run.skip_count ? ` (${run.skip_count} occurrences)` : ""}</p>
      {run.retry_at && <p>Retry at {formatRunAt(run.retry_at)}</p>}
    </div>)}
    {hasMore && <Button type="button" onClick={() => {
      void getTaskRuns(taskId, runs.length).then((data) => { setRuns((old) => [...old, ...data.runs]); setHasMore(data.has_more); })
        .catch((err) => setError(String(err.message || err)));
    }}>Load more executions</Button>}
    {detail && <div className="task-editor-section">
      <p>{formatRunAt(detail.due_at)} · {detail.status} · {detail.failure_stage || detail.phase}</p>
      {detail.error && <p>{detail.error_type}: {detail.error}</p>}
      {detail.resolved_at && <p>Reviewed at {formatRunAt(detail.resolved_at)}</p>}
      {detail.tools?.map((tool, index) => <p key={`${tool.name}-${index}`}>Started {tool.name} · {formatRunAt(tool.started_at)} · {tool.safety === "read_only" ? "read only" : tool.safety === "idempotent" ? "safe to repeat" : "may change external state"}</p>)}
      {detail.attempts?.map((attempt) => <p key={attempt.id}>Attempt {attempt.id.slice(0, 8)} · {formatRunAt(attempt.started_at)} · {attempt.status}{attempt.error ? ` · ${attempt.error}` : ""}</p>)}
      {detail.deliveries?.map((part) => <p key={part.id}>{part.description || part.id} · {part.status}{part.error ? ` · ${part.error}` : ""}</p>)}
      {detail.status === "needs_review" && <p>The earlier operation may have completed. This occurrence will not be resent automatically.</p>}
      <div className="toolbar-actions">
        {detail.can_retry_prepare && <Button disabled={busy} onClick={() => void act("prepare")}>Retry preparation</Button>}
        {detail.can_retry_deliver && <Button disabled={busy} onClick={() => void act("deliver")}>Retry unsent parts</Button>}
        {detail.can_resolve && <Button disabled={busy} onClick={() => void act("resolve")}>Mark reviewed without resending</Button>}
      </div>
    </div>}
  </section>;
}
