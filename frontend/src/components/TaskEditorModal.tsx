import { X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { classNames } from "../lib/format";
import {
  createDefaultTaskFormState,
  formStateToCreateInput,
  formStateToUpdateInput,
  taskToFormState,
  validateTaskForm,
  WEEKDAY_OPTIONS,
  type IntervalEndMode,
  type IntervalFirstRunMode,
  type IntervalStartMode,
  type OneShotMode,
  type TaskFormState,
  type TaskScheduleKind,
  type WeekdayOption,
} from "../lib/taskFormUtils";
import type { ScheduledTaskItem, TaskCreateInput, TaskUpdateInput } from "../types";
import { Button, IconButton } from "./ui";
import { WizardField } from "./WizardField";

export type TaskEditorSave =
  | { mode: "create"; input: TaskCreateInput }
  | { mode: "edit"; taskId: string; patch: TaskUpdateInput };

interface TaskEditorModalProps {
  open: boolean;
  mode: "create" | "edit";
  task: ScheduledTaskItem | null;
  saving?: boolean;
  error?: string;
  onClose: () => void;
  onSave: (payload: TaskEditorSave) => void;
}

export function TaskEditorModal({
  open,
  mode,
  task,
  saving = false,
  error = "",
  onClose,
  onSave,
}: TaskEditorModalProps) {
  const [form, setForm] = useState<TaskFormState>(() => createDefaultTaskFormState());
  const [localError, setLocalError] = useState("");
  const validationError = useMemo(() => validateTaskForm(form), [form]);
  const isEdit = mode === "edit";
  const isNonApiTask = isEdit && task && String(task.channel || "api").toLowerCase() !== "api";

  useEffect(() => {
    if (!open) return;
    setLocalError("");
    setForm(task ? taskToFormState(task) : createDefaultTaskFormState());
  }, [open, task]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, open, saving]);

  if (!open) return null;

  const updateForm = (patch: Partial<TaskFormState>) => {
    setLocalError("");
    setForm((current) => ({ ...current, ...patch }));
  };

  const save = () => {
    const message = validateTaskForm(form);
    if (message) {
      setLocalError(message);
      return;
    }
    if (isEdit) {
      if (!task) return;
      onSave({ mode: "edit", taskId: task.task_id, patch: formStateToUpdateInput(form, task) });
      return;
    }
    onSave({ mode: "create", input: formStateToCreateInput(form) });
  };

  const toggleWeekday = (weekday: WeekdayOption) => {
    const exists = form.weekdays.includes(weekday);
    updateForm({
      weekdays: exists ? form.weekdays.filter((item) => item !== weekday) : [...form.weekdays, weekday],
    });
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={() => !saving && onClose()}>
      <div
        className="modal-card task-editor-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-editor-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <div>
            <h3 id="task-editor-title">{isEdit ? "Edit task" : "Create task"}</h3>
            <p className="task-editor-subtitle">API channel is used for new web tasks. Chat-created delivery targets stay unchanged on edit.</p>
          </div>
          <IconButton type="button" onClick={onClose} disabled={saving} aria-label="Close task editor">
            <X size={16} />
          </IconButton>
        </div>

        <div className="modal-body">
          {(localError || error) && <div className="error-banner">{localError || error}</div>}
          {isNonApiTask ? (
            <div className="task-editor-notice">
              Editing delivery channel <span className="data-chip">{task?.channel}</span>. The channel and target will not be changed.
            </div>
          ) : null}

          <form
            className="task-editor-form"
            onSubmit={(event) => {
              event.preventDefault();
              save();
            }}
          >
            <div className="task-editor-grid">
              <WizardField label="Title" hint="Optional. Empty titles are saved as Reminder.">
                <input value={form.title} onChange={(event) => updateForm({ title: event.target.value })} />
              </WizardField>

              <WizardField label="Type">
                <select value={form.taskType} onChange={(event) => updateForm({ taskType: event.target.value as "message" | "agent" })}>
                  <option value="message">message</option>
                  <option value="agent">agent</option>
                </select>
              </WizardField>
            </div>

            <WizardField label="Content">
              <textarea value={form.content} onChange={(event) => updateForm({ content: event.target.value })} rows={4} />
            </WizardField>

            <WizardField label="Schedule">
              <select value={form.scheduleKind} onChange={(event) => updateForm({ scheduleKind: event.target.value as TaskScheduleKind })}>
                <option value="oneshot">One-shot</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="interval">Interval</option>
              </select>
            </WizardField>

            {form.scheduleKind === "oneshot" && <OneShotFields form={form} updateForm={updateForm} />}
            {form.scheduleKind === "daily" && (
              <WizardField label="Daily time" hint="Local wall-clock time.">
                <input type="time" value={form.dailyTime} onChange={(event) => updateForm({ dailyTime: event.target.value })} />
              </WizardField>
            )}
            {form.scheduleKind === "weekly" && (
              <WeeklyFields form={form} updateForm={updateForm} toggleWeekday={toggleWeekday} />
            )}
            {form.scheduleKind === "interval" && <IntervalFields form={form} updateForm={updateForm} />}
          </form>
        </div>

        <div className="modal-footer">
          <span className="task-editor-save-hint">{validationError ? validationError : "Ready to save."}</span>
          <div className="modal-footer-actions">
            <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>
              Cancel
            </Button>
            <Button type="button" onClick={save} disabled={saving || Boolean(validationError)}>
              {saving ? "Saving..." : "Save"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

function OneShotFields({
  form,
  updateForm,
}: {
  form: TaskFormState;
  updateForm: (patch: Partial<TaskFormState>) => void;
}) {
  return (
    <div className="task-editor-section">
      <RadioGroup
        name="oneshot-mode"
        value={form.oneShotMode}
        options={[
          { value: "delay", label: "Run after a delay" },
          { value: "absolute", label: "Run at a specific time" },
        ]}
        onChange={(value) => updateForm({ oneShotMode: value as OneShotMode })}
      />
      {form.oneShotMode === "delay" ? (
        <WizardField label="Delay (minutes)">
          <input type="number" min="0" step="1" value={form.delayMinutes} onChange={(event) => updateForm({ delayMinutes: event.target.value })} />
        </WizardField>
      ) : (
        <WizardField label="Run at">
          <input type="datetime-local" value={form.runAt} onChange={(event) => updateForm({ runAt: event.target.value })} />
        </WizardField>
      )}
    </div>
  );
}

function WeeklyFields({
  form,
  updateForm,
  toggleWeekday,
}: {
  form: TaskFormState;
  updateForm: (patch: Partial<TaskFormState>) => void;
  toggleWeekday: (weekday: WeekdayOption) => void;
}) {
  return (
    <div className="task-editor-section">
      <WizardField label="Weekly time" hint="Local wall-clock time.">
        <input type="time" value={form.weeklyTime} onChange={(event) => updateForm({ weeklyTime: event.target.value })} />
      </WizardField>
      <div className="task-editor-field-block">
        <span>Weekdays</span>
        <div className="weekday-chip-list">
          {WEEKDAY_OPTIONS.map((weekday) => (
            <button
              key={weekday}
              type="button"
              className={classNames("weekday-chip", form.weekdays.includes(weekday) && "selected")}
              onClick={() => toggleWeekday(weekday)}
            >
              {weekday}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function IntervalFields({
  form,
  updateForm,
}: {
  form: TaskFormState;
  updateForm: (patch: Partial<TaskFormState>) => void;
}) {
  return (
    <div className="task-editor-section">
      <RadioGroup
        name="interval-start-mode"
        value={form.intervalStartMode}
        options={[
          { value: "now", label: "Start from now" },
          { value: "at", label: "Start at a specific time" },
        ]}
        onChange={(value) => updateForm({ intervalStartMode: value as IntervalStartMode })}
      />
      {form.intervalStartMode === "at" ? (
        <WizardField label="Start at" hint="First reminder fires at this time, then repeats on the grid.">
          <input
            type="datetime-local"
            value={form.intervalStartAt}
            onChange={(event) => updateForm({ intervalStartAt: event.target.value })}
          />
        </WizardField>
      ) : null}

      <WizardField label="Every (minutes)" hint="Minimum 1 minute.">
        <input
          type="number"
          min="1"
          step="1"
          value={form.intervalMinutes}
          onChange={(event) => updateForm({ intervalMinutes: event.target.value })}
        />
      </WizardField>

      <RadioGroup
        name="interval-end-mode"
        value={form.intervalEndMode}
        options={[
          { value: "duration", label: "Run for a duration" },
          { value: "end_at", label: "Stop at a specific time" },
        ]}
        onChange={(value) => updateForm({ intervalEndMode: value as IntervalEndMode })}
      />
      {form.intervalEndMode === "duration" ? (
        <WizardField label="Duration (minutes)" hint="Required; max 30 days.">
          <input
            type="number"
            min="1"
            step="1"
            value={form.intervalDurationMinutes}
            onChange={(event) => updateForm({ intervalDurationMinutes: event.target.value })}
          />
        </WizardField>
      ) : (
        <WizardField label="Stop at">
          <input type="datetime-local" value={form.intervalEndAt} onChange={(event) => updateForm({ intervalEndAt: event.target.value })} />
        </WizardField>
      )}

      {form.intervalStartMode === "now" ? (
        <>
          <RadioGroup
            name="interval-first-run-mode"
            value={form.intervalFirstRunMode}
            options={[
              { value: "default", label: "First run after one interval" },
              { value: "immediate", label: "Run once immediately" },
              { value: "delay", label: "Custom first-run delay" },
            ]}
            onChange={(value) => updateForm({ intervalFirstRunMode: value as IntervalFirstRunMode })}
          />
          {form.intervalFirstRunMode === "delay" ? (
            <WizardField label="First run delay (minutes)">
              <input
                type="number"
                min="0"
                step="1"
                value={form.intervalDelayMinutes}
                onChange={(event) => updateForm({ intervalDelayMinutes: event.target.value })}
              />
            </WizardField>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function RadioGroup({
  name,
  value,
  options,
  onChange,
}: {
  name: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <div className="task-editor-radio-group">
      {options.map((option) => (
        <label key={option.value}>
          <input
            type="radio"
            name={name}
            checked={value === option.value}
            onChange={() => onChange(option.value)}
          />
          <span>{option.label}</span>
        </label>
      ))}
    </div>
  );
}
