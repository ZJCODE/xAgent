import { CalendarClock, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { deleteTask, getTasks } from "../lib/api";
import { classNames } from "../lib/format";
import type { ScheduledTaskItem, TasksResponse } from "../types";

function formatRunAt(value: string): string {
  if (!value) return "";
  const date = new Date(value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function taskTarget(task: ScheduledTaskItem): string {
  const delivery = task.payload.delivery || {};
  const target = delivery.target || {};
  const channel = String(delivery.channel || "");
  const userId = String(delivery.user_id || target.user_id || "");
  const chatId = String(target.chat_id || "");
  return [channel, userId || chatId].filter(Boolean).join(" · ") || "local";
}

function taskContent(task: ScheduledTaskItem): string {
  return String(task.payload.task?.content || "").trim();
}

function taskTitle(task: ScheduledTaskItem): string {
  const type = String(task.payload.task?.type || "").trim();
  const title = String(task.payload.title || "").trim();
  if (title) return type ? `${title} · ${type}` : title;
  return type ? `Scheduled ${type}` : "Scheduled task";
}

export function TasksPage() {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

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
                      <h3>{taskTitle(task)}</h3>
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
