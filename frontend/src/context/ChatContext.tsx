import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { ChatEvent, ChatMessage, ChatPanelState, ChatSettings } from "../types";
import { makeId } from "../lib/format";

const GLOBAL_SETTINGS_KEY = "xagent_web_settings";
const HISTORY_KEY = "xagent_chat_history";

type PanelId = ChatPanelState["id"];

interface ChatContextValue {
  dualMode: boolean;
  setDualMode: (value: boolean) => void;
  panels: Record<PanelId, ChatPanelState>;
  activePanels: ChatPanelState[];
  status: "idle" | "sending";
  updateSettings: (panelId: PanelId, settings: Partial<ChatSettings>) => void;
  addImages: (panelId: PanelId, files: FileList | File[]) => void;
  removeImage: (panelId: PanelId, index: number) => void;
  sendMessage: (panelId: PanelId, text: string) => Promise<void>;
  sendObservation: (panelId: PanelId, text: string) => Promise<void>;
  clearPanel: (panelId: PanelId) => void;
  clearVisiblePanels: () => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

function defaultSettings(panelId: PanelId): ChatSettings {
  return {
    userId: panelId === "left" ? "John" : panelId === "right" ? "Alice" : "web_user",
    stream: true,
    memory: true,
  };
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function settingsKey(panelId: PanelId): string {
  return `${GLOBAL_SETTINGS_KEY}_${panelId}`;
}

function historyKey(panelId: PanelId): string {
  return `${HISTORY_KEY}_${panelId}`;
}

function createPanel(panelId: PanelId): ChatPanelState {
  const global = readJson<Partial<ChatSettings> & { dual?: boolean }>(GLOBAL_SETTINGS_KEY, {});
  const savedSettings =
    panelId === "single"
      ? {
          userId: global.userId,
          stream: global.stream,
          memory: global.memory,
        }
      : readJson<Partial<ChatSettings>>(settingsKey(panelId), {});
  const history = readJson<ChatMessage[]>(historyKey(panelId), []);
  return {
    id: panelId,
    messages: Array.isArray(history) ? history.filter((message) => !message.pending) : [],
    pendingImages: [],
    settings: {
      ...defaultSettings(panelId),
      ...savedSettings,
    },
    sending: false,
  };
}

function webSocketUrl(path: string): string {
  const url = new URL(path, window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function persistSettings(panel: ChatPanelState, dualMode: boolean) {
  if (panel.id === "single") {
    localStorage.setItem(
      GLOBAL_SETTINGS_KEY,
      JSON.stringify({
        userId: panel.settings.userId,
        stream: panel.settings.stream,
        memory: panel.settings.memory,
        dual: dualMode,
      }),
    );
    return;
  }
  localStorage.setItem(settingsKey(panel.id), JSON.stringify(panel.settings));
}

function persistHistory(panel: ChatPanelState) {
  const slim = panel.messages
    .filter((message) => !message.pending)
    .map((message) => ({
      ...message,
      images: (message.images || []).map(() => "[image]"),
    }));
  try {
    localStorage.setItem(historyKey(panel.id), JSON.stringify(slim));
  } catch {
    // Local image data may exceed browser storage; chat still remains in memory.
  }
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const global = readJson<{ dual?: boolean }>(GLOBAL_SETTINGS_KEY, {});
  const [dualMode, setDualModeState] = useState(Boolean(global.dual));
  const [panels, setPanels] = useState<Record<PanelId, ChatPanelState>>({
    single: createPanel("single"),
    left: createPanel("left"),
    right: createPanel("right"),
  });
  const socketsRef = useRef<Record<string, WebSocket>>({});

  const setDualMode = useCallback((value: boolean) => {
    setDualModeState(value);
    const current = readJson<Record<string, unknown>>(GLOBAL_SETTINGS_KEY, {});
    localStorage.setItem(GLOBAL_SETTINGS_KEY, JSON.stringify({ ...current, dual: value }));
  }, []);

  const patchPanel = useCallback((panelId: PanelId, updater: (panel: ChatPanelState) => ChatPanelState) => {
    setPanels((current) => {
      const updatedPanel = updater(current[panelId]);
      const next = { ...current, [panelId]: updatedPanel };
      persistHistory(updatedPanel);
      return next;
    });
  }, []);

  const updateSettings = useCallback(
    (panelId: PanelId, settings: Partial<ChatSettings>) => {
      setPanels((current) => {
        const updatedPanel = {
          ...current[panelId],
          settings: { ...current[panelId].settings, ...settings },
        };
        persistSettings(updatedPanel, dualMode);
        return { ...current, [panelId]: updatedPanel };
      });
    },
    [dualMode],
  );

  const addImages = useCallback((panelId: PanelId, files: FileList | File[]) => {
    Array.from(files).forEach((file) => {
      if (!file.type.match(/^image\/(png|jpeg)$/)) return;
      const reader = new FileReader();
      reader.onload = (event) => {
        const value = String(event.target?.result || "");
        if (!value) return;
        patchPanel(panelId, (panel) => ({
          ...panel,
          pendingImages: [...panel.pendingImages, value],
        }));
      };
      reader.readAsDataURL(file);
    });
  }, [patchPanel]);

  const removeImage = useCallback(
    (panelId: PanelId, index: number) => {
      patchPanel(panelId, (panel) => ({
        ...panel,
        pendingImages: panel.pendingImages.filter((_, itemIndex) => itemIndex !== index),
      }));
    },
    [patchPanel],
  );

  const runSocket = useCallback((panelId: PanelId, payload: Record<string, unknown>, assistantId: string) => {
    return new Promise<void>((resolve, reject) => {
      const socketKey = `${panelId}-${assistantId}`;
      const socket = new WebSocket(webSocketUrl("/ws/chat"));
      socketsRef.current[socketKey] = socket;
      let settled = false;
      let text = "";

      const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        delete socketsRef.current[socketKey];
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
          socket.close(1000);
        }
        patchPanel(panelId, (panel) => ({
          ...panel,
          sending: false,
          messages: panel.messages.map((message) =>
            message.id === assistantId
              ? {
                  ...message,
                  pending: false,
                  error: Boolean(error),
                  content: error ? `Error: ${error.message}` : message.content,
                }
              : message,
          ),
        }));
        if (error) reject(error);
        else resolve();
      };

      socket.addEventListener("open", () => {
        socket.send(JSON.stringify(payload));
      });

      socket.addEventListener("message", (event) => {
        let parsed: ChatEvent;
        try {
          parsed = JSON.parse(event.data) as ChatEvent;
        } catch {
          finish(new Error("Invalid WebSocket response."));
          return;
        }

        if (parsed.type === "error" || parsed.error) {
          finish(new Error(parsed.error || "WebSocket chat failed."));
          return;
        }

        if (parsed.type === "message_delta" && parsed.delta) {
          text += parsed.delta;
          patchPanel(panelId, (panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === assistantId ? { ...message, content: text } : message,
            ),
          }));
          return;
        }

        if (parsed.type === "message_done") {
          text = parsed.content == null ? text : String(parsed.content);
          patchPanel(panelId, (panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === assistantId ? { ...message, content: text, pending: false } : message,
            ),
          }));
          return;
        }

        if (parsed.message != null) {
          text = typeof parsed.message === "string" ? parsed.message : JSON.stringify(parsed.message, null, 2);
          patchPanel(panelId, (panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === assistantId ? { ...message, content: text, pending: false } : message,
            ),
          }));
          return;
        }

        if (parsed.type === "done") finish();
      });

      socket.addEventListener("error", () => finish(new Error("WebSocket connection failed.")));
      socket.addEventListener("close", () => {
        if (!settled) finish(new Error("WebSocket connection closed before completion."));
      });
    });
  }, [patchPanel]);

  const sendMessage = useCallback(
    async (panelId: PanelId, rawText: string) => {
      const text = rawText.trim();
      if (!text || panels[panelId].sending) return;
      const panel = panels[panelId];
      const images = [...panel.pendingImages];
      const userMessage: ChatMessage = {
        id: makeId("user"),
        role: "user",
        content: text,
        images,
      };
      const assistantMessage: ChatMessage = {
        id: makeId("assistant"),
        role: "assistant",
        content: "",
        pending: true,
      };

      patchPanel(panelId, (current) => ({
        ...current,
        pendingImages: [],
        sending: true,
        messages: [...current.messages, userMessage, assistantMessage],
      }));

      const payload: Record<string, unknown> = {
        user_id: panel.settings.userId,
        user_message: text,
        stream: panel.settings.stream,
        enable_memory: panel.settings.memory,
      };
      if (images.length === 1) payload.image_source = images[0];
      if (images.length > 1) payload.image_source = images;
      persistSettings(panel, dualMode);

      await runSocket(panelId, payload, assistantMessage.id).catch(() => undefined);
    },
    [dualMode, panels, patchPanel, runSocket],
  );

  const sendObservation = useCallback(
    async (panelId: PanelId, rawText: string) => {
      const text = rawText.trim();
      if (!text || panels[panelId].sending) return;
      const observationMessage: ChatMessage = {
        id: makeId("observation"),
        role: "observation",
        content: text,
      };
      patchPanel(panelId, (panel) => ({
        ...panel,
        sending: true,
        messages: [...panel.messages, observationMessage],
      }));

      try {
        const socket = new WebSocket(webSocketUrl("/ws/observe"));
        await new Promise<void>((resolve, reject) => {
          let settled = false;
          const finish = (error?: Error) => {
            if (settled) return;
            settled = true;
            if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
              socket.close(1000);
            }
            if (error) reject(error);
            else resolve();
          };
          socket.addEventListener("open", () => {
            socket.send(JSON.stringify({ context: text, source: "web", event_type: "observation" }));
          });
          socket.addEventListener("message", (event) => {
            let parsed: ChatEvent;
            try {
              parsed = JSON.parse(event.data) as ChatEvent;
            } catch {
              finish(new Error("Invalid WebSocket response."));
              return;
            }
            if (parsed.type === "error" || parsed.error) finish(new Error(parsed.error || "Observe failed."));
            if (parsed.type === "done") finish();
          });
          socket.addEventListener("error", () => finish(new Error("WebSocket connection failed.")));
          socket.addEventListener("close", () => {
            if (!settled) finish(new Error("WebSocket connection closed before completion."));
          });
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        patchPanel(panelId, (panel) => ({
          ...panel,
          messages: [...panel.messages, { id: makeId("error"), role: "assistant", content: `Error: ${message}`, error: true }],
        }));
      } finally {
        patchPanel(panelId, (panel) => ({ ...panel, sending: false }));
      }
    },
    [panels, patchPanel],
  );

  const clearPanel = useCallback(
    (panelId: PanelId) => {
      patchPanel(panelId, (panel) => ({ ...panel, messages: [], pendingImages: [] }));
      localStorage.removeItem(historyKey(panelId));
    },
    [patchPanel],
  );

  const clearVisiblePanels = useCallback(() => {
    (dualMode ? (["left", "right"] as PanelId[]) : (["single"] as PanelId[])).forEach(clearPanel);
  }, [clearPanel, dualMode]);

  const activePanels = useMemo(
    () => (dualMode ? [panels.left, panels.right] : [panels.single]),
    [dualMode, panels],
  );
  const status: "idle" | "sending" = activePanels.some((panel) => panel.sending) ? "sending" : "idle";

  const value = useMemo(
    () => ({
      dualMode,
      setDualMode,
      panels,
      activePanels,
      status,
      updateSettings,
      addImages,
      removeImage,
      sendMessage,
      sendObservation,
      clearPanel,
      clearVisiblePanels,
    }),
    [
      dualMode,
      setDualMode,
      panels,
      activePanels,
      status,
      updateSettings,
      addImages,
      removeImage,
      sendMessage,
      sendObservation,
      clearPanel,
      clearVisiblePanels,
    ],
  );

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export function useChat() {
  const value = useContext(ChatContext);
  if (!value) throw new Error("useChat must be used inside ChatProvider");
  return value;
}
