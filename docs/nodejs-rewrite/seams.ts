/**
 * xAgent-native TypeScript seams (design sketch only).
 *
 * This is not a runtime and must not be imported by the Python product.
 * It shows how a Node rewrite would look if we kept GOAL.md instead of
 * becoming a DeepSeek Harness profile.
 *
 * Mapping:
 *   AgentLoop / ModelClient  ->  LlmAdapter + AgentRuntime
 *   MessageStorage           ->  MessageStore (SQLite, same files)
 *   MarkdownMemory           ->  DiaryMemory
 *   Feishu/Weixin/Voice/Web  ->  ChannelAdapter
 *   Subconscious + scheduler ->  LifeRuntime
 */

export type ChannelName = "web" | "cli" | "api" | "feishu" | "weixin" | "voice";

export type Role = "user" | "assistant" | "tool" | "system" | "observation";

/** One row in the agent-owned message stream. Not a dsh SessionEvent. */
export interface AgentMessage {
  id: string;
  role: Role;
  content: string;
  senderId: string;
  recipientId: string;
  channel?: ChannelName;
  roomName?: string;
  createdAt: string;
  imageSources?: string[];
  attachments?: Record<string, unknown>[];
  metadata?: Record<string, unknown>;
}

export interface ParticipationDecision {
  shouldReply: boolean;
  reason: string;
}

export interface ChatTurnInput {
  userMessage: string;
  userId: string;
  channel?: ChannelName;
  roomName?: string;
  imageSource?: string | string[];
  attachments?: Record<string, unknown>[];
  channelInstructions?: string;
  stream?: boolean;
}

/** Structured events already consumed by the Web UI and channel adapters. */
export type ChatEvent =
  | { type: "message_delta"; message_id: string; delta: string }
  | { type: "message_done"; message_id: string; content: string; phase: string }
  | { type: "tool_call"; name: string; arguments: string }
  | { type: "tool_result"; name: string; preview: string }
  | { type: "error"; error: string };

export interface LlmAdapter {
  readonly providerName: string;
  readonly model: string;
  complete(request: unknown): Promise<unknown>;
  stream(request: unknown): AsyncIterable<unknown>;
}

export interface ToolHandler {
  name: string;
  schema: Record<string, unknown>;
  execute(args: Record<string, unknown>): Promise<string>;
}

export interface MessageStore {
  append(message: AgentMessage): Promise<void>;
  recent(limit: number): Promise<AgentMessage[]>;
}

export interface DiaryMemory {
  appendDaily(content: string, day?: string): Promise<string>;
  search(terms: string[], scope?: "daily" | "weekly" | "monthly" | "yearly" | "all"): Promise<string>;
}

export interface ChannelAdapter {
  readonly name: ChannelName;
  start(): Promise<void>;
  stop(): Promise<void>;
  deliver(userId: string, text: string, extras?: Record<string, unknown>): Promise<void>;
}

export interface LifeRuntime {
  upsertContact(channel: ChannelName, userId: string, target: Record<string, unknown>): Promise<void>;
  tickSubconscious(): Promise<void>;
  tickScheduler(): Promise<void>;
}

/**
 * Product kernel. Channels talk to this; they do not own memory.
 * A dsh rewrite would invert this: session log owns history, plugins own tools.
 * xAgent keeps one agent-owned stream plus diary.
 */
export interface AgentRuntime {
  chatEvents(input: ChatTurnInput): AsyncIterable<ChatEvent>;
  decideParticipation(input: ChatTurnInput): Promise<ParticipationDecision>;
}

export interface XAgentHost {
  agent: AgentRuntime;
  llm: LlmAdapter;
  tools: ToolHandler[];
  messages: MessageStore;
  memory: DiaryMemory;
  channels: ChannelAdapter[];
  life: LifeRuntime;
}

/**
 * Why this is not a Cordis profile:
 * - Host owns one agent identity and one diary, not a stack of coding sessions.
 * - ChannelAdapter is a first-class seam; dsh has no Feishu/Weixin/voice seam.
 * - LifeRuntime (subconscious + contacts) is product core, not a job plugin.
 * - MessageStore must read ~/.xagent SQLite, not a new harness home.
 *
 * Channel-free minimal spike: keep DiaryMemory + identity prompt only.
 * Drop ChannelAdapter and LifeRuntime. Do not reimplement LlmAdapter or the
 * tool loop; use dsh or a few-dozen-line chat-completions host.
 */
