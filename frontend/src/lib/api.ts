import type {
  AgentNameAvailability,
  AgentSetupSchema,
  AgentsResponse,
  ChannelActionResponse,
  ChannelId,
  ChannelLogsResponse,
  ChannelSetupInput,
  ChannelSetupResponse,
  ChannelSetupSchema,
  ChannelsResponse,
  CreateAgentInput,
  MemoryFile,
  MemoryIndexResponse,
  MessagesResponse,
  OverviewResponse,
  QrSessionResponse,
  RuntimeDelivery,
  RuntimeActionResponse,
  RuntimeStatus,
  RuntimeTask,
  SettingsDocument,
  SetupChannelId,
  TaskCreateInput,
  TaskUpdateInput,
} from "../types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail : detail?.message || `HTTP ${response.status}`;
    throw new ApiError(response.status, message);
  }
  return data as T;
}

const jsonRequest = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: body === undefined ? undefined : { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export function getWebHealth(): Promise<{ status: string; web: boolean; runtime_running: boolean }> {
  return requestJson("/api/health", { signal: AbortSignal.timeout(5000) });
}

export function getRuntime(): Promise<RuntimeStatus> {
  return requestJson("/api/runtime");
}

export function getOverview(): Promise<OverviewResponse> {
  return requestJson("/api/overview");
}

export function startRuntime(): Promise<RuntimeActionResponse> {
  return requestJson("/api/runtime/start", { method: "POST" });
}

export function stopRuntime(): Promise<RuntimeActionResponse> {
  return requestJson("/api/runtime/stop", { method: "POST" });
}

export function restartRuntime(): Promise<RuntimeActionResponse> {
  return requestJson("/api/runtime/restart", { method: "POST" });
}

export function getSettings(): Promise<SettingsDocument> {
  return requestJson("/api/settings");
}

export function updateSettings(settings: SettingsDocument["settings"]): Promise<SettingsDocument> {
  return requestJson("/api/settings", jsonRequest("PUT", { settings }));
}

export function getMessages({
  limit = 50,
  offset = 0,
  query = "",
  role = "",
  source = "",
}: {
  limit?: number;
  offset?: number;
  query?: string;
  role?: string;
  source?: string;
} = {}): Promise<MessagesResponse> {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    q: query,
    role,
    source,
  });
  return requestJson(`/api/messages?${params.toString()}`);
}

export function getMemory({
  scope = "all",
  query = "",
  limit = 200,
}: {
  scope?: string;
  query?: string;
  limit?: number;
} = {}): Promise<MemoryIndexResponse> {
  const params = new URLSearchParams({
    scope,
    q: query,
    limit: String(limit),
  });
  return requestJson(`/api/memory?${params.toString()}`);
}

export function getMemoryFile(path: string): Promise<MemoryFile> {
  return requestJson(`/api/memory/file?path=${encodeURIComponent(path)}`);
}

export function getAgents(): Promise<AgentsResponse> {
  return requestJson("/api/agents");
}

export function selectAgent(name: string): Promise<AgentsResponse> {
  return requestJson("/api/agents/select", jsonRequest("POST", { name }));
}

export function getAgentSetupSchema(): Promise<AgentSetupSchema> {
  return requestJson("/api/agents/setup-schema");
}

export function getAgentNameAvailability(name: string): Promise<AgentNameAvailability> {
  return requestJson(`/api/agents/availability?name=${encodeURIComponent(name)}`);
}

export function createAgent(input: CreateAgentInput): Promise<AgentsResponse> {
  return requestJson("/api/agents", jsonRequest("POST", input));
}

export function deleteAgent(name: string, confirm: string): Promise<AgentsResponse> {
  return requestJson(`/api/agents/${encodeURIComponent(name)}`, jsonRequest("DELETE", { confirm }));
}

export function getTasks(status?: string): Promise<{ tasks: RuntimeTask[] }> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return requestJson(`/api/tasks${query}`);
}

export function createTask(input: TaskCreateInput): Promise<{ task: RuntimeTask }> {
  return requestJson("/api/tasks", jsonRequest("POST", input));
}

export function updateTask(taskId: string, input: TaskUpdateInput): Promise<{ task: RuntimeTask }> {
  return requestJson(`/api/tasks/${encodeURIComponent(taskId)}`, jsonRequest("PATCH", input));
}

export function pauseTask(taskId: string): Promise<{ task: RuntimeTask }> {
  return requestJson(`/api/tasks/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
}

export function resumeTask(taskId: string): Promise<{ task: RuntimeTask }> {
  return requestJson(`/api/tasks/${encodeURIComponent(taskId)}/resume`, { method: "POST" });
}

export function deleteTask(taskId: string): Promise<{ task: RuntimeTask }> {
  return requestJson(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

export function getChannels(): Promise<ChannelsResponse> {
  return requestJson("/api/channels");
}

export function startChannel(channel: ChannelId): Promise<ChannelActionResponse> {
  return requestJson(`/api/channels/${channel}/start`, { method: "POST" });
}

export function stopChannel(channel: ChannelId): Promise<ChannelActionResponse> {
  return requestJson(`/api/channels/${channel}/stop`, { method: "POST" });
}

export function restartChannel(channel: ChannelId): Promise<ChannelActionResponse> {
  return requestJson(`/api/channels/${channel}/restart`, { method: "POST" });
}

export function getChannelLogs(channel: ChannelId, lines = 80): Promise<ChannelLogsResponse> {
  return requestJson(`/api/channels/${channel}/logs?lines=${lines}`);
}

export function getChannelSetupSchema(channel: SetupChannelId): Promise<ChannelSetupSchema> {
  return requestJson(`/api/channels/${channel}/setup-schema`);
}

export function setupChannel(channel: SetupChannelId, input: ChannelSetupInput): Promise<ChannelSetupResponse> {
  return requestJson(`/api/channels/${channel}/setup`, jsonRequest("POST", input));
}

export function startChannelQr(channel: Extract<ChannelId, "feishu" | "weixin">): Promise<QrSessionResponse> {
  return requestJson(`/api/channels/${channel}/qr/start`, { method: "POST" });
}

export function pollChannelQr(
  channel: Extract<ChannelId, "feishu" | "weixin">,
  sessionId: string,
): Promise<QrSessionResponse> {
  return requestJson(`/api/channels/${channel}/qr/${encodeURIComponent(sessionId)}`);
}

export function cancelChannelQr(
  channel: Extract<ChannelId, "feishu" | "weixin">,
  sessionId: string,
): Promise<{ status: string; session_id: string }> {
  return requestJson(`/api/channels/${channel}/qr/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export function getDeliveries(status?: string): Promise<{ deliveries: RuntimeDelivery[] }> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return requestJson(`/api/deliveries${query}`);
}

export function retryDelivery(deliveryId: string): Promise<{ delivery: RuntimeDelivery }> {
  return requestJson(`/api/deliveries/${encodeURIComponent(deliveryId)}/retry`, { method: "POST" });
}
