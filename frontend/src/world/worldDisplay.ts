import type { WorldEvent } from "./protocol";

export const REPLY_WAIT_TIMEOUT_MS = 90_000;

export type ChatRow =
  | { kind: "event"; event: WorldEvent; key: string }
  | { kind: "reconnected"; key: string };

/** Collapse back-to-back self leave+join (e.g. F5 refresh) into one line. */
export function buildChatRows(events: WorldEvent[], memberId: string): ChatRow[] {
  const rows: ChatRow[] = [];
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    const next = events[index + 1];
    const seq = event.seq ?? index;
    if (
      event.kind === "leave" &&
      event.actor_id === memberId &&
      next?.kind === "join" &&
      next.actor_id === memberId
    ) {
      rows.push({ kind: "reconnected", key: `reconnect-${seq}` });
      index += 1;
      continue;
    }
    rows.push({
      kind: "event",
      event,
      key: String(event.seq ?? `${event.kind}-${event.actor_id}-${index}`),
    });
  }
  return rows;
}

export function systemEventLabel(
  event: WorldEvent,
  memberId: string,
  displayOf: (id: string) => string,
): string {
  const kind = event.kind || "";
  const actor = event.actor_id || "";
  const name = displayOf(actor);
  const mine = actor === memberId;
  if (kind === "join") {
    return mine ? "你已进入" : `${name} 加入了`;
  }
  if (kind === "leave") {
    return mine ? "你已离开" : `${name} 离开了`;
  }
  return `[${kind}] ${mine ? "you" : name}${event.text ? `: ${event.text}` : ""}`;
}

export function countOtherPresent(
  present: Array<{ member_id: string }>,
  memberId: string,
): number {
  return present.filter((item) => item.member_id !== memberId).length;
}

export function hasReplyAfterSeq(events: WorldEvent[], memberId: string, afterSeq: number): boolean {
  for (const event of events) {
    const seq = Number(event.seq || 0);
    if (seq <= afterSeq) continue;
    if (event.kind === "utterance" && event.actor_id && event.actor_id !== memberId) {
      return true;
    }
  }
  return false;
}

export function lastSelfUtteranceSeq(events: WorldEvent[], memberId: string): number {
  let max = 0;
  for (const event of events) {
    if (event.kind !== "utterance" || event.actor_id !== memberId) continue;
    max = Math.max(max, Number(event.seq || 0));
  }
  return max;
}
