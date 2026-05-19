export type RoutePath = "/" | "/memory" | "/message" | "/workspace" | "/agent";

export type ChatRole = "user" | "assistant" | "observation";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  meta?: string;
  images?: string[];
  pending?: boolean;
  error?: boolean;
}

export interface ChatSettings {
  userId: string;
  stream: boolean;
  memory: boolean;
}

export interface ChatPanelState {
  id: "single";
  messages: ChatMessage[];
  pendingImages: string[];
  settings: ChatSettings;
  sending: boolean;
}

export interface ChatEvent {
  type?: string;
  event?: string;
  delta?: string;
  content?: string;
  message?: unknown;
  message_id?: string;
  phase?: string;
  error?: string;
  status_code?: number;
}

export interface AgentInfo {
  provider?: string;
  model: string;
  workspace: string;
  workspace_dir: string;
  memory_dir: string;
  message_storage: Record<string, unknown>;
  tools: string[];
  identity?: string;
  identity_file?: string;
  identity_path?: string;
  identity_editable?: boolean;
  system_prompt?: string;
}

export interface AgentIdentity {
  identity: string;
  path: string;
  filename: string;
  modified: number;
}

export interface FileNode {
  name: string;
  path: string;
  type: "dir" | "file";
  children?: FileNode[];
  size?: number;
  modified?: number;
  mime_type?: string;
  binary?: boolean;
}

export interface FileReadResult extends FileNode {
  content: string;
  text?: boolean;
  blob_url?: string;
}

export interface SearchResult extends FileNode {
  matched_in: string[];
  snippet?: string;
}

export interface MessageItem {
  role: string;
  type: string;
  content: string;
  sender_id?: string;
  timestamp?: number;
  metadata?: Record<string, unknown>;
  tool_call?: {
    name: string;
    arguments: unknown;
    output: unknown;
  };
}

export interface MessageSearchResult extends MessageItem {
  matched_in: string[];
  snippet?: string;
}

export interface MessagesResponse {
  messages: MessageItem[];
  total: number;
  count: number;
  offset: number;
  has_more: boolean;
}

export interface MessagesStats {
  total: number;
  storage?: Record<string, unknown>;
  earliest_timestamp?: number;
  latest_timestamp?: number;
}

export interface MessageSearchResponse {
  query: string;
  results: MessageSearchResult[];
}
