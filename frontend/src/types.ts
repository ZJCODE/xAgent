export type RoutePath = "/" | "/memory" | "/message" | "/workspace" | "/skills" | "/tasks" | "/channels" | "/devices" | "/voice" | "/agent";

export interface DeviceProfileInput { name: string; location: string; description?: string }
export interface DeviceInfo {
  device_id: string;
  online: boolean;
  voice_online?: boolean;
  name?: string;
  location?: string;
  description?: string;
  reported_name?: string;
  reported_location?: string;
  capabilities?: { name: string; description?: string }[];
}
export interface DeviceStatus {
  runtime_available: boolean;
  api_available: boolean;
  api_url: string;
  devices: DeviceInfo[];
}
export interface DevicePairing { pairing_code: string; expires_in: number; expires_at: string }

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
  generate_image?: boolean;
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
  error_code?: string;
  error_id?: string;
  status_code?: number;
  task?: ScheduledTaskItem;
  turn_id?: string;
  event_id?: string;
  request_id?: string;
  steered?: boolean;
}

export interface ScheduledTaskRecurrenceRule {
  kind: "daily" | "weekly" | "monthly" | "interval" | string;
  time?: string;
  weekdays?: string[];
  day?: number;
  weekday?: string;
  nth?: number;
  every_seconds?: number;
  start_at?: string;
  end_at?: string;
}

export interface TaskExecutionSettings {
  max_lateness_seconds?: number;
  block_on_uncertain?: boolean;
}

export interface TaskRun {
  id: string;
  task_id: string;
  due_at: string;
  status: string;
  phase: string;
  error?: string;
  error_type?: string;
  failure_stage?: string;
  retry_at?: string;
  started_at?: string;
  finished_at?: string;
  resolved_at?: string;
  attempt_count?: number;
  skip_count?: number;
  unsafe_tools_started?: boolean;
  tools?: Array<{ name: string; safety: string; started_at: string }>;
  can_retry_prepare?: boolean;
  can_retry_deliver?: boolean;
  can_resolve?: boolean;
  attempts?: Array<{ id: string; status: string; started_at?: string; error?: string }>;
  deliveries?: Array<{ id: string; status: string; description?: string; error?: string }>;
}

export interface ScheduledTaskItem {
  timezone?: string;
  execution?: TaskExecutionSettings;
  task_id: string;
  title: string;
  task_type: "message" | "agent" | string;
  content: string;
  next_run_at: string | null;
  next_retry_at?: string | null;
  blocked_on_review?: boolean;
  recurrence?: ScheduledTaskRecurrenceRule[] | null;
  status: "active" | "paused" | "completed" | "failed" | "needs_review" | string;
  reason?: string;
  channel?: string;
  user_id?: string;
  target?: Record<string, unknown>;
  paused_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  failed_at?: string | null;
  last_run_at?: string | null;
  last_run_status?: "succeeded" | "failed" | string | null;
  completion_reason?: string | null;
  last_error?: string | null;
  run_stage?: string | null;
  failure_stage?: string | null;
  scheduled_run_at?: string | null;
  run_id?: string | null;
  attempt_id?: string | null;
  unresolved_runs?: string[];
  last_review_error?: string | null;
  state?: "pending" | "running" | "failed" | "completed" | string;
}

export interface TaskCreateInput {
  timezone?: string;
  execution?: TaskExecutionSettings;
  task_type: "message" | "agent" | string;
  content: string;
  title?: string;
  run_at?: string;
  delay_seconds?: number;
  recurrence?: ScheduledTaskRecurrenceRule[];
  interval_seconds?: number;
  duration_seconds?: number;
  start_at?: string;
  end_at?: string;
  channel?: string;
  user_id?: string;
  target?: Record<string, unknown>;
}

export interface TaskUpdateInput {
  timezone?: string;
  execution?: TaskExecutionSettings;
  title?: string;
  content?: string;
  task_type?: string;
  run_at?: string;
  delay_seconds?: number;
  recurrence?: ScheduledTaskRecurrenceRule[];
  interval_seconds?: number;
  duration_seconds?: number;
  start_at?: string;
  end_at?: string;
}

export type TaskDuplicateInput = Omit<TaskCreateInput, "channel" | "user_id" | "target">;

export type TaskScope = "current" | "scheduled" | "attention" | "archive";

export interface TasksResponse {
  root: string;
  tasks: ScheduledTaskItem[];
  total: number;
  counts: {
    scheduled: number;
    attention: number;
    archive: number;
  };
  limit: number;
  offset: number;
  has_more: boolean;
}

export type ChannelId = "api" | "voice" | "weixin";

export type ChannelRuntimeStatus = "running" | "connected" | "starting" | "connecting" | "reconnecting" | "disconnecting" | "disconnected" | "stopped" | "disabled" | "error" | "failed";

export interface AgentRuntimeStatus {
  status: string;
  state?: string;
  runtime_running: boolean;
  runtime_ready?: boolean;
  needs_restart?: boolean;
  error?: string;
  restart_warnings?: string[];
  channels: Record<string, { status?: string; state?: string; error?: string }>;
  queue?: { pending: number; active_turn_id?: string | null };
  memory?: JournalRuntimeStatus;
  journal?: JournalRuntimeStatus;
  tasks?: { needs_review?: number | Array<{ task_id?: string; reason?: string }>; failed?: number };
}

export interface JournalRuntimeStatus {
  backlog: number;
  needs_review?: Array<{ reason?: string }>;
  last_maintenance?: { status?: string; finished_at?: string | null; error?: string };
}

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
  can_connect: boolean;
  can_disconnect: boolean;
  setup_hint: string;
}

export interface ChannelsResponse {
  config_dir: string;
  channels: ChannelStatus[];
  runtime: AgentRuntimeStatus;
}

export interface ChannelActionResponse {
  channel: ChannelStatus;
  runtime: AgentRuntimeStatus;
}

export interface ChannelLogsResponse {
  channel: ChannelId;
  log_path: string;
  text: string;
  lines: number;
}

export type SetupChannelId = Extract<ChannelId, "weixin">;

export interface VoicePreferences {
  voice: string;
  speed: number;
  interruptions: boolean;
  audio_input: string | number | null;
  audio_output: string | number | null;
}

export interface AgentVoiceSettings extends VoicePreferences {
  configured: boolean;
  has_api_key: boolean;
  voice_options: SetupOption[];
  restart_required: boolean;
}

export interface VoiceAudioDevices {
  input: SetupOption[];
  output: SetupOption[];
  error?: string;
}

export interface WeixinSetupSchema {
  defaults: {
    base_url: string;
    cdn_base_url: string;
    owner_only: boolean;
    allow_users: string[];
    media_enabled: boolean;
  };
  configured: boolean;
  can_force: boolean;
}

export type ChannelSetupSchema = WeixinSetupSchema;

export interface ChannelSetupInput {
  force: boolean;
  selection: Record<string, unknown>;
}

export interface ChannelSetupResponse {
  status: string;
  setup: {
    channel: string;
    config_path: string;
    configured: boolean;
  };
  channel: ChannelStatus;
}

export interface QrSessionResponse {
  session_id: string;
  channel: string;
  status: string;
  qr_url?: string | null;
  expire_in?: number | null;
  result?: Record<string, unknown> | null;
  error?: string | null;
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
  tools_source?: "configured" | "runtime";
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
  api_url: string;
  active: boolean;
  selected: boolean;
  initialized: boolean;
  channel_running: boolean;
  runtime_running?: boolean;
  voice_configured?: boolean;
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

export interface ReasoningConfigInput {
  enabled: boolean;
  effort?: string;
  budget_tokens?: number;
}

export interface ReasoningCapability {
  supported: boolean;
  controls: Array<"effort" | "budget_tokens">;
  effort_values: string[];
  min_budget_tokens?: number;
}

export interface AgentSetupSchema {
  providers: SetupOption[];
  models: Record<string, string[]>;
  default_models: Record<string, string>;
  provider_base_urls: Record<string, string>;
  custom_model_apis: string[];
  reasoning: {
    providers: Record<string, ReasoningCapability>;
    custom_model_apis: Record<string, ReasoningCapability>;
  };
  defaults: {
    identity: string;
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
  reasoning?: ReasoningConfigInput | null;
}

export interface CreateAgentInput {
  name: string;
  title?: string;
  replace_existing: boolean;
  selection: InitSelectionInput;
}

export type AgentEditSetupFeatureId = "model" | "search" | "image_generation" | "observability";

export interface AgentEditSetupFeature {
  id: AgentEditSetupFeatureId;
  kind: "agent";
  label: string;
  description: string;
  status: string;
  disabled: boolean;
  disabled_reason?: string;
}

export interface AgentEditSetupSchema {
  features: AgentEditSetupFeature[];
  model_provider: string;
  model: {
    providers: SetupOption[];
    models: Record<string, string[]>;
    default_models: Record<string, string>;
    provider_base_urls: Record<string, string>;
    custom_model_apis: string[];
    reasoning: {
      providers: Record<string, ReasoningCapability>;
      custom_model_apis: Record<string, ReasoningCapability>;
    };
    current: {
      provider: string;
      model: string;
      base_url: string;
      model_api: string;
      supports_vision: boolean;
      has_api_key: boolean;
      reasoning?: ReasoningConfigInput | null;
    };
    placeholders: Record<string, string>;
  };
  search: {
    providers: SetupOption[];
    current: { provider: string; has_api_key: boolean };
    placeholders: Record<string, string>;
  };
  image_generation: {
    providers: SetupOption[];
    current: { provider: string; has_api_key: boolean };
    placeholders: Record<string, string>;
  };
  observability: {
    available: boolean;
    current: {
      enabled: boolean;
      has_public_key: boolean;
      has_secret_key: boolean;
      base_url: string;
    };
    placeholders: Record<string, string>;
  };
}

export interface AgentEditSetupChange {
  path: string;
  before: string;
  after: string;
}

export interface AgentEditSetupResponse {
  status: string;
  feature: string;
  restart_required: boolean;
  restart_channel?: ChannelId;
  changed: boolean;
  changes: AgentEditSetupChange[];
}

export interface AgentIdentity {
  identity: string;
  path: string;
  filename: string;
  modified: number;
  needs_restart?: boolean;
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
  revision?: string;
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
  sender_name?: string | null;
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
  line?: number;
  column?: number;
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

export interface SkillWriteInput {
  path: string;
  content: string;
  create_parents?: boolean;
  expected_revision?: string;
}

export interface SkillEntryCreateInput {
  parent_path: string;
  name: string;
  kind: "file" | "directory";
  content?: string;
}

export interface SkillEntryMoveInput {
  path: string;
  new_parent_path: string;
  new_name: string;
  expected_revision?: string;
}

export interface SkillFileMutationResponse {
  status: string;
  entry: FileNode;
}

export interface SkillApiErrorDetail {
  code?: string;
  message?: string;
  issues?: SkillValidationIssue[];
  current?: FileReadResult | null;
}
