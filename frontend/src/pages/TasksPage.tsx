import { CalendarClock, Pause, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { TaskEditorModal, type TaskEditorSave } from "../components/TaskEditorModal";
import { Button, EmptyState, PageShell, PageToolbar } from "../components/ui";
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

function taskTitle(task: ScheduledTaskItem): string {
  const type = String(task.task_type || "").trim();
  const title = String(task.title || "").trim();
  if (title) return type ? `${title} · ${type}` : title;
  return type ? `Scheduled ${type}` : "Scheduled task";
}

function formatSeconds(value: unknown): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function taskRecurrenceLabels(task: ScheduledTaskItem): string[] {
  const recurrence = Array.isArray(task.recurrence) ? task.recurrence : [];
  return recurrence
    .map((rule) => {
      const kind = String(rule?.kind || "").trim();
      if (kind === "interval") {
        const every = formatSeconds(rule?.every_seconds);
        const startAt = formatRunAt(String(rule?.start_at || ""));
        const endAt = formatRunAt(String(rule?.end_at || ""));
        const windowLabel = startAt && endAt ? `${startAt} - ${endAt}` : endAt ? `until ${endAt}` : "";
        return [kind, every ? `every ${every}` : "", windowLabel].filter(Boolean).join(" · ");
      }
      const time = String(rule?.time || "").trim();
      const weekdays = Array.isArray(rule?.weekdays)
        ? rule.weekdays.map((item) => String(item || "").trim()).filter(Boolean).join(", ")
        : "";
      return [kind, weekdays, time].filter(Boolean).join(" · ");
    })
    .filter(Boolean);
}

type EditorMode = "create" | "edit" | null;

export function TasksPage() {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editorMode, setEditorMode] = useState<EditorMode>(null);
  const [editingTask, setEditingTask] = useState<ScheduledTaskItem | null>(null);
  const [saving, setSaving] = useState(false);

  const tasks = useMemo(() => data?.tasks || [], [data]);

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

  return (
    <PageShell>
      <PageToolbar
        title="Tasks"
        subtitle={data?.root || "tasks"}
        actions={
          <Button type="button" onClick={openCreate}>
            <Plus size={15} />
            Create
          </Button>
        }
      />

      <div className="tasks-layout">
        <section className="task-list-panel">
          {error && <div className="error-banner">{error}</div>}
          {loading ? (
            <EmptyState title="Loading tasks..." />
          ) : tasks.length ? (
            <div className="task-list">
              {tasks.map((task) => (
                <article key={task.task_id} className="task-row">
                  <div className="task-row-icon">
                    <CalendarClock size={18} />
                  </div>
                  <div className="task-row-main">
                    <div className="task-row-title">
                      <h3>{taskTitle(task)}</h3>
                      <span className="task-state">{task.status}</span>
                    </div>
                    <p>{taskContent(task) || task.task_id}</p>
                    <div className="chip-list">
                      <span className="data-chip">{formatRunAt(task.next_run_at)}</span>
                      <span className="data-chip">{taskTarget(task)}</span>
                      {taskRecurrenceLabels(task).map((label) => (
                        <span key={`${task.task_id}-${label}`} className="data-chip">{label}</span>
                      ))}
                    </div>
                  </div>
                  <div className="task-row-actions">
                    {task.status !== "failed" && (
                      <Button type="button" onClick={() => void togglePause(task)} title={task.status === "paused" ? "Resume" : "Pause"}>
                        {task.status === "paused" ? <Play size={15} /> : <Pause size={15} />}
                        {task.status === "paused" ? "Resume" : "Pause"}
                      </Button>
                    )}
                    {task.status !== "failed" && (
                      <Button type="button" onClick={() => openEdit(task)} title="Edit task">
                        <Pencil size={15} />
                        Edit
                      </Button>
                    )}
                    <Button type="button" variant="danger" onClick={() => void removeTask(task)} title="Delete task">
                      <Trash2 size={15} />
                      Delete
                    </Button>
                  </div>
                </article>
              ))}
            </div>
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
