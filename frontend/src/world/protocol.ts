export const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
export const STORE_ID = "world.identity";
export const WORLD_ACTOR_ID = "world";
export const MAX_ATTACHMENTS = 4;
export const MAX_FILE_BYTES = 8 * 1024 * 1024;

export type EventKind = "utterance" | "join" | "leave" | "scene" | string;

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

export interface WorldEvent {
  type?: string;
  seq?: number;
  room_seq?: number;
  ts?: number;
  room_id?: string;
  kind?: EventKind;
  actor_id?: string;
  text?: string;
  mentions?: string[];
  attachments?: WorldAttachment[];
  name?: string;
  setting?: string;
  present?: WorldMember[];
  events?: WorldEvent[];
  sync?: boolean;
  has_more?: boolean;
  next_after_seq?: number;
  after_seq?: number;
  code?: string;
  message?: string;
  protocol_version?: number;
  world_id?: string;
  rooms?: WorldRoom[];
  member_id?: string;
  display_name?: string;
  present_rooms?: string[];
}

export interface WorldRoom {
  id: string;
  name?: string;
  setting?: string;
  latest_seq?: number;
  latest_room_seq?: number;
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

export function worldFileUrl(attachment: WorldAttachment | string): string {
  if (typeof attachment !== "string") {
    const explicit = String(attachment.url || "").trim();
    if (explicit) return explicit;
    return `/files/${encodeURIComponent(attachment.id)}`;
  }
  return `/files/${encodeURIComponent(attachment)}`;
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

export function worldWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  if (import.meta.env.DEV) {
    return `${proto}//127.0.0.1:7182/`;
  }
  return `${proto}//${window.location.host}/`;
}

export function loadIdentity(): string {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_ID) || "{}") as {
      member_id?: string;
      display_name?: string;
    };
    return saved.member_id || saved.display_name || "";
  } catch {
    return "";
  }
}

export function saveIdentity(memberId: string): void {
  localStorage.setItem(STORE_ID, JSON.stringify({ member_id: memberId }));
}

export function initialOf(name: string): string {
  const text = String(name || "?").trim();
  return (text[0] || "?").toUpperCase();
}

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function extractMentions(
  text: string,
  people: Array<{ member_id: string; display_name?: string }>,
): string[] {
  const found = new Set<string>();
  for (const person of people) {
    const tokens = [person.member_id, person.display_name].filter(Boolean) as string[];
    for (const token of tokens) {
      const pattern = new RegExp(`(?:^|\\s)@${escapeRegExp(token)}(?=$|\\s|[.,!?，。！？])`, "i");
      if (pattern.test(text)) found.add(person.member_id);
    }
  }
  return [...found];
}

export function formatEventTime(ts?: number): string {
  if (!ts) return "";
  const ms = ts > 1e12 ? ts : ts * 1000;
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function validateMemberId(memberId: string, taken: Set<string>): string {
  if (!ID_RE.test(memberId)) {
    return "名字需为 1-64 位字母、数字、点、下划线或短横，且以字母或数字开头";
  }
  if (memberId === WORLD_ACTOR_ID) {
    return "'world' 是世界自己，不能占用";
  }
  if (taken.has(memberId)) {
    return "这个名字已被占用";
  }
  return "";
}
