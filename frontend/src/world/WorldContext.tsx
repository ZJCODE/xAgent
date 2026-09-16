import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  extractMentions,
  fileToBase64,
  resolveHumanIdentity,
  validateWorldName,
  HUMAN_DISPLAY_NAME,
  HUMAN_MEMBER_ID,
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  worldWsUrl,
  type NeighborAgent,
  type PendingFile,
  type WorldEvent,
  type WorldMember,
  type WorldSummary,
} from "./protocol";

export type ConnKind = "ok" | "bad" | "info" | "";

interface WorldState {
  worldId: string;
  worldName: string;
  worlds: WorldSummary[];
  memberId: string;
  names: Record<string, string>;
  present: WorldMember[];
  events: WorldEvent[];
  neighbors: NeighborAgent[];
  joined: boolean;
  connected: boolean;
  status: string;
  statusKind: ConnKind;
  gateError: string;
  agentError: string;
  createError: string;
  deleteError: string;
}

interface WorldContextValue extends WorldState {
  createName: string;
  setCreateName: (value: string) => void;
  speakText: string;
  setSpeakText: (value: string) => void;
  pendingFiles: PendingFile[];
  attachError: string;
  addFiles: (files: FileList | File[]) => void;
  removeFile: (id: string) => void;
  sending: boolean;
  creating: boolean;
  deleting: boolean;
  sidebarOpen: boolean;
  setSidebarOpen: (value: boolean) => void;
  enterWorld: (worldId: string) => Promise<void>;
  createWorld: () => Promise<boolean>;
  deleteWorld: (worldId: string) => Promise<boolean>;
  speak: () => Promise<void>;
  knockAgent: (agent: NeighborAgent, action: "join" | "leave") => Promise<void>;
  displayOf: (id: string) => string;
  refreshWorlds: () => Promise<WorldSummary[]>;
}

const WorldContext = createContext<WorldContextValue | null>(null);

const initialState: WorldState = {
  worldId: "",
  worldName: "Select a world",
  worlds: [],
  memberId: "",
  names: {},
  present: [],
  events: [],
  neighbors: [],
  joined: false,
  connected: false,
  status: "idle",
  statusKind: "",
  gateError: "",
  agentError: "",
  createError: "",
  deleteError: "",
};

function closeIfNarrow() {
  return window.matchMedia("(max-width: 860px)").matches;
}

export function WorldProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<WorldState>(initialState);
  const [createName, setCreateName] = useState("");
  const [speakText, setSpeakText] = useState("");
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [attachError, setAttachError] = useState("");
  const [sending, setSending] = useState(false);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const seenRef = useRef(new Set<number>());
  const lastSeqRef = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;

  const displayOf = useCallback((id: string) => {
    if (id === HUMAN_MEMBER_ID || id.startsWith(`${HUMAN_MEMBER_ID}-`)) {
      return HUMAN_DISPLAY_NAME;
    }
    return stateRef.current.names[id] || id;
  }, []);

  const send = useCallback((obj: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }, []);

  const rememberName = useCallback((id: string, name?: string) => {
    if (!id) return;
    setState((prev) => {
      if (prev.names[id] && !name) return prev;
      return { ...prev, names: { ...prev.names, [id]: name || prev.names[id] || id } };
    });
  }, []);

  const appendEvents = useCallback((incoming: WorldEvent[]) => {
    setState((prev) => {
      const next = [...prev.events];
      for (const event of incoming) {
        const seq = Number(event.seq || 0);
        if (seq && seenRef.current.has(seq)) continue;
        if (seq) seenRef.current.add(seq);
        if (seq > lastSeqRef.current) lastSeqRef.current = seq;
        const actor = String(event.actor_id || "");
        if (actor) rememberName(actor);
        next.push(event);
      }
      return { ...prev, events: next };
    });
  }, [rememberName]);

  const handleMessage = useCallback((msg: WorldEvent) => {
    const type = msg.type;
    if (type === "welcome") {
      setState((prev) => ({
        ...prev,
        worldId: msg.world_id || prev.worldId,
        worldName: msg.name || prev.worldName,
      }));
      send({ type: "join" });
      return;
    }
    if (type === "snapshot") {
      if (msg.sync) {
        appendEvents(msg.events || []);
        return;
      }
      seenRef.current = new Set();
      lastSeqRef.current = 0;
      const incoming = msg.events || [];
      const nextEvents: WorldEvent[] = [];
      for (const event of incoming) {
        const seq = Number(event.seq || 0);
        if (seq && seenRef.current.has(seq)) continue;
        if (seq) seenRef.current.add(seq);
        if (seq > lastSeqRef.current) lastSeqRef.current = seq;
        nextEvents.push(event);
      }
      const roster = Array.isArray(msg.present) ? msg.present : [];
      setState((prev) => {
        const names = { ...prev.names };
        for (const person of roster) {
          names[person.member_id] = person.display_name || person.member_id;
        }
        return {
          ...prev,
          worldName: msg.name || prev.worldName,
          present: roster,
          names,
          events: nextEvents,
          joined: true,
          status: "Present",
          statusKind: "ok",
        };
      });
      return;
    }
    if (type === "event") {
      const kind = msg.kind;
      const actor = String(msg.actor_id || "");
      if (kind === "join" && actor) {
        setState((prev) => {
          if (prev.present.some((item) => item.member_id === actor)) return prev;
          return {
            ...prev,
            present: [...prev.present, { member_id: actor, display_name: prev.names[actor] || actor }],
            names: { ...prev.names, [actor]: prev.names[actor] || actor },
          };
        });
      }
      if (kind === "leave") {
        setState((prev) => ({
          ...prev,
          present: prev.present.filter((item) => item.member_id !== actor),
          joined: actor === prev.memberId ? false : prev.joined,
        }));
      }
      appendEvents([msg]);
      return;
    }
    if (type === "lagged") {
      setState((prev) => ({ ...prev, status: "Syncing", statusKind: "bad" }));
      send({
        type: "sync",
        after_seq: msg.after_seq || lastSeqRef.current,
      });
      return;
    }
    if (type === "error") {
      const message = `${msg.code ? `${msg.code}: ` : ""}${msg.message || "error"}`;
      if (["bad_file", "too_many_files", "file_too_large", "text_required"].includes(String(msg.code || ""))) {
        setAttachError(message);
        return;
      }
      if (msg.code === "replaced") {
        setState((prev) => ({
          ...prev,
          status: "Replaced elsewhere",
          statusKind: "bad",
          joined: false,
          gateError: "This body is already present elsewhere",
        }));
        return;
      }
      if (msg.code === "world_gone") {
        setState((prev) => ({
          ...prev,
          status: "Deleted",
          statusKind: "bad",
          joined: false,
          connected: false,
          worldId: "",
          worldName: "Select a world",
          present: [],
          events: [],
          gateError: "This world was deleted",
        }));
        return;
      }
      setState((prev) => ({
        ...prev,
        joined: msg.code === "not_present" ? false : prev.joined,
        gateError: `${msg.code ? `${msg.code}: ` : ""}${msg.message || "error"}`,
      }));
    }
  }, [appendEvents, send]);

  const refreshWorlds = useCallback(async () => {
    try {
      const res = await fetch("/worlds");
      const data = (await res.json()) as { worlds?: WorldSummary[] };
      const worlds = data.worlds || [];
      setState((prev) => {
        const found = prev.worldId ? worlds.find((item) => item.id === prev.worldId) : undefined;
        if (found) {
          return { ...prev, worlds, worldName: found.name };
        }
        if (prev.connected) {
          return { ...prev, worlds };
        }
        return {
          ...prev,
          worlds,
          worldId: "",
          worldName: "Select a world",
        };
      });
      return worlds;
    } catch {
      setState((prev) => ({ ...prev, worlds: [] }));
      return [] as WorldSummary[];
    }
  }, []);

  const loadNeighbors = useCallback(async () => {
    try {
      const res = await fetch("/neighbors");
      const data = (await res.json()) as { agents?: NeighborAgent[] };
      const agents = data.agents || [];
      setState((prev) => ({ ...prev, neighbors: agents }));
      return agents;
    } catch {
      setState((prev) => ({ ...prev, neighbors: [] }));
      return [] as NeighborAgent[];
    }
  }, []);

  const handleMessageRef = useRef(handleMessage);
  handleMessageRef.current = handleMessage;

  const disconnectSocket = useCallback(() => {
    const ws = wsRef.current;
    wsRef.current = null;
    if (ws && ws.readyState <= WebSocket.OPEN) {
      try {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "leave" }));
      } catch {
        /* ignore */
      }
      ws.close();
    }
  }, []);

  const enterWorld = useCallback(async (worldId: string) => {
    const targetId = worldId.trim();
    if (!targetId) return;
    const current = stateRef.current;
    if (current.connected && current.worldId === targetId && current.joined) return;

    const found = current.worlds.find((item) => item.id === targetId);
    const worldName = found?.name || current.worldName || targetId;
    const neighbors = await loadNeighbors();
    const taken = new Set(neighbors.map((agent) => agent.name));
    const identity = resolveHumanIdentity(taken);
    const { member_id: name, display_name } = identity;

    disconnectSocket();
    setState((prev) => ({
      ...prev,
      worldId: targetId,
      worldName,
      memberId: name,
      names: { ...prev.names, [name]: display_name },
      present: [],
      events: [],
      joined: false,
      connected: false,
      status: "Connecting",
      statusKind: "info",
      gateError: "",
    }));
    seenRef.current = new Set();
    lastSeqRef.current = 0;

    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(worldWsUrl(targetId));
      wsRef.current = ws;
      let opened = false;
      ws.onopen = () => {
        opened = true;
        ws.send(JSON.stringify({ type: "hello", member_id: name, display_name }));
        setState((prev) => ({
          ...prev,
          connected: true,
          status: "Connected",
          statusKind: "ok",
        }));
        if (closeIfNarrow()) setSidebarOpen(false);
        resolve();
      };
      ws.onmessage = (event) => {
        try {
          handleMessageRef.current(JSON.parse(String(event.data)) as WorldEvent);
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (wsRef.current !== ws) return;
        wsRef.current = null;
        setState((prev) => ({
          ...prev,
          connected: false,
          joined: false,
          status: "Disconnected",
          statusKind: "bad",
        }));
        if (!opened) reject(new Error("connection closed"));
      };
      ws.onerror = () => {
        if (wsRef.current !== ws) return;
        setState((prev) => ({ ...prev, status: "Error", statusKind: "bad" }));
        if (!opened) reject(new Error("ws error"));
      };
    }).catch((err: Error) => {
      setState((prev) => ({ ...prev, gateError: err.message || "connection failed" }));
    });
  }, [disconnectSocket, loadNeighbors]);

  const createWorld = useCallback(async () => {
    const name = createName.trim();
    if (!name) {
      setState((prev) => ({ ...prev, createError: "Name is required" }));
      return false;
    }
    const invalid = validateWorldName(name);
    if (invalid) {
      setState((prev) => ({ ...prev, createError: invalid }));
      return false;
    }
    setCreating(true);
    setState((prev) => ({ ...prev, createError: "" }));
    try {
      const params = new URLSearchParams({ name });
      const res = await fetch(`/worlds/create?${params.toString()}`);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setState((prev) => ({
          ...prev,
          createError: String((body as { error?: string }).error || res.statusText || "Create failed"),
        }));
        return false;
      }
      const created = body as { id?: string; name?: string };
      const id = String(created.id || "");
      setCreateName("");
      await refreshWorlds();
      if (!id) return false;
      setState((prev) => ({
        ...prev,
        worldId: id,
        worldName: created.name || name,
        createError: "",
      }));
      await enterWorld(id);
      return true;
    } catch (err) {
      const message = err instanceof Error ? err.message : "Create failed";
      setState((prev) => ({ ...prev, createError: message }));
      return false;
    } finally {
      setCreating(false);
    }
  }, [createName, enterWorld, refreshWorlds]);

  const deleteWorld = useCallback(async (worldId: string) => {
    const targetId = worldId.trim();
    if (!targetId) {
      setState((prev) => ({ ...prev, deleteError: "World id is required" }));
      return false;
    }
    setDeleting(true);
    setState((prev) => ({ ...prev, deleteError: "" }));
    try {
      const params = new URLSearchParams({ confirm: targetId });
      const res = await fetch(`/worlds/${encodeURIComponent(targetId)}/delete?${params.toString()}`);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setState((prev) => ({
          ...prev,
          deleteError: String((body as { error?: string }).error || res.statusText || "Delete failed"),
        }));
        return false;
      }
      if (stateRef.current.worldId === targetId) {
        disconnectSocket();
      }
      const worlds = await refreshWorlds();
      setState((prev) => {
        if (prev.worldId !== targetId) {
          return { ...prev, worlds, deleteError: "" };
        }
        return {
          ...prev,
          worlds,
          worldId: "",
          worldName: "Select a world",
          present: [],
          events: [],
          joined: false,
          connected: false,
          status: "idle",
          statusKind: "",
          gateError: "",
          deleteError: "",
        };
      });
      return true;
    } catch (err) {
      const message = err instanceof Error ? err.message : "Delete failed";
      setState((prev) => ({ ...prev, deleteError: message }));
      return false;
    } finally {
      setDeleting(false);
    }
  }, [disconnectSocket, refreshWorlds]);

  const addFiles = useCallback((files: FileList | File[]) => {
    const incoming = Array.from(files);
    if (!incoming.length) return;
    setAttachError("");
    setPendingFiles((prev) => {
      const next = [...prev];
      for (const file of incoming) {
        if (next.length >= MAX_ATTACHMENTS) {
          setAttachError(`At most ${MAX_ATTACHMENTS} files per message`);
          break;
        }
        if (file.size > MAX_FILE_BYTES) {
          setAttachError(`${file.name} exceeds 8MB`);
          continue;
        }
        if (!file.size) {
          setAttachError(`${file.name} is empty`);
          continue;
        }
        next.push({
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          name: file.name,
          mime: file.type || "application/octet-stream",
          size: file.size,
          file,
          previewUrl: URL.createObjectURL(file),
        });
      }
      return next;
    });
  }, []);

  const removeFile = useCallback((id: string) => {
    setPendingFiles((prev) => {
      const target = prev.find((item) => item.id === id);
      if (target) URL.revokeObjectURL(target.previewUrl);
      return prev.filter((item) => item.id !== id);
    });
    setAttachError("");
  }, []);

  const speak = useCallback(async () => {
    const current = stateRef.current;
    if (!current.joined || sending) return;
    const text = speakText.trim();
    const files = pendingFiles;
    if (!text && !files.length) return;
    const people = [
      ...current.present,
      ...current.neighbors.map((agent) => ({
        member_id: agent.name,
        display_name: agent.title || agent.name,
      })),
    ];
    const mentions = extractMentions(text, people);
    setSending(true);
    setAttachError("");
    try {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        setAttachError("Not connected to a world");
        return;
      }
      const attachments = await Promise.all(
        files.map(async (item) => ({
          name: item.name,
          mime: item.mime,
          data: await fileToBase64(item.file),
        })),
      );
      ws.send(JSON.stringify({
        type: "speak",
        text,
        ...(mentions.length ? { mentions } : {}),
        ...(attachments.length ? { attachments } : {}),
      }));
      setSpeakText("");
      files.forEach((item) => URL.revokeObjectURL(item.previewUrl));
      setPendingFiles([]);
    } catch (err) {
      const message = err instanceof Error ? err.message : "read failed";
      setAttachError(message);
    } finally {
      setSending(false);
    }
  }, [speakText, pendingFiles, sending]);

  const knockAgent = useCallback(async (agent: NeighborAgent, action: "join" | "leave") => {
    setState((prev) => ({ ...prev, agentError: "" }));
    const here = stateRef.current.present.some((item) => item.member_id === agent.name);
    if (action === "join" && here) return;
    if (action === "leave" && !here) return;
    const worldId = stateRef.current.worldId;
    if (action === "join" && !worldId) {
      setState((prev) => ({ ...prev, agentError: "Join a world first" }));
      return;
    }
    const path = action === "leave" ? "/world/leave" : "/world/join";
    const url = agent.api_url.replace(/\/$/, "") + path;
    try {
      const options: RequestInit = { method: "POST", headers: { "Content-Type": "application/json" } };
      if (action === "join") {
        options.body = JSON.stringify({
          world_url: worldWsUrl(worldId),
          member_id: agent.name,
          display_name: agent.title || agent.name,
        });
      }
      const res = await fetch(url, options);
      if (!res.ok) {
        if (res.status === 404) {
          setState((prev) => ({ ...prev, agentError: `${agent.name} needs an API restart` }));
          return;
        }
        const body = await res.text();
        setState((prev) => ({ ...prev, agentError: `${agent.name} ${res.status} ${body.slice(0, 160)}` }));
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "failed";
      setState((prev) => ({ ...prev, agentError: `${agent.name} ${message}` }));
    }
  }, []);

  useEffect(() => {
    void refreshWorlds();
    void loadNeighbors();
    const timer = window.setInterval(() => {
      void refreshWorlds();
      void loadNeighbors();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadNeighbors, refreshWorlds]);

  useEffect(() => () => disconnectSocket(), [disconnectSocket]);

  const pendingRef = useRef(pendingFiles);
  pendingRef.current = pendingFiles;
  useEffect(
    () => () => {
      pendingRef.current.forEach((item) => URL.revokeObjectURL(item.previewUrl));
    },
    [],
  );

  const value = useMemo<WorldContextValue>(
    () => ({
      ...state,
      createName,
      setCreateName,
      speakText,
      setSpeakText,
      pendingFiles,
      attachError,
      addFiles,
      removeFile,
      sending,
      creating,
      deleting,
      sidebarOpen,
      setSidebarOpen,
      enterWorld,
      createWorld,
      deleteWorld,
      speak,
      knockAgent,
      displayOf,
      refreshWorlds,
    }),
    [
      state,
      createName,
      speakText,
      pendingFiles,
      attachError,
      sidebarOpen,
      creating,
      deleting,
      sending,
      enterWorld,
      createWorld,
      deleteWorld,
      speak,
      addFiles,
      removeFile,
      knockAgent,
      displayOf,
      refreshWorlds,
    ],
  );

  return <WorldContext.Provider value={value}>{children}</WorldContext.Provider>;
}

export function useWorld() {
  const value = useContext(WorldContext);
  if (!value) throw new Error("useWorld must be used inside WorldProvider");
  return value;
}
