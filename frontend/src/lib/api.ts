import type {
  AgentConfig,
  AgentIdentity,
  AgentInfo,
  AgentNameAvailability,
  AgentSetupSchema,
  AgentsResponse,
  CreateAgentInput,
  ChannelActionResponse,
  ChannelId,
  ChannelLogsResponse,
  ChannelSetupInput,
  ChannelSetupResponse,
  ChannelSetupSchema,
  ChannelsResponse,
  FeishuSetupSchema,
  QrSessionResponse,
  SetupChannelId,
  VoiceSetupSchema,
  WeixinSetupSchema,
  FileReadResult,
  FileNode,
  MessageSearchResponse,
  MessagesResponse,
  MessagesStats,
  SearchResult,
  SkillCreateInput,
  SkillCreateResponse,
  SkillEntryCreateInput,
  SkillEntryMoveInput,
  SkillFileMutationResponse,
  SkillApiErrorDetail,
  SkillWriteInput,
  SkillStateResponse,
  SkillsInfo,
  SkillsTreeResponse,
  TaskCreateInput,
  TaskDuplicateInput,
  TaskScope,
  TaskUpdateInput,
  TasksResponse,
  ScheduledTaskItem,
  BackgroundJobItem,
  JobCreateInput,
  JobScope,
  JobsResponse,
  WorkspaceUploadResult,
} from "../types";

export class ApiError extends Error {
  status: number;
  code?: string;
  detail?: SkillApiErrorDetail;

  constructor(status: number, message: string, detail?: SkillApiErrorDetail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = detail?.code;
    this.detail = detail;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const rawDetail = data?.detail;
    const detail = rawDetail && typeof rawDetail === "object" ? rawDetail as SkillApiErrorDetail : undefined;
    const message = typeof rawDetail === "string"
      ? rawDetail
      : detail?.message || `HTTP ${response.status}`;
    throw new ApiError(response.status, message, detail);
  }
  return data as T;
}

export async function getHealth(): Promise<{ status: string; service: string }> {
  return requestJson("/health", { signal: AbortSignal.timeout(5000) });
}

export async function getWebHealth(): Promise<{ status: string; web: boolean; api_reachable: boolean }> {
  return requestJson("/api/health", { signal: AbortSignal.timeout(5000) });
}

export async function getAgents(): Promise<AgentsResponse> {
  return requestJson("/api/agents");
}

export async function selectAgent(name: string): Promise<AgentsResponse> {
  return requestJson("/api/agents/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function getAgentSetupSchema(): Promise<AgentSetupSchema> {
  return requestJson("/api/agents/setup-schema");
}

export async function getAgentNameAvailability(name: string): Promise<AgentNameAvailability> {
  return requestJson(`/api/agents/availability?name=${encodeURIComponent(name)}`);
}

export async function createAgent(input: CreateAgentInput): Promise<AgentsResponse> {
  return requestJson("/api/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function deleteAgent(name: string, confirm: string): Promise<AgentsResponse> {
  return requestJson(`/api/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm }),
  });
}

export async function getAgentInfo(): Promise<AgentInfo> {
  return requestJson("/api/agent/info");
}

export async function getAgentIdentity(): Promise<AgentIdentity> {
  return requestJson("/api/agent/identity");
}

export async function updateAgentIdentity(identity: string): Promise<AgentIdentity> {
  return requestJson("/api/agent/identity", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identity }),
  });
}

export async function getAgentConfig(): Promise<AgentConfig> {
  return requestJson("/api/agent/config");
}

export async function updateAgentConfig(config: string): Promise<AgentConfig> {
  return requestJson("/api/agent/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
}

export async function clearMemory(): Promise<{ status: string }> {
  return requestJson("/api/memory/clear", { method: "POST" });
}

export async function clearMessages(): Promise<{ status: string; message: string }> {
  return requestJson("/clear_messages", { method: "POST" });
}

export async function clearWorkspace(): Promise<{ status: string; message: string; deleted: number }> {
  return requestJson("/api/workspace/clear", { method: "POST" });
}

export async function getMemoryTree(): Promise<{ tree: FileNode[] }> {
  return requestJson("/api/memory/tree");
}

export async function readMemoryFile(path: string): Promise<FileReadResult> {
  return requestJson(`/api/memory/read?path=${encodeURIComponent(path)}`);
}

export async function searchMemory(query: string): Promise<{ query: string; results: SearchResult[] }> {
  return requestJson(`/api/memory/search?query=${encodeURIComponent(query)}`);
}

export async function getWorkspaceTree(): Promise<{ root: string; tree: FileNode[] }> {
  return requestJson("/api/workspace/tree");
}

export async function readWorkspaceFile(path: string): Promise<FileReadResult> {
  return requestJson(`/api/workspace/read?path=${encodeURIComponent(path)}`);
}

export async function writeWorkspaceFile(
  path: string,
  content: string,
  createParents = true,
): Promise<{ status: string } & FileNode> {
  return requestJson("/api/workspace/write", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content, create_parents: createParents }),
  });
}

export async function deleteWorkspaceFile(
  path: string,
  recursive = false,
): Promise<{ status: string; deleted: FileNode }> {
  const params = new URLSearchParams({
    path,
    recursive: recursive ? "true" : "false",
  });
  return requestJson(`/api/workspace/delete?${params.toString()}`, { method: "DELETE" });
}

export async function searchWorkspace(query: string): Promise<{ query: string; results: SearchResult[] }> {
  return requestJson(`/api/workspace/search?query=${encodeURIComponent(query)}`);
}

export async function uploadWorkspaceFile(file: File, path?: string): Promise<WorkspaceUploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  if (path) formData.append("path", path);
  return requestJson("/api/workspace/upload", {
    method: "POST",
    body: formData,
  });
}

export async function getSkillsInfo(): Promise<SkillsInfo> {
  return requestJson("/api/skills/info");
}

export async function getSkillsTree(): Promise<SkillsTreeResponse> {
  return requestJson("/api/skills/tree");
}

export async function readSkillFile(path: string): Promise<FileReadResult> {
  return requestJson(`/api/skills/read?path=${encodeURIComponent(path)}`);
}

export async function searchSkills(query: string): Promise<{ query: string; results: SearchResult[] }> {
  return requestJson(`/api/skills/search?query=${encodeURIComponent(query)}`);
}

export async function createSkill(input: SkillCreateInput): Promise<SkillCreateResponse> {
  return requestJson("/api/skills/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function writeSkillFile(input: SkillWriteInput): Promise<FileReadResult & { status: string }> {
  return requestJson("/api/skills/write", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function createSkillEntry(input: SkillEntryCreateInput): Promise<SkillFileMutationResponse> {
  return requestJson("/api/skills/entries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function moveSkillEntry(input: SkillEntryMoveInput): Promise<SkillFileMutationResponse> {
  return requestJson("/api/skills/entries", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function deleteSkillEntry(
  path: string,
  recursive = false,
  expectedRevision?: string,
): Promise<{ status: string; deleted: FileNode }> {
  const params = new URLSearchParams({ path, recursive: recursive ? "true" : "false" });
  if (expectedRevision) params.set("expected_revision", expectedRevision);
  return requestJson(`/api/skills/entries?${params.toString()}`, { method: "DELETE" });
}

export async function updateSkillState(name: string, enabled: boolean): Promise<SkillStateResponse> {
  return requestJson("/api/skills/state", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, enabled }),
  });
}

export async function deleteSkillPath(path: string, recursive = false): Promise<{ status: string; deleted: FileNode }> {
  return requestJson(`/api/skills/delete?path=${encodeURIComponent(path)}&recursive=${recursive ? "true" : "false"}`, {
    method: "DELETE",
  });
}

export async function getMessages(count: number, offset: number): Promise<MessagesResponse> {
  return requestJson(`/api/messages?count=${count}&offset=${offset}`);
}

export async function searchMessages(query: string): Promise<MessageSearchResponse> {
  return requestJson(`/api/messages/search?query=${encodeURIComponent(query)}`);
}

export async function getMessagesStats(): Promise<MessagesStats> {
  return requestJson("/api/messages/stats");
}

export async function getTasks(
  scope: TaskScope = "current",
  query = "",
  limit = 50,
  offset = 0,
): Promise<TasksResponse> {
  const params = new URLSearchParams({ scope, query, limit: String(limit), offset: String(offset) });
  return requestJson(`/api/tasks?${params.toString()}`);
}

export async function createTask(input: TaskCreateInput): Promise<{ status: string; task: ScheduledTaskItem }> {
  return requestJson("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function pauseTask(taskId: string): Promise<{ status: string; task: ScheduledTaskItem }> {
  return requestJson(`/api/tasks/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
}

export async function resumeTask(taskId: string): Promise<{ status: string; task: ScheduledTaskItem }> {
  return requestJson(`/api/tasks/${encodeURIComponent(taskId)}/resume`, { method: "POST" });
}

export async function updateTask(taskId: string, input: TaskUpdateInput): Promise<{ status: string; task: ScheduledTaskItem }> {
  return requestJson(`/api/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function duplicateTask(taskId: string, input: TaskDuplicateInput): Promise<{ status: string; task: ScheduledTaskItem }> {
  return requestJson(`/api/tasks/${encodeURIComponent(taskId)}/duplicate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function deleteTask(taskId: string): Promise<{ status: string; deleted: unknown }> {
  return requestJson(`/api/tasks/delete?task_id=${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

export async function getJobs(
  scope: JobScope = "current",
  query = "",
  limit = 50,
  offset = 0,
): Promise<JobsResponse> {
  const params = new URLSearchParams({ scope, query, limit: String(limit), offset: String(offset) });
  return requestJson(`/api/jobs?${params.toString()}`);
}

export async function createJob(input: JobCreateInput): Promise<{ status: string; job: BackgroundJobItem }> {
  return requestJson("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function getJob(jobId: string): Promise<{ status: string; job: BackgroundJobItem }> {
  return requestJson(`/api/jobs/${encodeURIComponent(jobId)}`);
}

export async function cancelJob(jobId: string): Promise<{ status: string; job: BackgroundJobItem }> {
  return requestJson(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
}

export async function deleteJob(jobId: string): Promise<{ status: string; deleted: unknown }> {
  return requestJson(`/api/jobs/delete?job_id=${encodeURIComponent(jobId)}`, { method: "DELETE" });
}

export async function getChannels(): Promise<ChannelsResponse> {
  return requestJson("/api/channels");
}

export async function startChannel(channel: ChannelId): Promise<ChannelActionResponse> {
  return requestJson(`/api/channels/${channel}/start`, { method: "POST" });
}

export async function stopChannel(channel: ChannelId): Promise<ChannelActionResponse> {
  return requestJson(`/api/channels/${channel}/stop`, { method: "POST" });
}

export async function restartChannel(channel: ChannelId): Promise<ChannelActionResponse> {
  return requestJson(`/api/channels/${channel}/restart`, { method: "POST" });
}

export async function getChannelLogs(channel: ChannelId, lines = 80): Promise<ChannelLogsResponse> {
  return requestJson(`/api/channels/${channel}/logs?lines=${lines}`);
}

export async function getChannelSetupSchema(channel: SetupChannelId): Promise<ChannelSetupSchema> {
  return requestJson(`/api/channels/${channel}/setup-schema`);
}

export async function setupChannel(
  channel: SetupChannelId,
  input: ChannelSetupInput,
): Promise<ChannelSetupResponse> {
  return requestJson(`/api/channels/${channel}/setup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function startChannelQr(channel: Extract<ChannelId, "feishu" | "weixin">): Promise<QrSessionResponse> {
  return requestJson(`/api/channels/${channel}/qr/start`, { method: "POST" });
}

export async function pollChannelQr(
  channel: Extract<ChannelId, "feishu" | "weixin">,
  sessionId: string,
): Promise<QrSessionResponse> {
  return requestJson(`/api/channels/${channel}/qr/${encodeURIComponent(sessionId)}`);
}

export async function cancelChannelQr(
  channel: Extract<ChannelId, "feishu" | "weixin">,
  sessionId: string,
): Promise<{ status: string; session_id: string }> {
  return requestJson(`/api/channels/${channel}/qr/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

export function workspaceBlobUrl(path: string): string {
  return `/api/workspace/blob?path=${encodeURIComponent(path)}`;
}
