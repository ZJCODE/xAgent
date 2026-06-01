import { CalendarClock, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createTask, deleteTask, getTasks } from "../lib/api";
import { classNames } from "../lib/format";
import type { ScheduledTaskItem, TasksResponse } from "../types";

interface TaskForm {
  message: string;
  delaySeconds: string;
  userId: string;
}

const defaultForm: TaskForm = {
  message: "",
  delaySeconds: "60",
  userId: "web_user",
};

function formatRunAt(value: string): string {
  if (!value) return "";
  const date = new Date(value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function taskTarget(task: ScheduledTaskItem): string {
  const target = task.payload.target || {};
  const channel = String(target.channel || "");
  const userId = String(target.user_id || task.payload.user_id || "");
  const chatId = String(target.chat_id || "");
  return [channel, userId || chatId].filter(Boolean).join(" · ") || "local";
}

function taskContent(task: ScheduledTaskItem): string {
  return String(task.payload.message || task.payload.command || "").trim();
}

export function TasksPage() {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState<TaskForm>(defaultForm);

  const tasks = useMemo(() => data?.tasks || [], [data]);
  const pendingCount = tasks.filter((task) => task.state === "pending").length;
  const failedCount = tasks.filter((task) => task.state === "failed").length;

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
  }, []);

  const submit = async () => {
    const message = form.message.trim();
    const delay = Number.parseInt(form.delaySeconds, 10);
    if (!message) {
      setError("Message is required");
      return;
    }
    if (!Number.isFinite(delay) || delay < 0) {
      setError("Delay must be zero or positive");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await createTask({
        message,
        delay_seconds: delay,
        user_id: form.userId.trim() || "web_user",
        title: "Reminder",
      });
      setForm((value) => ({ ...value, message: "" }));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const removeTask = async (task: ScheduledTaskItem) => {
    if (!window.confirm(`Delete task ${task.name}?`)) return;
    setError("");
    try {
      await deleteTask(task.name);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="console-page">
      <div className="console-toolbar">
        <div className="min-w-0">
          <h2>Tasks</h2>
          <p>{data?.root || "tasks"}</p>
        </div>
        <div className="console-toolbar-actions">
          <span className="data-chip">{pendingCount} pending</span>
          <span className="data-chip">{failedCount} failed</span>
          <button type="button" className="ghost-button icon-text-button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={15} />
            Refresh
          </button>
        </div>
      </div>

      <div className="tasks-layout">
        <section className="task-form-panel">
          <div className="content-heading">
            <div>
              <h3>New Reminder</h3>
              <span>web channel</span>
            </div>
          </div>
          <div className="skill-form">
            <label className="form-field">
              <span>Message</span>
              <textarea
                value={form.message}
                placeholder="走两步"
                onChange={(event) => setForm((value) => ({ ...value, message: event.target.value }))}
              />
            </label>
            <div className="task-form-grid">
              <label className="form-field">
                <span>Delay Seconds</span>
                <input
                  value={form.delaySeconds}
                  inputMode="numeric"
                  onChange={(event) => setForm((value) => ({ ...value, delaySeconds: event.target.value }))}
                />
              </label>
              <label className="form-field">
                <span>User</span>
                <input
                  value={form.userId}
                  onChange={(event) => setForm((value) => ({ ...value, userId: event.target.value }))}
                />
              </label>
            </div>
            <button type="button" className="ghost-button icon-text-button" disabled={saving} onClick={() => void submit()}>
              <Plus size={15} />
              Create
            </button>
          </div>
        </section>

        <section className="task-list-panel">
          {error && <div className="error-banner">{error}</div>}
          {loading ? (
            <div className="empty-state">Loading tasks...</div>
          ) : tasks.length ? (
            <div className="task-list">
              {tasks.map((task) => (
                <article key={`${task.state}-${task.name}`} className="task-row">
                  <div className="task-row-icon">
                    <CalendarClock size={18} />
                  </div>
                  <div className="task-row-main">
                    <div className="task-row-title">
                      <h3>{task.payload.title || (task.kind === "command" ? "Command" : "Reminder")}</h3>
                      <span className={classNames("task-state", task.state === "failed" && "failed")}>{task.state}</span>
                    </div>
                    <p>{taskContent(task) || task.name}</p>
                    <div className="chip-list">
                      <span className="data-chip">{formatRunAt(task.run_at)}</span>
                      <span className="data-chip">{taskTarget(task)}</span>
                      {task.reason && <span className="data-chip">{task.reason}</span>}
                    </div>
                  </div>
                  <button type="button" className="danger-button" onClick={() => void removeTask(task)} title="Delete task">
                    <Trash2 size={15} />
                    Delete
                  </button>
                </article>
              ))}
            </div>
          ) : (
            <div className="empty-state">No scheduled tasks</div>
          )}
        </section>
      </div>
    </div>
  );
}