import type { ScheduledTaskItem, ScheduledTaskRecurrenceRule, TaskCreateInput, TaskUpdateInput } from "../types";

export const WEEKDAY_OPTIONS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type WeekdayOption = (typeof WEEKDAY_OPTIONS)[number];
export type TaskScheduleKind = "oneshot" | "daily" | "weekly" | "interval";
export type OneShotMode = "delay" | "absolute";
export type IntervalStartMode = "now" | "at";
export type IntervalEndMode = "duration" | "end_at";
export type IntervalFirstRunMode = "default" | "immediate" | "delay";

export interface TaskFormState {
  title: string;
  taskType: "message" | "agent";
  content: string;
  scheduleKind: TaskScheduleKind;
  oneShotMode: OneShotMode;
  delayMinutes: string;
  runAt: string;
  dailyTime: string;
  weeklyTime: string;
  weekdays: WeekdayOption[];
  intervalMinutes: string;
  intervalStartMode: IntervalStartMode;
  intervalStartAt: string;
  intervalEndMode: IntervalEndMode;
  intervalDurationMinutes: string;
  intervalEndAt: string;
  intervalFirstRunMode: IntervalFirstRunMode;
  intervalDelayMinutes: string;
}

const MAX_INTERVAL_DURATION_MINUTES = 30 * 24 * 60;

export function createDefaultTaskFormState(): TaskFormState {
  return {
    title: "",
    taskType: "message",
    content: "",
    scheduleKind: "oneshot",
    oneShotMode: "delay",
    delayMinutes: "5",
    runAt: "",
    dailyTime: "09:00",
    weeklyTime: "09:00",
    weekdays: [],
    intervalMinutes: "5",
    intervalStartMode: "now",
    intervalStartAt: "",
    intervalEndMode: "end_at",
    intervalDurationMinutes: "",
    intervalEndAt: "",
    intervalFirstRunMode: "default",
    intervalDelayMinutes: "",
  };
}

export function inferScheduleKind(task: ScheduledTaskItem): TaskScheduleKind {
  const rule = firstRecurrenceRule(task);
  if (rule?.kind === "interval") return "interval";
  if (rule?.kind === "daily") return "daily";
  if (rule?.kind === "weekly") return "weekly";
  return "oneshot";
}

export function taskToFormState(task: ScheduledTaskItem): TaskFormState {
  const state = createDefaultTaskFormState();
  const rule = firstRecurrenceRule(task);
  state.title = task.title || "";
  state.taskType = task.task_type === "agent" ? "agent" : "message";
  state.content = task.content || "";
  state.scheduleKind = inferScheduleKind(task);

  if (state.scheduleKind === "interval" && rule) {
    state.intervalMinutes = secondsToMinutesString(rule.every_seconds, "5");
    state.intervalEndMode = "end_at";
    state.intervalEndAt = fromApiDateTime(rule.end_at || "");
    if (rule.start_at) {
      state.intervalStartMode = "at";
      state.intervalStartAt = fromApiDateTime(rule.start_at);
    }
    return state;
  }

  if (state.scheduleKind === "daily" && rule) {
    state.dailyTime = fromApiTime(rule.time || "09:00:00");
    return state;
  }

  if (state.scheduleKind === "weekly" && rule) {
    state.weeklyTime = fromApiTime(rule.time || "09:00:00");
    state.weekdays = normalizeWeekdays(rule.weekdays);
    return state;
  }

  state.oneShotMode = "absolute";
  state.runAt = fromApiDateTime(task.next_run_at);
  return state;
}

export function validateTaskForm(state: TaskFormState): string | null {
  if (!state.content.trim()) return "Content is required.";

  if (state.scheduleKind === "oneshot") {
    if (state.oneShotMode === "delay") {
      const delay = parseNumber(state.delayMinutes);
      if (delay == null || delay < 0) return "Delay must be zero or positive.";
      return null;
    }
    return validateFutureDateTime(state.runAt, "Run at");
  }

  if (state.scheduleKind === "daily") {
    if (!isValidTimeInput(state.dailyTime)) return "Daily time is required.";
    return null;
  }

  if (state.scheduleKind === "weekly") {
    if (!isValidTimeInput(state.weeklyTime)) return "Weekly time is required.";
    if (state.weekdays.length === 0) return "Select at least one weekday.";
    return null;
  }

  const everyMinutes = parseNumber(state.intervalMinutes);
  if (everyMinutes == null || everyMinutes < 1) return "Interval must be at least 1 minute.";

  if (state.intervalStartMode === "at") {
    const startError = validateFutureDateTime(state.intervalStartAt, "Start at");
    if (startError) return startError;
  }

  if (state.intervalEndMode === "duration") {
    const durationMinutes = parseNumber(state.intervalDurationMinutes);
    if (durationMinutes == null || durationMinutes <= 0) return "Interval duration must be positive.";
    if (durationMinutes > MAX_INTERVAL_DURATION_MINUTES) return "Interval duration cannot exceed 30 days.";
  } else {
    const endError = validateFutureDateTime(state.intervalEndAt, "Stop at");
    if (endError) return endError;
  }

  if (state.intervalStartMode === "at") {
    const startMs = parseDateTime(state.intervalStartAt);
    const endMs =
      state.intervalEndMode === "duration"
        ? startMs + minutesToSeconds(state.intervalDurationMinutes) * 1000
        : parseDateTime(state.intervalEndAt);
    if (endMs <= startMs) return "Stop time must be after start time.";
  }

  if (state.intervalStartMode === "now" && state.intervalFirstRunMode === "delay") {
    const delay = parseNumber(state.intervalDelayMinutes);
    if (delay == null || delay < 0) return "First run delay must be zero or positive.";
  }

  return null;
}

export function formStateToCreateInput(state: TaskFormState): TaskCreateInput {
  return {
    task_type: state.taskType,
    title: normalizedTitle(state.title),
    content: state.content.trim(),
    channel: "api",
    ...schedulePayloadForCreate(state),
  };
}

export function formStateToUpdateInput(state: TaskFormState, original: ScheduledTaskItem): TaskUpdateInput {
  const patch: TaskUpdateInput = {
    title: normalizedTitle(state.title),
    content: state.content.trim(),
    task_type: state.taskType,
  };

  if (!hasScheduleChanged(state, original)) return patch;

  return {
    ...patch,
    ...schedulePayloadForUpdate(state, original),
  };
}

export function toApiDateTime(value: string): string {
  const text = value.trim();
  if (!text) return "";
  const normalized = text.replace("T", " ");
  return normalized.length === 16 ? `${normalized}:00` : normalized;
}

export function fromApiDateTime(value: string): string {
  const text = String(value || "").trim();
  if (!text) return "";
  const normalized = text.replace(" ", "T");
  return normalized.slice(0, 16);
}

export function toApiTime(value: string): string {
  const text = value.trim();
  if (!text) return "";
  return text.length === 5 ? `${text}:00` : text;
}

function schedulePayloadForCreate(state: TaskFormState): Partial<TaskCreateInput> {
  if (state.scheduleKind === "oneshot") {
    return state.oneShotMode === "delay"
      ? { delay_seconds: minutesToSeconds(state.delayMinutes) }
      : { run_at: toApiDateTime(state.runAt) };
  }

  if (state.scheduleKind === "daily") {
    return { recurrence: [{ kind: "daily", time: toApiTime(state.dailyTime) }] };
  }

  if (state.scheduleKind === "weekly") {
    return { recurrence: [{ kind: "weekly", time: toApiTime(state.weeklyTime), weekdays: state.weekdays }] };
  }

  return intervalPayload(state);
}

function schedulePayloadForUpdate(state: TaskFormState, original: ScheduledTaskItem): Partial<TaskUpdateInput> {
  if (state.scheduleKind === "oneshot") {
    return state.oneShotMode === "delay"
      ? { delay_seconds: minutesToSeconds(state.delayMinutes) }
      : { run_at: toApiDateTime(state.runAt) };
  }

  if (state.scheduleKind === "daily") {
    return { recurrence: [{ kind: "daily", time: toApiTime(state.dailyTime) }] };
  }

  if (state.scheduleKind === "weekly") {
    return { recurrence: [{ kind: "weekly", time: toApiTime(state.weeklyTime), weekdays: state.weekdays }] };
  }

  if (inferScheduleKind(original) === "interval") {
    return intervalPayload(state);
  }

  return {
    recurrence: [intervalRecurrenceRule(state)],
    ...(state.intervalStartMode === "now" ? intervalFirstRunPayload(state) : {}),
  };
}

function intervalPayload(state: TaskFormState): Partial<TaskCreateInput & TaskUpdateInput> {
  const payload: Partial<TaskCreateInput & TaskUpdateInput> = {
    interval_seconds: minutesToSeconds(state.intervalMinutes),
  };
  if (state.intervalStartMode === "at") {
    payload.start_at = toApiDateTime(state.intervalStartAt);
    payload.end_at = resolveIntervalEndAt(state);
    return payload;
  }
  if (state.intervalEndMode === "duration") {
    payload.duration_seconds = minutesToSeconds(state.intervalDurationMinutes);
  } else {
    payload.end_at = toApiDateTime(state.intervalEndAt);
  }
  return {
    ...payload,
    ...intervalFirstRunPayload(state),
  };
}

function intervalRecurrenceRule(state: TaskFormState): ScheduledTaskRecurrenceRule {
  const rule: ScheduledTaskRecurrenceRule = {
    kind: "interval",
    every_seconds: minutesToSeconds(state.intervalMinutes),
    end_at: resolveIntervalEndAt(state),
  };
  if (state.intervalStartMode === "at") {
    rule.start_at = toApiDateTime(state.intervalStartAt);
  }
  return rule;
}

function resolveIntervalEndAt(state: TaskFormState): string {
  if (state.intervalEndMode === "duration") {
    if (state.intervalStartMode === "at") {
      return toApiDateTime(
        formatDateTimeLocal(new Date(parseDateTime(state.intervalStartAt) + minutesToSeconds(state.intervalDurationMinutes) * 1000)),
      );
    }
    return endAtFromDuration(state.intervalDurationMinutes);
  }
  return toApiDateTime(state.intervalEndAt);
}

function intervalFirstRunPayload(state: TaskFormState): Partial<TaskCreateInput & TaskUpdateInput> {
  if (state.intervalStartMode === "at") return {};
  if (state.intervalFirstRunMode === "immediate") return { delay_seconds: 0 };
  if (state.intervalFirstRunMode === "delay") return { delay_seconds: minutesToSeconds(state.intervalDelayMinutes) };
  return {};
}

function hasScheduleChanged(state: TaskFormState, original: ScheduledTaskItem): boolean {
  return scheduleSignature(state) !== scheduleSignature(taskToFormState(original));
}

function scheduleSignature(state: TaskFormState): string {
  if (state.scheduleKind === "oneshot") {
    return `oneshot:${state.oneShotMode}:${state.oneShotMode === "delay" ? minutesToSeconds(state.delayMinutes) : toApiDateTime(state.runAt)}`;
  }
  if (state.scheduleKind === "daily") return `daily:${toApiTime(state.dailyTime)}`;
  if (state.scheduleKind === "weekly") return `weekly:${toApiTime(state.weeklyTime)}:${state.weekdays.join(",")}`;
  return [
    "interval",
    minutesToSeconds(state.intervalMinutes),
    state.intervalStartMode,
    state.intervalStartMode === "at" ? toApiDateTime(state.intervalStartAt) : "",
    state.intervalEndMode,
    state.intervalEndMode === "duration" ? minutesToSeconds(state.intervalDurationMinutes) : toApiDateTime(state.intervalEndAt),
    state.intervalStartMode === "now" ? state.intervalFirstRunMode : "",
    state.intervalStartMode === "now" && state.intervalFirstRunMode === "delay" ? minutesToSeconds(state.intervalDelayMinutes) : "",
  ].join(":");
}

function firstRecurrenceRule(task: ScheduledTaskItem): ScheduledTaskRecurrenceRule | null {
  return Array.isArray(task.recurrence) ? task.recurrence[0] || null : null;
}

function normalizeWeekdays(value: unknown): WeekdayOption[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is WeekdayOption => WEEKDAY_OPTIONS.includes(item as WeekdayOption));
}

function fromApiTime(value: string): string {
  return String(value || "").slice(0, 5) || "09:00";
}

function normalizedTitle(value: string): string {
  return value.trim() || "Reminder";
}

function minutesToSeconds(value: string): number {
  return Math.round((parseNumber(value) || 0) * 60);
}

function secondsToMinutesString(value: unknown, fallback: string): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return fallback;
  return String(Math.max(1, Math.round(seconds / 60)));
}

function parseNumber(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parseDateTime(value: string): number {
  return new Date(value).getTime();
}

function validateFutureDateTime(value: string, label: string): string | null {
  if (!value.trim()) return `${label} is required.`;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return `${label} must be a valid date and time.`;
  if (parsed.getTime() <= Date.now()) return `${label} must be in the future.`;
  return null;
}

function isValidTimeInput(value: string): boolean {
  return /^\d{2}:\d{2}$/.test(value.trim());
}

function endAtFromDuration(durationMinutes: string): string {
  return toApiDateTime(formatDateTimeLocal(new Date(Date.now() + minutesToSeconds(durationMinutes) * 1000)));
}

function formatDateTimeLocal(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
