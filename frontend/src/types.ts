export type RoutePath = "/" | "/memory" | "/message" | "/workspace" | "/skills" | "/agent";

export type ChatRole = "user" | "assistant" | "observation";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  meta?: string;
  images?: string[];
  imageCount?: number;
  attachments?: AttachmentAsset[];
  attachmentCount?: number;
  pending?: boolean;
  error?: boolean;
}

export interface AttachmentAsset {
  kind?: string;
  path?: string;
  workspace_path?: string;
  blob_url?: string;
  mime_type?: string;
  size_bytes?: number;
  file_name?: string;
  original_name?: string;
  caption?: string;
  source_channel?: string;
  source_message_id?: string;
  source_resource_id?: string;
  source_resource_type?: string;
}

export interface ImageAsset extends AttachmentAsset {
  external_url?: string;
  width?: number;
  height?: number;
}

export interface ChatSettings {
  userId: string;
  stream: boolean;
  memory: boolean;
}

export interface ChatPanelState {
  id: "single";
  messages: ChatMessage[];
  pendingAttachments: AttachmentAsset[];
  settings: ChatSettings;
  sending: boolean;
}

export interface AgentCapabilities {
  vision: boolean;
  vision_input?: boolean;
  web_search: boolean;
  image_generation: boolean;
  image_generation_provider?: string;
  image_editing?: boolean;
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
  skills_dir?: string;
  memory_dir: string;
  message_storage: Record<string, unknown>;
  tools: string[];
  capabilities?: Partial<AgentCapabilities>;
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

export interface WorkspaceUploadResult extends FileNode {
  status: string;
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
  images?: ImageAsset[];
  image_count?: number;
  attachments?: AttachmentAsset[];
  attachment_count?: number;
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

export interface SkillValidationIssue {
  path: string;
  code: string;
  message: string;
}

export interface SkillMetadata {
  name: string;
  description: string;
  path: string;
  skill_file: string;
  enabled: boolean;
  valid: boolean;
  modified?: number;
  license?: string;
  compatibility?: string;
  metadata?: Record<string, unknown>;
  allowed_tools?: string;
  errors: SkillValidationIssue[];
}

export interface SkillsValidationResult {
  valid: boolean;
  skills: Array<{
    name: string;
    path: string;
    valid: boolean;
    errors: SkillValidationIssue[];
  }>;
}

export interface SkillsInfo {
  root: string;
  count: number;
  enabled_count: number;
  disabled_count: number;
  invalid_count: number;
  skills: SkillMetadata[];
  validation: SkillsValidationResult;
}

export interface SkillsTreeResponse {
  root: string;
  tree: FileNode[];
  skills: SkillMetadata[];
}

export interface SkillCreateInput {
  name: string;
  description: string;
  body?: string;
  license?: string;
  compatibility?: string;
  metadata?: Record<string, unknown>;
  allowed_tools?: string;
}

export interface SkillCreateResponse {
  status: string;
  skill: SkillMetadata;
}

export interface SkillStateResponse {
  status: string;
  skill: SkillMetadata;
}
