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
  loadIdentity,
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  saveIdentity,
  validateMemberId,
  worldWsUrl,
  type NeighborAgent,
  type PendingFile,
  type WorldEvent,
  type WorldMember,
} from "./protocol";

export type ConnKind = "ok" | "bad" | "info" | "";

interface WorldState {
  roomId: string;
  roomTitle: string;
  setting: string;
  worldLabel: string;
  memberId: string;
  names: Record<string, string>;
  present: WorldMember[];
  events: WorldEvent[];
  neighbors: NeighborAgent[];
  presentInRoom: boolean;
  connected: boolean;
  status: string;
  statusKind: ConnKind;
  gateError: string;
  agentError: string;
}

interface WorldContextValue extends WorldState {
  draftName: string;
  setDraftName: (value: string) => void;
  speakText: string;
  setSpeakText: (value: string) => void;
  pendingFiles: PendingFile[];
  attachError: string;
  addFiles: (files: FileList | File[]) => void;
  removeFile: (id: string) => void;
  sending: boolean;
  sidebarOpen: boolean;
  setSidebarOpen: (value: boolean) => void;
  enterHall: () => Promise<void>;
  leaveHall: () => void;
  speak: () => Promise<void>;
  knockAgent: (agent: NeighborAgent, action: "join" | "leave") => Promise<void>;
  displayOf: (id: string) => string;
}

const WorldContext = createContext<WorldContextValue | null>(null);

const initialState: WorldState = {
  roomId: "hall",
  roomTitle: "大厅",
  setting: "",
  worldLabel: "未连接",
  memberId: "",
  names: {},
  present: [],
  events: [],
  neighbors: [],
  presentInRoom: false,
  connected: false,
  status: "idle",
  statusKind: "",
  gateError: "",
  agentError: "",
};

function closeIfNarrow() {
  return window.matchMedia("(max-width: 860px)").matches;
}

export function WorldProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<WorldState>(initialState);
  const [draftName, setDraftName] = useState(loadIdentity);
  const [speakText, setSpeakText] = useState("");
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [attachError, setAttachError] = useState("");
  const [sending, setSending] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const seenRef = useRef(new Set<number>());
  const lastSeqRef = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;

  const displayOf = useCallback((id: string) => stateRef.current.names[id] || id, []);

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
      const rooms = msg.rooms || [];
      const currentId = stateRef.current.roomId;
      const roomId = rooms.length && !rooms.some((room) => room.id === currentId)
        ? rooms[0].id
        : currentId;
      setState((prev) => ({
        ...prev,
        worldLabel: `world ${msg.world_id || ""}`.trim(),
        roomId,
      }));
      send({ type: "join", room_id: roomId });
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
      const present = msg.present || [];
      setState((prev) => {
        const names = { ...prev.names };
        for (const person of present) {
          names[person.member_id] = person.display_name || person.member_id;
        }
        return {
          ...prev,
          roomId: msg.room_id || prev.roomId,
          roomTitle: msg.name || msg.room_id || prev.roomTitle,
          setting: msg.setting || "",
          present,
          names,
          events: nextEvents,
          presentInRoom: true,
          status: `in ${msg.room_id || prev.roomId}`,
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
          presentInRoom: actor === prev.memberId ? false : prev.presentInRoom,
        }));
      }
      appendEvents([msg]);
      return;
    }
    if (type === "lagged") {
      setState((prev) => ({ ...prev, status: "lagged · sync", statusKind: "bad" }));
      send({
        type: "sync",
        room_id: msg.room_id || stateRef.current.roomId,
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
          status: "replaced",
          statusKind: "bad",
          presentInRoom: false,
          gateError: "这个身体已在别处出现",
        }));
        return;
      }
      setState((prev) => ({
        ...prev,
        presentInRoom: msg.code === "not_present" ? false : prev.presentInRoom,
        gateError: `${msg.code ? `${msg.code}: ` : ""}${msg.message || "error"}`,
      }));
    }
  }, [appendEvents, send]);

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

  const enterHall = useCallback(async () => {
    const name = draftName.trim();
    const neighbors = await loadNeighbors();
    const taken = new Set(neighbors.map((agent) => agent.name));
    const invalid = validateMemberId(name, taken);
    if (invalid) {
      setState((prev) => ({ ...prev, gateError: invalid }));
      return;
    }
    saveIdentity(name);
    setState((prev) => ({
      ...prev,
      memberId: name,
      names: { ...prev.names, [name]: name },
      presentInRoom: false,
      status: "connecting",
      statusKind: "info",
      gateError: "",
    }));
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(worldWsUrl());
      wsRef.current = ws;
      let opened = false;
      ws.onopen = () => {
        opened = true;
        ws.send(JSON.stringify({ type: "hello", member_id: name, display_name: name }));
        setState((prev) => ({
          ...prev,
          connected: true,
          status: "connected",
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
        wsRef.current = null;
        setState((prev) => ({
          ...prev,
          connected: false,
          presentInRoom: false,
          status: "disconnected",
          statusKind: "bad",
        }));
        if (!opened) reject(new Error("连接关闭"));
      };
      ws.onerror = () => {
        setState((prev) => ({ ...prev, status: "error", statusKind: "bad" }));
        if (!opened) reject(new Error("ws error"));
      };
    }).catch((err: Error) => {
      setState((prev) => ({ ...prev, gateError: err.message || "连接失败" }));
    });
  }, [draftName, loadNeighbors]);

  const leaveHall = useCallback(() => {
    send({ type: "leave", room_id: stateRef.current.roomId });
    setState((prev) => ({ ...prev, presentInRoom: false }));
    wsRef.current?.close();
  }, [send]);

  const addFiles = useCallback((files: FileList | File[]) => {
    const incoming = Array.from(files);
    if (!incoming.length) return;
    setAttachError("");
    setPendingFiles((prev) => {
      const next = [...prev];
      for (const file of incoming) {
        if (next.length >= MAX_ATTACHMENTS) {
          setAttachError(`一次最多 ${MAX_ATTACHMENTS} 个文件`);
          break;
        }
        if (file.size > MAX_FILE_BYTES) {
          setAttachError(`${file.name} 超过 8MB`);
          continue;
        }
        if (!file.size) {
          setAttachError(`${file.name} 是空文件`);
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
    if (!current.presentInRoom || sending) return;
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
        setAttachError("未连接到大厅");
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
        room_id: current.roomId,
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
    const path = action === "leave" ? "/world/leave" : "/world/join";
    const url = agent.api_url.replace(/\/$/, "") + path;
    try {
      const options: RequestInit = { method: "POST", headers: { "Content-Type": "application/json" } };
      if (action === "join") {
        options.body = JSON.stringify({
          world_url: worldWsUrl(),
          room_id: stateRef.current.roomId,
          member_id: agent.name,
          display_name: agent.title || agent.name,
        });
      }
      const res = await fetch(url, options);
      if (!res.ok) {
        if (res.status === 404) {
          setState((prev) => ({ ...prev, agentError: `${agent.name} 需重启 API` }));
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
    void loadNeighbors();
    const timer = window.setInterval(() => void loadNeighbors(), 5000);
    return () => window.clearInterval(timer);
  }, [loadNeighbors]);

  useEffect(() => () => wsRef.current?.close(), []);

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
      draftName,
      setDraftName,
      speakText,
      setSpeakText,
      pendingFiles,
      attachError,
      addFiles,
      removeFile,
      sending,
      sidebarOpen,
      setSidebarOpen,
      enterHall,
      leaveHall,
      speak,
      knockAgent,
      displayOf,
    }),
    [state, draftName, speakText, pendingFiles, attachError, sidebarOpen, enterHall, leaveHall, speak, addFiles, removeFile, sending, knockAgent, displayOf],
  );

  return <WorldContext.Provider value={value}>{children}</WorldContext.Provider>;
}

export function useWorld() {
  const value = useContext(WorldContext);
  if (!value) throw new Error("useWorld must be used inside WorldProvider");
  return value;
}
