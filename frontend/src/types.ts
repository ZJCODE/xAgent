export type RoutePath = "/" | "/memory" | "/message" | "/workspace" | "/skills" | "/tasks" | "/channels" | "/agent";

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
  client?: string;
}

export interface ImageAsset extends AttachmentAsset {
  external_url?: string;
  width?: number;
  height?: number;
}

export interface ChatSettings {
  userId: string;
  stream: boolean;
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
  attachments?: AttachmentAsset[];
  message?: unknown;
  message_id?: string;
  phase?: string;
  error?: string;
  status_code?: number;
  task?: ScheduledTaskItem;
}

export interface ScheduledTaskRecurrenceRule {
  kind: "daily" | "weekly" | string;
  time: string;
  weekdays?: string[];
}

export interface ScheduledTaskItem {
  task_id: string;
  title: string;
  task_type: "message" | "agent" | string;
  content: string;
  next_run_at: string;
  recurrence?: ScheduledTaskRecurrenceRule[] | null;
  status: "active" | string;
  channel?: string;
  user_id?: string;
  target?: Record<string, unknown>;
}

export interface TasksResponse {
  root: string;
  tasks: ScheduledTaskItem[];
  total: number;
}

export type ChannelId = "api" | "voice" | "feishu" | "weixin";

export type ChannelRuntimeStatus = "running" | "stopped" | "disabled" | "error";

export interface ChannelStatus {
  id: ChannelId;
  label: string;
  status: ChannelRuntimeStatus;
  configured: boolean;
  ready: boolean;
  pid: number | null;
  detail: string;
  pid_path: string;
  log_path: string;
  can_start: boolean;
  can_stop: boolean;
  can_restart: boolean;
  setup_hint: string;
}

export interface ChannelsResponse {
  config_dir: string;
  channels: ChannelStatus[];
}

export interface ChannelActionResponse {
  status: string;
  message: string;
  channel: ChannelStatus;
}

export interface ChannelLogsResponse {
  channel: ChannelId;
  log_path: string;
  text: string;
  lines: number;
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

export interface AgentSummary {
  name: string;
  title: string;
  path: string;
  active: boolean;
  selected: boolean;
  initialized: boolean;
  channel_running: boolean;
}

export interface AgentsResponse {
  active_agent: string;
  selected_agent: string;
  agents: AgentSummary[];
}

export interface AgentNameAvailability {
  name: string;
  registered: boolean;
  directory_exists: boolean;
  path: string;
}

export interface SetupOption {
  id: string;
  label?: string;
  description?: string;
}

export interface AgentSetupSchema {
  providers: SetupOption[];
  models: Record<string, string[]>;
  provider_base_urls: Record<string, string>;
  custom_model_apis: string[];
  search_providers: SetupOption[];
  image_generation_providers: SetupOption[];
  voice_providers: SetupOption[];
  voice_custom_providers: string[];
  defaults: {
    identity: string;
    wake_phrases: string[];
    exit_phrases: string[];
  };
  placeholders: Record<string, string>;
  name_pattern: string;
}

export interface InitSelectionInput {
  provider: string;
  base_url: string;
  api_key: string;
  model: string;
  identity: string;
  model_api: string;
  supports_vision: boolean;
  search_provider: string;
  search_api_key: string;
  image_generation_provider: string;
  image_generation_api_key: string;
  observability_enabled: boolean;
  langfuse_public_key: string;
  langfuse_secret_key: string;
  langfuse_base_url: string;
  voice_enabled: boolean;
  voice_provider: string;
  voice_api_key: string;
  voice_stt_provider: string;
  voice_stt_api_key: string;
  voice_tts_provider: string;
  voice_tts_api_key: string;
  voice_enable_interruptions: boolean;
  voice_wake_enabled: boolean;
  voice_wake_phrases: string[];
  voice_exit_phrases: string[];
}

export interface CreateAgentInput {
  name: string;
  title?: string;
  replace_existing: boolean;
  selection: InitSelectionInput;
}

export interface AgentIdentity {
  identity: string;
  path: string;
  filename: string;
  modified: number;
}

export interface AgentConfig {
  config: string;
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
  recipient_id?: string;
  timestamp?: number;
  metadata?: Record<string, unknown>;
  images?: ImageAsset[];
  image_count?: number;
  attachments?: AttachmentAsset[];
  attachment_count?: number;
  channel?: string;
  room_name?: string;
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
