import { CalendarClock, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button, EmptyState, PageShell, PageToolbar } from "../components/ui";
import { deleteTask, getTasks } from "../lib/api";
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
        const endAt = formatRunAt(String(rule?.end_at || ""));
        return [kind, every ? `every ${every}` : "", endAt ? `until ${endAt}` : ""].filter(Boolean).join(" · ");
      }
      const time = String(rule?.time || "").trim();
      const weekdays = Array.isArray(rule?.weekdays)
        ? rule.weekdays.map((item) => String(item || "").trim()).filter(Boolean).join(", ")
        : "";
      return [kind, weekdays, time].filter(Boolean).join(" · ");
    })
    .filter(Boolean);
}

export function TasksPage() {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

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
  }, []);

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
      <PageToolbar title="Tasks" subtitle={data?.root || "tasks"} />

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
                  <Button type="button" variant="danger" onClick={() => void removeTask(task)} title="Delete task">
                    <Trash2 size={15} />
                    Delete
                  </Button>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState title="No scheduled tasks" />
          )}
        </section>
      </div>
    </PageShell>
  );
}
