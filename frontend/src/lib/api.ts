import type {
  AgentIdentity,
  AgentInfo,
  FileReadResult,
  FileNode,
  MessageSearchResponse,
  MessagesResponse,
  MessagesStats,
  SearchResult,
  SkillCreateInput,
  SkillCreateResponse,
  SkillStateResponse,
  SkillsInfo,
  SkillsTreeResponse,
  TasksResponse,
  WorkspaceUploadResult,
  ConfigPreview,
  ConsoleAgentSummary,
  ConsoleAgentsResponse,
  ConsoleChannelState,
  ConsoleConfigResponse,
  ConsoleCreateAgentInput,
  ConsoleLogResponse,
  ConsoleOverviewResponse,
  SetupSessionResponse,
} from "../types";

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data?.detail === "string" ? data.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return data as T;
}

export async function getHealth(): Promise<{ status: string; service: string }> {
  return requestJson("/health", { signal: AbortSignal.timeout(5000) });
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

export async function getTasks(): Promise<TasksResponse> {
  return requestJson("/api/tasks");
}

export async function deleteTask(taskId: string): Promise<{ status: string; deleted: unknown }> {
  return requestJson(`/api/tasks/delete?task_id=${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

export function workspaceBlobUrl(path: string): string {
  return `/api/workspace/blob?path=${encodeURIComponent(path)}`;
}

function agentPath(agentName: string, suffix: string): string {
  return `/api/console/agents/${encodeURIComponent(agentName)}${suffix}`;
}

export async function getConsoleAgents(): Promise<ConsoleAgentsResponse> {
  return requestJson("/api/console/agents");
}

export async function createConsoleAgent(input: ConsoleCreateAgentInput): Promise<ConsoleAgentsResponse & { status: string }> {
  return requestJson("/api/console/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function updateConsoleAgent(name: string, input: { title?: string }): Promise<{ status: string; agent: ConsoleAgentSummary }> {
  return requestJson(agentPath(name, ""), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function selectConsoleAgent(name: string): Promise<{ status: string; active_agent: string }> {
  return requestJson(agentPath(name, "/select"), { method: "POST" });
}

export async function deleteConsoleAgent(name: string, stopRunningChannels = false): Promise<{ status: string; deleted: unknown }> {
  return requestJson(`${agentPath(name, "")}?stop_running_channels=${stopRunningChannels ? "true" : "false"}`, {
    method: "DELETE",
  });
}

export async function getConsoleOverview(name: string): Promise<ConsoleOverviewResponse> {
  return requestJson(agentPath(name, "/overview"));
}

export async function getConsoleIdentity(name: string): Promise<AgentIdentity> {
  return requestJson(agentPath(name, "/identity"));
}

export async function updateConsoleIdentity(name: string, identity: string): Promise<AgentIdentity> {
  return requestJson(agentPath(name, "/identity"), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identity }),
  });
}

export async function getConsoleConfig(name: string): Promise<ConsoleConfigResponse> {
  return requestJson(agentPath(name, "/config"));
}

export async function previewConsoleConfig(name: string, config: Record<string, unknown>): Promise<ConfigPreview> {
  return requestJson(agentPath(name, "/config/preview"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
}

export async function updateConsoleConfig(name: string, config: Record<string, unknown>): Promise<ConsoleConfigResponse> {
  return requestJson(agentPath(name, "/config"), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
}

export async function getConsoleChannels(name: string): Promise<{ channels: ConsoleChannelState[] }> {
  return requestJson(agentPath(name, "/channels"));
}

export async function runConsoleChannelAction(name: string, channel: string, action: "start" | "stop" | "restart"): Promise<{ status: string; channel: ConsoleChannelState }> {
  return requestJson(agentPath(name, `/channels/${encodeURIComponent(channel)}/${action}`), { method: "POST" });
}

export async function getConsoleChannelLogs(name: string, channel: string, lines = 120): Promise<ConsoleLogResponse> {
  return requestJson(agentPath(name, `/channels/${encodeURIComponent(channel)}/logs?lines=${lines}`));
}

export async function startConsoleSetupSession(name: string, input: Record<string, unknown>): Promise<SetupSessionResponse> {
  return requestJson(agentPath(name, "/setup-sessions"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function getConsoleSetupSessionEvents(sessionId: string): Promise<{ session_id: string; events: import("../types").SetupSessionEvent[] }> {
  return requestJson(`/api/console/setup-sessions/${encodeURIComponent(sessionId)}/events`);
}

export async function getConsoleMemoryTree(name: string): Promise<{ tree: FileNode[] }> {
  return requestJson(agentPath(name, "/memory/tree"));
}

export async function readConsoleMemoryFile(name: string, path: string): Promise<FileReadResult> {
  return requestJson(`${agentPath(name, "/memory/read")}?path=${encodeURIComponent(path)}`);
}

export async function searchConsoleMemory(name: string, query: string): Promise<{ query: string; results: SearchResult[] }> {
  return requestJson(`${agentPath(name, "/memory/search")}?query=${encodeURIComponent(query)}`);
}

export async function getConsoleWorkspaceTree(name: string): Promise<{ root: string; tree: FileNode[] }> {
  return requestJson(agentPath(name, "/workspace/tree"));
}

export async function readConsoleWorkspaceFile(name: string, path: string): Promise<FileReadResult> {
  return requestJson(`${agentPath(name, "/workspace/read")}?path=${encodeURIComponent(path)}`);
}

export async function uploadConsoleWorkspaceFile(name: string, file: File, path?: string): Promise<WorkspaceUploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  if (path) formData.append("path", path);
  return requestJson(agentPath(name, "/workspace/upload"), { method: "POST", body: formData });
}

export async function getConsoleMessages(name: string, count: number, offset: number): Promise<MessagesResponse> {
  return requestJson(`${agentPath(name, "/messages")}?count=${count}&offset=${offset}`);
}

export async function getConsoleMessagesStats(name: string): Promise<MessagesStats> {
  return requestJson(agentPath(name, "/messages/stats"));
}

export async function getConsoleSkillsInfo(name: string): Promise<SkillsInfo> {
  return requestJson(agentPath(name, "/skills/info"));
}

export async function getConsoleSkillsTree(name: string): Promise<SkillsTreeResponse> {
  return requestJson(agentPath(name, "/skills/tree"));
}

export async function getConsoleTasks(name: string): Promise<TasksResponse> {
  return requestJson(agentPath(name, "/tasks"));
}

export async function deleteConsoleTask(name: string, taskId: string): Promise<{ status: string; deleted: unknown }> {
  return requestJson(`${agentPath(name, "/tasks/delete")}?task_id=${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

export function consoleWorkspaceBlobUrl(agentName: string, path: string): string {
  return agentPath(agentName, `/workspace/blob?path=${encodeURIComponent(path)}`);
}

export function consoleWebSocketUrl(path: string): string {
  const url = new URL(path, window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}
