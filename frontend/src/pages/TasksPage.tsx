import { CalendarClock, Pause, Pencil, Play, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { TaskEditorModal, type TaskEditorSave } from "../components/TaskEditorModal";
import { Button, EmptyState, IconButton, PageShell, PageToolbar, SearchField, StatusBadge } from "../components/ui";
import { createTask, deleteTask, getTasks, pauseTask, resumeTask, updateTask } from "../lib/api";
import type { ScheduledTaskItem, TasksResponse } from "../types";

function formatRunAt(value: string): string {
  if (!value) return "";
  const date = new Date(value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function taskTarget(task: ScheduledTaskItem): string {
  const target = task.target || {};
  const channel = String(task.channel || "");
  const userId = String(task.user_id || target.user_id || "");
  const chatId = String(target.chat_id || "");
  return [channel, userId || chatId].filter(Boolean).join(" · ") || "local";
}

function taskContent(task: ScheduledTaskItem): string {
  return String(task.content || "").trim();
}

function taskDisplayTitle(task: ScheduledTaskItem): string {
  const title = String(task.title || "").trim();
  if (title) return title;
  const content = taskContent(task);
  if (content) return content.length > 48 ? `${content.slice(0, 48)}…` : content;
  return "Scheduled task";
}

function taskTypeBadge(task: ScheduledTaskItem) {
  const type = task.task_type === "agent" ? "agent" : "message";
  if (type === "agent") {
    return <StatusBadge className="task-type-agent">{type}</StatusBadge>;
  }
  return <StatusBadge tone="info">{type}</StatusBadge>;
}

function taskStatusTone(status: string): "good" | "muted" | "danger" {
  if (status === "paused") return "muted";
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
  const recurrence = Array.isArray(task.recurrence) ? task.recurrence : [];
  const chips: string[] = [];

  for (const rule of recurrence) {
    const kind = String(rule?.kind || "").trim();
    if (!kind) continue;

    if (kind === "interval") {
      chips.push(kind);
      const every = formatSeconds(rule?.every_seconds);
      if (every) chips.push(`every ${every}`);
      const startAt = formatRunAt(String(rule?.start_at || ""));
      const endAt = formatRunAt(String(rule?.end_at || ""));
      if (startAt && endAt) chips.push(`${startAt} - ${endAt}`);
      else if (endAt) chips.push(`until ${endAt}`);
      continue;
    }

    chips.push(kind);
    const weekdays = Array.isArray(rule?.weekdays)
      ? rule.weekdays.map((item) => String(item || "").trim()).filter(Boolean).join(", ")
      : "";
    if (weekdays) chips.push(weekdays);
    const time = String(rule?.time || "").trim();
    if (time) chips.push(time);
  }

  return chips;
}

function taskSearchText(task: ScheduledTaskItem): string {
  const target = task.target || {};
  return [
    task.task_id,
    task.title,
    task.content,
    task.status,
    task.task_type,
    task.channel,
    task.user_id,
    String(target.user_id || ""),
    String(target.chat_id || ""),
    formatRunAt(task.next_run_at),
    taskTarget(task),
    ...taskRecurrenceChips(task),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function filterTasks(tasks: ScheduledTaskItem[], query: string): ScheduledTaskItem[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return tasks;
  return tasks.filter((task) => taskSearchText(task).includes(needle));
}

type EditorMode = "create" | "edit" | null;

export function TasksPage() {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editorMode, setEditorMode] = useState<EditorMode>(null);
  const [editingTask, setEditingTask] = useState<ScheduledTaskItem | null>(null);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");

  const tasks = useMemo(() => data?.tasks || [], [data]);
  const visibleTasks = useMemo(
    () => (appliedQuery ? filterTasks(tasks, appliedQuery) : tasks),
    [tasks, appliedQuery],
  );

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      setData(await getTasks());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      void getTasks()
        .then(setData)
        .catch(() => undefined);
    }, 20000);
    return () => window.clearInterval(timer);
  }, []);

  const runSearch = () => {
    setAppliedQuery(query.trim());
  };

  const clearSearch = () => {
    setQuery("");
    setAppliedQuery("");
  };

  const openCreate = () => {
    setError("");
    setEditorMode("create");
    setEditingTask(null);
  };

  const openEdit = (task: ScheduledTaskItem) => {
    setError("");
    setEditorMode("edit");
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
      else await updateTask(payload.taskId, payload.patch);
      closeEditor();
      await load();
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
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const removeTask = async (task: ScheduledTaskItem) => {
    if (!window.confirm(`Delete task ${task.task_id}?`)) return;
    setError("");
    try {
      await deleteTask(task.task_id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const renderTaskActions = (task: ScheduledTaskItem) => {
    const pauseLabel = task.status === "paused" ? "Resume" : "Pause";
    return (
      <div className="task-row-actions">
        {task.status !== "failed" && (
          <Button
            type="button"
            className="task-action-button"
            onClick={() => void togglePause(task)}
            title={pauseLabel}
            aria-label={pauseLabel}
          >
            {task.status === "paused" ? <Play size={15} /> : <Pause size={15} />}
            {pauseLabel}
          </Button>
        )}
        {task.status !== "failed" && (
          <Button
            type="button"
            className="task-action-button"
            onClick={() => openEdit(task)}
            title="Edit task"
            aria-label="Edit task"
          >
            <Pencil size={15} />
            Edit
          </Button>
        )}
        <IconButton
          type="button"
          variant="danger"
          onClick={() => void removeTask(task)}
          title="Delete task"
          aria-label={`Delete task ${task.task_id}`}
        >
          <Trash2 size={15} />
        </IconButton>
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
            <Button type="button" onClick={openCreate}>
              <Plus size={15} />
              Create
            </Button>
            <SearchField
              placeholder="Search tasks"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onSubmit={runSearch}
            />
            <Button type="button" onClick={runSearch}>
              <Search size={15} />
              Search
            </Button>
            <IconButton type="button" onClick={clearSearch} title="Clear search">
              <X size={16} />
            </IconButton>
            <IconButton type="button" onClick={() => void load()} title="Refresh">
              <RefreshCw size={16} />
            </IconButton>
          </>
        }
      />

      <div className="tasks-layout">
        <section className="task-list-panel">
          {error && <div className="error-banner">{error}</div>}
          {loading ? (
            <EmptyState title="Loading tasks..." />
          ) : visibleTasks.length ? (
            <div className="task-list">
              {visibleTasks.map((task) => (
                <article key={task.task_id} className="task-row">
                  <div className="task-row-icon">
                    <CalendarClock size={18} />
                  </div>
                  <div className="task-row-main">
                    <div className="task-row-title">
                      <h3>{taskDisplayTitle(task)}</h3>
                      <div className="task-row-badges">
                        {taskTypeBadge(task)}
                        <StatusBadge tone={taskStatusTone(task.status)}>{task.status}</StatusBadge>
                      </div>
                    </div>
                    <p>{taskContent(task) || task.task_id}</p>
                    <div className="chip-list">
                      <span className="data-chip data-chip-wrap">{formatRunAt(task.next_run_at)}</span>
                      <span className="data-chip data-chip-wrap">{taskTarget(task)}</span>
                      {taskRecurrenceChips(task).map((label) => (
                        <span key={`${task.task_id}-${label}`} className="data-chip data-chip-wrap">
                          {label}
                        </span>
                      ))}
                    </div>
                  </div>
                  {renderTaskActions(task)}
                </article>
              ))}
            </div>
          ) : appliedQuery ? (
            <EmptyState title={`No tasks match "${appliedQuery}"`} />
          ) : (
            <EmptyState title="No scheduled tasks" />
          )}
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
    </PageShell>
  );
}
