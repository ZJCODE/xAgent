import { Pause, Pencil, Play, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { TaskEditorModal, type TaskEditorSave } from "../components/TaskEditorModal";
import { Button, EmptyState, IconButton, PageShell, PageToolbar, StatusBadge } from "../components/ui";
import { createTask, deleteTask, getTasks, pauseTask, resumeTask, updateTask } from "../lib/api";
import type { RuntimeTask, TaskStatus } from "../types";

const statuses: Array<TaskStatus | "all"> = ["all", "active", "paused", "running", "completed", "failed"];

function formatTime(value: number | null): string {
  return value ? new Date(value * 1000).toLocaleString() : "No next run";
}

function scheduleLabel(task: RuntimeTask): string {
  const schedule = task.schedule;
  if (schedule.kind === "once") return `Once · ${new Date(schedule.run_at).toLocaleString()}`;
  if (schedule.kind === "daily") return `Daily · ${schedule.local_time}`;
  if (schedule.kind === "weekly") return `Weekly · day ${schedule.weekday + 1} · ${schedule.local_time}`;
  return `Every ${schedule.interval_seconds}s · bounded`;
}

export function TasksPage() {
  const [tasks, setTasks] = useState<RuntimeTask[]>([]);
  const [status, setStatus] = useState<TaskStatus | "all">("all");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [editorTask, setEditorTask] = useState<RuntimeTask | null | undefined>(undefined);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setTasks((await getTasks(status === "all" ? undefined : status)).tasks);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => { void load(); }, [load]);

  const act = async (operation: () => Promise<unknown>) => {
    setError("");
    try {
      await operation();
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const save = async (value: TaskEditorSave) => {
    setSaving(true);
    setError("");
    try {
      if (value.mode === "create") await createTask(value.input);
      else await updateTask(value.taskId, value.patch);
      setEditorTask(undefined);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };

  return (
    <PageShell>
      <PageToolbar
        eyebrow="Automation"
        title="Tasks"
        subtitle="Schedule Agent work, with optional explicit delivery of the result"
        actions={
          <>
            <select value={status} onChange={(event) => setStatus(event.target.value as TaskStatus | "all")}>
              {statuses.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <IconButton type="button" title="Refresh" aria-label="Refresh" onClick={() => void load()}>
              <RefreshCw size={15} />
            </IconButton>
            <Button type="button" variant="primary" onClick={() => setEditorTask(null)}><Plus size={15} />Create task</Button>
          </>
        }
      />
      <div className="page-body">
        {error ? <div className="error-strip">{error}</div> : null}
        {tasks.length ? (
          <div className="task-list">
            {tasks.map((task) => (
              <article className="task-row" key={task.task_id}>
              <div className="task-row-main">
                <div className="task-row-title">
                  <h3>{task.instruction.length > 70 ? `${task.instruction.slice(0, 70)}…` : task.instruction}</h3>
                  <div className="task-row-badges">
                    <StatusBadge tone={task.status === "failed" ? "danger" : task.status === "active" ? "good" : "muted"}>{task.status}</StatusBadge>
                    <StatusBadge tone="info">Agent action</StatusBadge>
                  </div>
                </div>
                <div className="chip-list">
                  <span className="data-chip">{scheduleLabel(task)}</span>
                  <span className="data-chip">{formatTime(task.next_run_at)}</span>
                  <span className="data-chip">Created in {task.created_source}</span>
                  <span className="data-chip">
                    {task.destination
                      ? `Send via ${task.destination.channel}`
                      : "Keep in timeline"}
                  </span>
                </div>
                {task.error ? <p className="task-error-copy">{task.error}</p> : null}
              </div>
              <div className="task-row-actions">
                {task.status === "active" ? (
                  <IconButton type="button" title="Pause" aria-label="Pause" onClick={() => void act(() => pauseTask(task.task_id))}><Pause size={15} /></IconButton>
                ) : null}
                {task.status === "paused" || task.status === "failed" ? (
                  <IconButton type="button" title="Resume" aria-label="Resume" onClick={() => void act(() => resumeTask(task.task_id))}><Play size={15} /></IconButton>
                ) : null}
                {task.status !== "running" ? (
                  <>
                    <IconButton type="button" title="Edit" aria-label="Edit" onClick={() => setEditorTask(task)}><Pencil size={15} /></IconButton>
                    <IconButton type="button" title="Delete" aria-label="Delete" variant="danger" onClick={() => void act(() => deleteTask(task.task_id))}><Trash2 size={15} /></IconButton>
                  </>
                ) : null}
              </div>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState title={loading ? "Loading tasks…" : "No tasks"}>
            Create an Agent action, then choose whether it runs once, daily, weekly, or for a bounded interval.
          </EmptyState>
        )}
      </div>
      <TaskEditorModal
        open={editorTask !== undefined}
        task={editorTask || null}
        saving={saving}
        error={error}
        onClose={() => setEditorTask(undefined)}
        onSave={(value) => void save(value)}
      />
    </PageShell>
  );
}
