import {
  CalendarClock,
  Copy,
  Eye,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { TaskEditorModal, type TaskEditorSave } from "../components/TaskEditorModal";
import { Button, EmptyState, IconButton, PageShell, PageToolbar, SearchField, StatusBadge } from "../components/ui";
import { createTask, deleteTask, duplicateTask, getTasks, pauseTask, resumeTask, updateTask } from "../lib/api";
import type { ScheduledTaskItem, TaskScope, TasksResponse } from "../types";

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
  return task.status;
}

function taskStatusTone(status: string): "good" | "muted" | "danger" | "info" {
  if (status === "running") return "info";
  if (status === "paused" || status === "completed") return "muted";
  if (status === "failed") return "danger";
  return "good";
}

function formatSeconds(value: unknown): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
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
  if (task.status === "completed") return formatRunAt(task.completed_at || task.last_run_at);
  if (task.status === "failed") return formatRunAt(task.failed_at || task.last_run_at);
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
      void getTasks(scope, appliedQuery, PAGE_SIZE, 0).then(setData).catch(() => undefined);
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
    if (task.status === "failed") {
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
    <PageShell>
      <PageToolbar
        title="Tasks"
        subtitle={data?.root || "tasks"}
        actions={
          <>
            <Button type="button" onClick={() => openEditor("create")}><Plus size={15} />Create</Button>
            <SearchField placeholder={`Search ${scope}`} value={query} onChange={(event) => setQuery(event.target.value)} onSubmit={() => setAppliedQuery(query.trim())} />
            <Button type="button" onClick={() => setAppliedQuery(query.trim())}><Search size={15} />Search</Button>
            <IconButton type="button" onClick={() => { setQuery(""); setAppliedQuery(""); }} title="Clear search"><X size={16} /></IconButton>
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
            <button key={value} type="button" className={`task-tab ${scope === value ? "active" : ""}`} onClick={() => setScope(value)}>
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
                      <div className="task-row-badges">{taskTypeBadge(task)}<StatusBadge tone={taskStatusTone(taskDisplayStatus(task))}>{taskDisplayStatus(task)}</StatusBadge></div>
                    </div>
                    <p>{taskContent(task) || task.task_id}</p>
                    {task.last_error ? <p className="task-error-copy">{task.last_error}</p> : null}
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
      <TaskDetailsModal task={selectedTask} onClose={() => setSelectedTask(null)} onDuplicate={(task) => { setSelectedTask(null); openEditor("duplicate", task); }} onDelete={(task) => void removeTask(task)} />
    </PageShell>
  );
}

function TaskDetailsModal({
  task,
  onClose,
  onDuplicate,
  onDelete,
}: {
  task: ScheduledTaskItem | null;
  onClose: () => void;
  onDuplicate: (task: ScheduledTaskItem) => void;
  onDelete: (task: ScheduledTaskItem) => void;
}) {
  if (!task) return null;
  const rows = [
    ["Status", task.status],
    ["Type", task.task_type],
    ["Delivery", taskTarget(task)],
    ["Created", formatRunAt(task.created_at)],
    ["Updated", formatRunAt(task.updated_at)],
    [task.status === "completed" ? "Completed" : "Failed", lifecycleTime(task)],
    ["Last run", formatRunAt(task.last_run_at)],
    ["Reason", task.completion_reason || task.last_error || task.reason || ""],
  ].filter(([, value]) => value);
  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <div className="modal-card task-details-modal" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header"><h3>{taskDisplayTitle(task)}</h3><IconButton onClick={onClose} aria-label="Close details"><X size={16} /></IconButton></div>
        <div className="modal-body">
          <p className="task-details-content">{taskContent(task)}</p>
          <dl className="task-details-grid">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
          {taskRecurrenceChips(task).length ? <div className="chip-list">{taskRecurrenceChips(task).map((label) => <span key={label} className="data-chip">{label}</span>)}</div> : null}
        </div>
        <div className="modal-footer"><Button variant="danger" onClick={() => onDelete(task)}><Trash2 size={15} />Delete permanently</Button>{task.status === "completed" ? <Button onClick={() => onDuplicate(task)}><Copy size={15} />Duplicate</Button> : null}</div>
      </div>
    </div>
  );
}
