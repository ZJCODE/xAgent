import { X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type {
  ChannelId,
  RuntimeTask,
  TaskCreateInput,
  TaskSchedule,
  TaskUpdateInput,
} from "../types";
import { Button, IconButton } from "./ui";
import { WizardField } from "./WizardField";

export type TaskEditorSave =
  | { mode: "create"; input: TaskCreateInput }
  | { mode: "edit"; taskId: string; patch: TaskUpdateInput };

interface Props {
  open: boolean;
  task: RuntimeTask | null;
  saving: boolean;
  error: string;
  onClose: () => void;
  onSave: (value: TaskEditorSave) => void;
}

type Kind = TaskSchedule["kind"];

function localDateTime(timestamp = Date.now() + 3_600_000): string {
  const date = new Date(timestamp - new Date().getTimezoneOffset() * 60_000);
  return date.toISOString().slice(0, 16);
}

export function TaskEditorModal({ open, task, saving, error, onClose, onSave }: Props) {
  const [content, setContent] = useState("");
  const [kind, setKind] = useState<Kind>("once");
  const [runAt, setRunAt] = useState(localDateTime);
  const [localTime, setLocalTime] = useState("09:00");
  const [weekday, setWeekday] = useState(0);
  const [intervalMinutes, setIntervalMinutes] = useState("5");
  const [durationHours, setDurationHours] = useState("4");
  const [endAt, setEndAt] = useState("");
  const [deliverResult, setDeliverResult] = useState(false);
  const [destinationChannel, setDestinationChannel] = useState<ChannelId>("feishu");
  const [destinationRecipient, setDestinationRecipient] = useState("");

  useEffect(() => {
    if (!open) return;
    setContent(task?.instruction || "");
    setKind(task?.schedule.kind || "once");
    setRunAt(
      task?.schedule.kind === "once"
        ? localDateTime(new Date(task.schedule.run_at).getTime())
        : localDateTime(),
    );
    setLocalTime(
      task?.schedule.kind === "daily" || task?.schedule.kind === "weekly"
        ? task.schedule.local_time
        : "09:00",
    );
    setWeekday(task?.schedule.kind === "weekly" ? task.schedule.weekday : 0);
    setIntervalMinutes(
      task?.schedule.kind === "interval"
        ? String(task.schedule.interval_seconds / 60)
        : "5",
    );
    setDurationHours(
      task?.schedule.kind === "interval" && task.schedule.duration_seconds
        ? String(task.schedule.duration_seconds / 3600)
        : "4",
    );
    setEndAt(
      task?.schedule.kind === "interval" && task.schedule.end_at
        ? localDateTime(new Date(task.schedule.end_at).getTime())
        : "",
    );
    setDeliverResult(Boolean(task?.destination));
    setDestinationChannel(task?.destination?.channel || "feishu");
    setDestinationRecipient(
      String(
        task?.destination?.target.chat_id
        || task?.destination?.target.user_id
        || "",
      ),
    );
  }, [open, task]);

  const validation = useMemo(() => {
    if (!content.trim()) return "Content is required.";
    if (deliverResult && destinationChannel !== "voice" && !destinationRecipient.trim()) {
      return destinationChannel === "feishu"
        ? "Feishu chat ID is required."
        : "Recipient user ID is required.";
    }
    if (kind === "once" && new Date(runAt).getTime() <= Date.now()) return "Run time must be in the future.";
    if (kind === "interval") {
      const interval = Number(intervalMinutes);
      if (!Number.isFinite(interval) || interval <= 0) return "Interval must be positive.";
      if (endAt && new Date(endAt).getTime() <= Date.now()) return "End time must be in the future.";
      if (!endAt && Number(durationHours) <= 0) return "Duration must be positive.";
    }
    return "";
  }, [
    content,
    deliverResult,
    destinationChannel,
    destinationRecipient,
    durationHours,
    endAt,
    intervalMinutes,
    kind,
    runAt,
  ]);

  if (!open) return null;

  const schedule = (): TaskSchedule => {
    if (kind === "once") return { kind, run_at: new Date(runAt).toISOString() };
    if (kind === "daily") return { kind, local_time: localTime };
    if (kind === "weekly") return { kind, weekday, local_time: localTime };
    return endAt
      ? { kind, interval_seconds: Number(intervalMinutes) * 60, end_at: new Date(endAt).toISOString() }
      : {
          kind,
          interval_seconds: Number(intervalMinutes) * 60,
          duration_seconds: Number(durationHours) * 3600,
        };
  };

  const save = () => {
    if (validation) return;
    const destination = deliverResult
      ? {
          channel: destinationChannel,
          target: destinationChannel === "voice"
            ? {}
            : destinationChannel === "feishu"
              ? { chat_id: destinationRecipient.trim() }
              : { user_id: destinationRecipient.trim() },
        }
      : null;
    if (task) {
      onSave({
        mode: "edit",
        taskId: task.task_id,
        patch: {
          instruction: content.trim(),
          schedule: schedule(),
          destination,
        },
      });
      return;
    }
    onSave({
      mode: "create",
      input: {
        instruction: content.trim(),
        schedule: schedule(),
        destination,
      },
    });
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={() => !saving && onClose()}>
      <div className="modal-card task-editor-modal" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h3>{task ? "Edit Agent task" : "Create Agent task"}</h3>
            <p className="task-editor-subtitle">Tell the Agent what to do and when. Results stay in its timeline and memory.</p>
          </div>
          <IconButton type="button" onClick={onClose} disabled={saving} aria-label="Close">
            <X size={16} />
          </IconButton>
        </div>
        <div className="modal-body">
          {error || validation ? <div className="error-banner">{error || validation}</div> : null}
          <div className="task-editor-grid task-editor-grid-single">
            <WizardField label="Schedule">
              <select value={kind} onChange={(event) => setKind(event.target.value as Kind)}>
                <option value="once">Once</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="interval">Bounded interval</option>
              </select>
            </WizardField>
          </div>
          <WizardField
            label="Instruction"
            hint="For example: Review today's conversations and summarize anything that needs attention."
          >
            <textarea
              rows={4}
              value={content}
              placeholder="What should the Agent do?"
              onChange={(event) => setContent(event.target.value)}
            />
          </WizardField>
          <div className="task-editor-grid">
            <WizardField
              label="Result"
              hint="Keeping the result stores it in Messages and memory without sending it."
            >
              <select
                value={deliverResult ? "send" : "keep"}
                onChange={(event) => setDeliverResult(event.target.value === "send")}
              >
                <option value="keep">Keep in Agent timeline</option>
                <option value="send">Send through a channel</option>
              </select>
            </WizardField>
            {deliverResult ? (
              <WizardField label="Channel">
                <select
                  value={destinationChannel}
                  onChange={(event) => setDestinationChannel(event.target.value as ChannelId)}
                >
                  <option value="api">API</option>
                  <option value="feishu">Feishu</option>
                  <option value="weixin">Weixin</option>
                  <option value="voice">Voice</option>
                </select>
              </WizardField>
            ) : null}
          </div>
          {deliverResult && destinationChannel !== "voice" ? (
            <WizardField
              label={destinationChannel === "feishu" ? "Feishu chat ID" : "Recipient user ID"}
              hint={
                destinationChannel === "feishu"
                  ? "The conversation ID that should receive the result."
                  : "The account ID subscribed to this channel."
              }
            >
              <input
                value={destinationRecipient}
                placeholder={destinationChannel === "feishu" ? "oc_…" : "user ID"}
                onChange={(event) => setDestinationRecipient(event.target.value)}
              />
            </WizardField>
          ) : null}
          {kind === "once" ? (
            <WizardField label="Run at"><input type="datetime-local" value={runAt} onChange={(event) => setRunAt(event.target.value)} /></WizardField>
          ) : null}
          {kind === "daily" || kind === "weekly" ? (
            <div className="task-editor-grid">
              {kind === "weekly" ? (
                <WizardField label="Weekday">
                  <select value={weekday} onChange={(event) => setWeekday(Number(event.target.value))}>
                    {["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((label, index) => (
                      <option key={label} value={index}>{label}</option>
                    ))}
                  </select>
                </WizardField>
              ) : null}
              <WizardField label="Local time"><input type="time" value={localTime} onChange={(event) => setLocalTime(event.target.value)} /></WizardField>
            </div>
          ) : null}
          {kind === "interval" ? (
            <div className="task-editor-grid">
              <WizardField label="Every minutes"><input type="number" min="1" value={intervalMinutes} onChange={(event) => setIntervalMinutes(event.target.value)} /></WizardField>
              <WizardField label="Duration hours" hint="Used when end time is empty."><input type="number" min="0.01" step="0.25" value={durationHours} onChange={(event) => setDurationHours(event.target.value)} /></WizardField>
              <WizardField label="Or end at"><input type="datetime-local" value={endAt} onChange={(event) => setEndAt(event.target.value)} /></WizardField>
            </div>
          ) : null}
        </div>
        <div className="modal-footer">
          <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button type="button" onClick={save} disabled={saving || Boolean(validation)}>{saving ? "Saving…" : "Save"}</Button>
        </div>
      </div>
    </div>
  );
}
