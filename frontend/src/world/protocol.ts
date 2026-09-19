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

export type MemberKind = "human" | "agent" | "script";

export interface WorldSummary {
  id: string;
  name: string;
  latest_seq?: number;
  present_count?: number;
  present_by_kind?: Partial<Record<MemberKind, number>>;
}

export interface WorldEvent {
  type?: string;
  seq?: number;
  ts?: number;
  kind?: EventKind;
  actor_id?: string;
  /** Display name at the time of the event (hub >= this protocol revision). */
  actor_name?: string;
  actor_kind?: MemberKind;
  /** Subject kind on welcome (not event kind). */
  member_kind?: MemberKind;
  capabilities?: string[];
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
  resume_token?: string;
}

export interface WorldMember {
  member_id: string;
  display_name?: string;
  kind?: MemberKind;
}

export function memberKind(item: WorldMember): MemberKind {
  const k = String(item.kind || "human").toLowerCase();
  if (k === "agent" || k === "script") return k;
  return "human";
}

export function isAgentKind(kind: MemberKind): boolean {
  return kind === "agent" || kind === "script";
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

/**
 * The person at this browser. They pick a name once; the browser remembers it
 * so the same person is the same member across worlds and reloads.
 */
export const PERSON_IDENTITY_STORAGE_KEY = "xagent.world.person";
/** Per-world resume token (sessionStorage); lets a refresh take the same body over. */
export const RESUME_TOKEN_STORAGE_PREFIX = "xagent.world.resumeToken:";
/** Must match agents_world.paths._MEMBER_ID_RE. */
export const MEMBER_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
export const MEMBER_ID_RULE =
  "Handle must be 1-64 characters of letters, digits, dots, hyphens, or underscores, starting with a letter or digit.";
export const MAX_DISPLAY_NAME_LENGTH = 64;

export type PersonIdentity = {
  member_id: string;
  display_name: string;
};

/** A wire-safe handle derived from a display name; random when the name has no ASCII letters. */
export function suggestMemberId(displayName: string): string {
  const ascii = String(displayName || "")
    .normalize("NFKD")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "")
    .replace(/-+/g, "-")
    .replace(/[-._]+$/, "")
    .toLowerCase()
    .slice(0, 64);
  if (ascii && MEMBER_ID_RE.test(ascii)) return ascii;
  return `p-${Math.random().toString(36).slice(2, 8)}`;
}

export function validateMemberId(memberId: string, taken: Set<string> = new Set()): string {
  const value = memberId.trim();
  if (!value) return "Handle is required";
  if (!MEMBER_ID_RE.test(value)) return MEMBER_ID_RULE;
  if (value === "world") return "'world' is reserved";
  if (taken.has(value)) return `'${value}' is a local agent; pick another handle`;
  return "";
}

export function validateDisplayName(name: string): string {
  const value = name.trim();
  if (!value) return "Name is required";
  if (value.length > MAX_DISPLAY_NAME_LENGTH) return `Name must be at most ${MAX_DISPLAY_NAME_LENGTH} characters`;
  return "";
}

export function loadPersonIdentity(): PersonIdentity | null {
  try {
    const raw = localStorage.getItem(PERSON_IDENTITY_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PersonIdentity>;
    const member_id = String(parsed.member_id || "").trim();
    const display_name = String(parsed.display_name || "").trim();
    if (!MEMBER_ID_RE.test(member_id) || !display_name) return null;
    return { member_id, display_name };
  } catch {
    return null;
  }
}

export function savePersonIdentity(identity: PersonIdentity): void {
  try {
    localStorage.setItem(PERSON_IDENTITY_STORAGE_KEY, JSON.stringify(identity));
  } catch {
    /* ignore quota / private mode */
  }
}

export function loadResumeToken(worldId: string, memberId: string): string {
  try {
    return sessionStorage.getItem(`${RESUME_TOKEN_STORAGE_PREFIX}${worldId}:${memberId}`) || "";
  } catch {
    return "";
  }
}

export function saveResumeToken(worldId: string, memberId: string, token: string): void {
  try {
    const key = `${RESUME_TOKEN_STORAGE_PREFIX}${worldId}:${memberId}`;
    if (token) sessionStorage.setItem(key, token);
    else sessionStorage.removeItem(key);
  } catch {
    /* ignore */
  }
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
