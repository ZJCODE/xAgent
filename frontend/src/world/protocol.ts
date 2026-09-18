export const MAX_ATTACHMENTS = 4;
export const MAX_FILE_BYTES = 8 * 1024 * 1024;
/** Must match agents_world.MAX_TEXT_LENGTH */
export const MAX_SPEAK_TEXT_LENGTH = 8000;
export const LAST_WORLD_STORAGE_KEY = "xagent.world.lastWorldId";
export const WORLD_NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;
export const WORLD_NAME_RULE =
  "Name must start with a lowercase letter and use only lowercase letters, digits, hyphens, or underscores.";
export { DEFAULT_AGENT_WEB_URL } from "../lib/ports";

export type EventKind = "utterance" | "join" | "leave" | string;

export interface WorldAttachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  url?: string;
}

export interface PendingFile {
  id: string;
  name: string;
  mime: string;
  size: number;
  file: File;
  previewUrl: string;
}

export interface WorldSummary {
  id: string;
  name: string;
  latest_seq?: number;
  present_count?: number;
}

export interface WorldEvent {
  type?: string;
  seq?: number;
  ts?: number;
  kind?: EventKind;
  actor_id?: string;
  text?: string;
  mentions?: string[];
  attachments?: WorldAttachment[];
  name?: string;
  present?: WorldMember[] | boolean;
  events?: WorldEvent[];
  sync?: boolean;
  has_more?: boolean;
  next_after_seq?: number;
  after_seq?: number;
  code?: string;
  message?: string;
  protocol_version?: number;
  world_id?: string;
  member_id?: string;
  display_name?: string;
  latest_seq?: number;
}

export interface WorldMember {
  member_id: string;
  display_name?: string;
}

export interface NeighborAgent {
  name: string;
  title?: string;
  running?: boolean;
  world_ready?: boolean;
  api_url: string;
}

export function worldFileUrl(attachment: WorldAttachment | string, worldId?: string): string {
  if (typeof attachment !== "string") {
    const explicit = String(attachment.url || "").trim();
    if (explicit) return explicit;
    if (worldId) return `/worlds/${encodeURIComponent(worldId)}/files/${encodeURIComponent(attachment.id)}`;
    return "";
  }
  if (worldId) return `/worlds/${encodeURIComponent(worldId)}/files/${encodeURIComponent(attachment)}`;
  return "";
}

export function isImageMime(mime?: string, name?: string): boolean {
  if (mime && mime.startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(String(name || ""));
}

export async function fileToBase64(file: File): Promise<string> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsDataURL(file);
  });
  const comma = dataUrl.indexOf(",");
  return comma >= 0 ? dataUrl.slice(comma + 1) : "";
}

export function hubHttpOrigin(): string {
  if (import.meta.env.DEV) return "http://127.0.0.1:7182";
  return window.location.origin;
}

export function worldWsUrl(worldId: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const id = encodeURIComponent(worldId);
  if (import.meta.env.DEV) {
    return `${proto}//127.0.0.1:7182/ws/${id}`;
  }
  return `${proto}//${window.location.host}/ws/${id}`;
}

/** Wire id for the human on this page. */
export const HUMAN_MEMBER_ID = "human";
export const HUMAN_DISPLAY_NAME = "human";

export type HumanIdentity = {
  member_id: string;
  display_name: string;
};

/** `human`; `human-2`… only if a local agent already took the id. */
export function resolveHumanIdentity(taken: Set<string>): HumanIdentity {
  if (!taken.has(HUMAN_MEMBER_ID)) {
    return { member_id: HUMAN_MEMBER_ID, display_name: HUMAN_DISPLAY_NAME };
  }
  for (let n = 2; n < 100; n += 1) {
    const member_id = `${HUMAN_MEMBER_ID}-${n}`;
    if (!taken.has(member_id)) {
      return { member_id, display_name: HUMAN_DISPLAY_NAME };
    }
  }
  return { member_id: HUMAN_MEMBER_ID, display_name: HUMAN_DISPLAY_NAME };
}

export function validateWorldName(name: string): string {
  const value = name.trim();
  if (!WORLD_NAME_RE.test(value)) return WORLD_NAME_RULE;
  return "";
}

export function initialOf(name: string): string {
  const text = String(name || "?").trim();
  return (text[0] || "?").toUpperCase();
}

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export type MentionPerson = {
  member_id: string;
  display_name?: string;
  /** False when listed from local agents but not currently in the world. */
  here?: boolean;
};

export function mentionLabel(person: MentionPerson): string {
  return String(person.display_name || person.member_id).trim() || person.member_id;
}

export function mentionToken(person: MentionPerson): string {
  const label = mentionLabel(person);
  if (/^[A-Za-z0-9._-]+$/.test(label)) return label;
  return person.member_id;
}

export function extractMentions(text: string, people: MentionPerson[]): string[] {
  const found = new Set<string>();
  for (const person of people) {
    const tokens = [...new Set([person.member_id, person.display_name].filter(Boolean) as string[])];
    for (const token of tokens) {
      const pattern = new RegExp(`(?:^|\\s)@${escapeRegExp(token)}(?=$|\\s|[.,!?，。！？])`, "i");
      if (pattern.test(text)) found.add(person.member_id);
    }
  }
  return [...found];
}

export function findMentionQuery(
  text: string,
  cursor: number,
): { start: number; query: string } | null {
  const before = text.slice(0, Math.max(0, cursor));
  const match = /(?:^|[\s([{])@([A-Za-z0-9._-]*)$/.exec(before);
  if (!match) return null;
  return { start: before.lastIndexOf("@"), query: match[1] };
}

export function insertMention(
  text: string,
  cursor: number,
  person: MentionPerson,
): { text: string; cursor: number } {
  const active = findMentionQuery(text, cursor);
  const token = `@${mentionToken(person)} `;
  if (!active) {
    const next = `${text.slice(0, cursor)}${token}${text.slice(cursor)}`;
    return { text: next, cursor: cursor + token.length };
  }
  const next = `${text.slice(0, active.start)}${token}${text.slice(cursor)}`;
  return { text: next, cursor: active.start + token.length };
}

export function filterMentionPeople(people: MentionPerson[], query: string): MentionPerson[] {
  const needle = query.trim().toLowerCase();
  const seen = new Set<string>();
  const here: MentionPerson[] = [];
  const away: MentionPerson[] = [];
  for (const person of people) {
    if (!person.member_id || seen.has(person.member_id)) continue;
    seen.add(person.member_id);
    if (needle) {
      const hay = `${person.member_id} ${person.display_name || ""}`.toLowerCase();
      if (!hay.includes(needle)) continue;
    }
    if (person.here === false) away.push(person);
    else here.push(person);
  }
  return [...here, ...away];
}
