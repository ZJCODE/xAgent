import type {
  AgentIdentity,
  AgentInfo,
  FileReadResult,
  FileNode,
  MessageSearchResponse,
  MessagesResponse,
  MessagesStats,
  SearchResult,
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
