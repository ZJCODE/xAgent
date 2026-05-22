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
  WorkspaceUploadResult,
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

export async function uploadWorkspaceFile(file: File, path: string): Promise<WorkspaceUploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("path", path);
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

export function workspaceBlobUrl(path: string): string {
  return `/api/workspace/blob?path=${encodeURIComponent(path)}`;
}
