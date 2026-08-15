export const SCHEDULED_AGENT_PROMPT_PREFIX =
  "This scheduled task is now due. Execute it and return the message to deliver.\n\nTask: ";

export function isScheduledWork(metadata?: Record<string, unknown> | null): boolean {
  if (!metadata || typeof metadata !== "object") return false;
  const kind = String(metadata.inbox_kind || "").trim();
  if (kind === "scheduled_turn") return true;
  return String(metadata.source || "").trim() === "scheduled_task";
}

export function scheduledTaskDisplayContent(
  content?: string | null,
  metadata?: Record<string, unknown> | null,
): string {
  const stored = String(metadata?.task_content || "").trim();
  if (stored) return stored;
  const text = String(content || "");
  if (text.startsWith(SCHEDULED_AGENT_PROMPT_PREFIX)) {
    return text.slice(SCHEDULED_AGENT_PROMPT_PREFIX.length).trim();
  }
  return text.trim();
}
