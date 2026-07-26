export type RoutePath =
  | "/"
  | "/chat"
  | "/messages"
  | "/memory"
  | "/tasks"
  | "/channels"
  | "/deliveries"
  | "/settings";

export type ChatRole = "user" | "assistant" | "observation";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  source?: string;
  pending?: boolean;
  error?: boolean;
}

export interface ChatPanelState {
  id: "single";
  messages: ChatMessage[];
  sending: boolean;
}

export interface ChatEvent {
  type?: string;
  delta?: string;
  content?: string;
  message?: unknown;
  message_id?: string;
  phase?: string;
  error?: string;
}

export type TaskStatus = "active" | "paused" | "running" | "completed" | "failed";
export type TaskSchedule =
  | { kind: "once"; run_at: string }
  | { kind: "daily"; local_time: string }
  | { kind: "weekly"; weekday: number; local_time: string }
  | {
      kind: "interval";
      interval_seconds: number;
      duration_seconds?: number;
      end_at?: string;
      end_timestamp?: number;
    };

export interface RuntimeTask {
  task_id: string;
  instruction: string;
  schedule: TaskSchedule;
  destination: TaskDestination | null;
  created_source: string;
  created_by: string;
  status: TaskStatus;
  next_run_at: number | null;
  created_at: number;
  updated_at: number;
  error: string;
}

export interface TaskCreateInput {
  instruction: string;
  schedule: TaskSchedule;
  destination: TaskDestination | null;
}

export interface TaskUpdateInput {
  instruction?: string;
  schedule?: TaskSchedule;
  destination?: TaskDestination | null;
}

export type ChannelId = "api" | "voice" | "feishu" | "weixin";
export interface TaskDestination {
  channel: ChannelId;
  target: Record<string, unknown>;
}
export type ChannelRuntimeStatus =
  | "runtime-stopped"
  | "stopped"
  | "starting"
  | "running"
  | "stopping"
  | "degraded"
  | "disabled"
  | "error";

export interface ChannelStatus {
  id: ChannelId;
  label: string;
  status: ChannelRuntimeStatus;
  configured: boolean;
  ready: boolean;
  enabled: boolean;
  pid: number | null;
  detail: string;
  log_path: string;
  can_start: boolean;
  can_stop: boolean;
  can_restart: boolean;
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

export type SetupChannelId = Extract<ChannelId, "voice" | "feishu" | "weixin">;

export interface SetupOption {
  id: string;
  label?: string;
  description?: string;
}

export interface VoiceSelectionInput {
  voice_enabled?: boolean;
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

export interface VoiceSetupSchema {
  voice_providers: SetupOption[];
  voice_custom_providers: string[];
  defaults: {
    voice_provider: string;
    voice_stt_provider: string;
    voice_tts_provider: string;
    wake_phrases: string[];
    exit_phrases: string[];
    voice_wake_enabled: boolean;
    voice_enable_interruptions: boolean;
  };
  placeholders: Record<string, string>;
  inherit_api_key_from: { provider: string; can_inherit_qwen_key: boolean };
  configured: boolean;
  can_force: boolean;
}

export interface FeishuSetupSchema {
  credential_modes: SetupOption[];
  defaults: {
    credential_mode: string;
    stream: boolean;
    group_fetch_limit: number;
    group_reply_only_when_mentioned: boolean;
  };
  configured: boolean;
  can_force: boolean;
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

export type ChannelSetupSchema = VoiceSetupSchema | FeishuSetupSchema | WeixinSetupSchema;
export interface ChannelSetupInput {
  force: boolean;
  selection: Record<string, unknown>;
}
export interface ChannelSetupResponse {
  status: string;
  setup: { channel: string; config_path: string; configured: boolean };
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

export interface AgentSummary {
  name: string;
  title: string;
  path: string;
  active: boolean;
  selected: boolean;
  initialized: boolean;
  runtime_running: boolean;
  pid: number | null;
  provider: string;
  model: string;
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
  provider_base_urls: Record<string, string>;
  custom_model_apis: string[];
  reasoning: {
    providers: Record<string, ReasoningCapability>;
    custom_model_apis: Record<string, ReasoningCapability>;
  };
  search_providers: SetupOption[];
  image_generation_providers: SetupOption[];
  voice_providers: SetupOption[];
  voice_custom_providers: string[];
  defaults: { identity: string; wake_phrases: string[]; exit_phrases: string[] };
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

export type DeliveryStatus = "pending" | "sending" | "delivered" | "blocked" | "failed" | "unknown";
export interface RuntimeDelivery {
  delivery_id: string;
  event_id: string;
  channel: string;
  target: Record<string, unknown>;
  payload: Record<string, unknown>;
  status: DeliveryStatus;
  attempts: number;
  channel_message_id: string;
  error: string;
  created_at: number;
  updated_at: number;
}

export interface RuntimeChannel {
  name: string;
  state: string;
  enabled: boolean;
  error: string;
}

export interface RuntimeStatus {
  pid: number | null;
  instance_id: string;
  started_at: number | null;
  uptime_seconds?: number;
  running: boolean;
  channels: RuntimeChannel[];
}

export interface RuntimeActionResponse {
  action: "start" | "stop" | "restart";
  outcome: string;
  runtime: RuntimeStatus;
}

export interface XAgentConfig {
  schema_version: 2;
  provider: {
    name: "openai" | "deepseek" | "minimax" | "qwen" | "anthropic" | "custom";
    model: string;
    api_key: string;
    base_url: string;
    model_api?: "openai_responses" | "openai_chat_completions" | "anthropic_messages";
    max_tokens?: number;
    reasoning?: { enabled: boolean; effort?: string; budget_tokens?: number };
    supports_vision?: boolean;
    [key: string]: unknown;
  };
  agent: {
    max_history: number;
    max_iter: number;
    subconscious_activity: number;
    memory_recent_days: number;
    [key: string]: unknown;
  };
  tools: {
    shell: { enabled: boolean; [key: string]: unknown };
    [key: string]: unknown;
  };
  channels: Record<string, Record<string, unknown>>;
  runtime: {
    heartbeat_enabled: boolean;
    heartbeat_interval_seconds: number;
    turn_timeout_seconds: number;
    tool_timeout_seconds: number;
    [key: string]: unknown;
  };
  search: Record<string, unknown>;
  image_generation: Record<string, unknown>;
  observability?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface SettingsDocument {
  settings: XAgentConfig;
  schema: Record<string, unknown>;
  secret_sentinel: string;
}

export interface OverviewCounts {
  messages: number;
  people: number;
  events: Record<string, number>;
  deliveries: Record<string, number>;
  tasks: Record<string, number>;
  memory_files: number;
}

export interface RuntimeEventSummary {
  sequence: number;
  event_id: string;
  kind: string;
  source: string;
  speaker_id: string;
  content: string;
  timestamp: number;
  status: string;
  error: string;
}

export interface MemoryEntry {
  path: string;
  scope: "daily" | "weekly" | "monthly" | "yearly" | string;
  title: string;
  excerpt: string;
  modified_at: number;
  size_bytes: number;
}

export interface MemoryIndexResponse {
  entries: MemoryEntry[];
  total: number;
  scope: string;
  query: string;
}

export interface MemoryFile {
  path: string;
  title: string;
  content: string;
  modified_at: number;
  size_bytes: number;
}

export interface OverviewResponse {
  runtime: RuntimeStatus;
  counts: OverviewCounts | null;
  recent_events: RuntimeEventSummary[];
  recent_memory: MemoryEntry[];
}

export interface PersistedMessage {
  id: number;
  type: "message" | "context_event" | string;
  role: "user" | "assistant" | "environment" | string;
  sender_id?: string | null;
  recipient_id?: string | null;
  source?: string | null;
  room_name?: string | null;
  content: string;
  timestamp: number;
  images?: Array<{ format: string; source?: string | null }> | null;
  metadata?: Record<string, unknown>;
}

export interface MessagesResponse {
  messages: PersistedMessage[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
}
