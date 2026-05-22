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
import { getAgentInfo, uploadWorkspaceFile, workspaceBlobUrl } from "../lib/api";
import type { AgentCapabilities, ChatEvent, ChatMessage, ChatPanelState, ChatSettings } from "../types";
import { makeId } from "../lib/format";

const GLOBAL_SETTINGS_KEY = "xagent_web_settings";
const HISTORY_KEY = "xagent_chat_history";
const DEFAULT_CAPABILITIES: AgentCapabilities = {
  vision: true,
  vision_input: true,
  web_search: false,
  image_generation: false,
};
const MAX_IMAGES_PER_MESSAGE = 5;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const ACCEPTED_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

type PanelId = ChatPanelState["id"];

interface ChatContextValue {
  panel: ChatPanelState;
  status: "idle" | "sending";
  capabilities: AgentCapabilities;
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
  void panelId;
  return {
    userId: "web_user",
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

function historyKey(panelId: PanelId): string {
  return `${HISTORY_KEY}_${panelId}`;
}

function clearPersistedHistory(panelId: PanelId) {
  try {
    localStorage.removeItem(historyKey(panelId));
  } catch {
    // Browser storage is best-effort; stale chat history should not block the UI.
  }
}

function canUseVision(capabilities: AgentCapabilities): boolean {
  return capabilities.vision_input ?? capabilities.vision;
}

function safeUploadName(file: File): string {
  const safeName = file.name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "image";
  return `temp/images/web/${Date.now()}-${Math.random().toString(16).slice(2, 10)}-${safeName}`;
}

function createPanel(panelId: PanelId): ChatPanelState {
  const savedSettings = readJson<Partial<ChatSettings>>(GLOBAL_SETTINGS_KEY, {});
  return {
    id: panelId,
    messages: [],
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

function persistSettings(panel: ChatPanelState) {
  localStorage.setItem(
    GLOBAL_SETTINGS_KEY,
    JSON.stringify({
      userId: panel.settings.userId,
      stream: panel.settings.stream,
      memory: panel.settings.memory,
    }),
  );
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const [panel, setPanel] = useState<ChatPanelState>(() => createPanel("single"));
  const [capabilities, setCapabilities] = useState<AgentCapabilities>(DEFAULT_CAPABILITIES);
  const socketsRef = useRef<Record<string, WebSocket>>({});

  const patchPanel = useCallback((panelId: PanelId, updater: (panel: ChatPanelState) => ChatPanelState) => {
    void panelId;
    setPanel((current) => updater(current));
  }, []);

  useEffect(() => {
    clearPersistedHistory("single");
  }, []);

  useEffect(() => {
    let cancelled = false;
    getAgentInfo()
      .then((info) => {
        if (cancelled) return;
        setCapabilities({
          ...DEFAULT_CAPABILITIES,
          ...(info.capabilities || {}),
        });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const updateSettings = useCallback(
    (panelId: PanelId, settings: Partial<ChatSettings>) => {
      void panelId;
      setPanel((current) => {
        const updatedPanel = {
          ...current,
          settings: { ...current.settings, ...settings },
        };
        persistSettings(updatedPanel);
        return updatedPanel;
      });
    },
    [],
  );

  const addImages = useCallback((panelId: PanelId, files: FileList | File[]) => {
    if (!canUseVision(capabilities)) return;
    const remainingSlots = Math.max(0, MAX_IMAGES_PER_MESSAGE - panel.pendingImages.length);
    const selectedFiles = Array.from(files)
      .filter((file) => ACCEPTED_IMAGE_TYPES.has(file.type) && file.size <= MAX_IMAGE_BYTES)
      .slice(0, remainingSlots);
    if (!selectedFiles.length) return;

    void Promise.all(
      selectedFiles.map(async (file) => {
        const uploaded = await uploadWorkspaceFile(file, safeUploadName(file));
        const previewUrl = workspaceBlobUrl(uploaded.path);
        patchPanel(panelId, (panel) => {
          if (panel.pendingImages.length >= MAX_IMAGES_PER_MESSAGE) return panel;
          return {
            ...panel,
            pendingImages: [...panel.pendingImages, previewUrl],
          };
        });
      }),
    ).catch(() => undefined);
  }, [capabilities, panel.pendingImages.length, patchPanel]);

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
      const textByMessageId = new Map<string, string>();
      const localMessageIds = new Set<string>([assistantId]);
      const remoteToLocalMessageId = new Map<string, string>();
      let claimedPlaceholder = false;

      const ensureAssistantMessage = (event: ChatEvent): string => {
        const remoteMessageId = event.message_id;
        if (remoteMessageId) {
          const existingLocalId = remoteToLocalMessageId.get(remoteMessageId);
          if (existingLocalId) return existingLocalId;

          const localId = claimedPlaceholder ? makeId("assistant") : assistantId;
          claimedPlaceholder = true;
          remoteToLocalMessageId.set(remoteMessageId, localId);
          localMessageIds.add(localId);

          if (localId !== assistantId) {
            patchPanel(panelId, (panel) => ({
              ...panel,
              messages: [...panel.messages, { id: localId, role: "assistant", content: "", pending: true }],
            }));
          }

          return localId;
        }

        claimedPlaceholder = true;
        return assistantId;
      };

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
            localMessageIds.has(message.id) && message.pending
              ? {
                  ...message,
                  pending: false,
                  error: Boolean(error),
                  content: error && !message.content ? `Error: ${error.message}` : message.content,
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

        if (parsed.type === "message_start") {
          const localId = ensureAssistantMessage(parsed);
          if (!textByMessageId.has(localId)) {
            textByMessageId.set(localId, "");
          }
          return;
        }

        if (parsed.type === "message_delta" && parsed.delta) {
          const localId = ensureAssistantMessage(parsed);
          const nextText = `${textByMessageId.get(localId) || ""}${parsed.delta}`;
          textByMessageId.set(localId, nextText);
          patchPanel(panelId, (panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === localId ? { ...message, content: nextText } : message,
            ),
          }));
          return;
        }

        if (parsed.type === "message_done") {
          const localId = ensureAssistantMessage(parsed);
          const nextText = parsed.content == null ? (textByMessageId.get(localId) || "") : String(parsed.content);
          textByMessageId.set(localId, nextText);
          patchPanel(panelId, (panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === localId ? { ...message, content: nextText, pending: false } : message,
            ),
          }));
          return;
        }

        if (parsed.message != null) {
          const localId = ensureAssistantMessage(parsed);
          const nextText = typeof parsed.message === "string" ? parsed.message : JSON.stringify(parsed.message, null, 2);
          textByMessageId.set(localId, nextText);
          patchPanel(panelId, (panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === localId ? { ...message, content: nextText, pending: false } : message,
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
      if (panel.sending) return;
      const currentPanel = panel;
      const images = canUseVision(capabilities) ? [...currentPanel.pendingImages] : [];
      if (!text && !images.length) return;
      const userMessage: ChatMessage = {
        id: makeId("user"),
        role: "user",
        content: text,
        images,
        imageCount: images.length || undefined,
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
      persistSettings(currentPanel);

      await runSocket(panelId, payload, assistantMessage.id).catch(() => undefined);
    },
    [capabilities, panel, patchPanel, runSocket],
  );

  const sendObservation = useCallback(
    async (panelId: PanelId, rawText: string) => {
      const text = rawText.trim();
      if (!text || panel.sending) return;
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
    [panel.sending, patchPanel],
  );

  const clearPanel = useCallback(
    (panelId: PanelId) => {
      patchPanel(panelId, (panel) => ({ ...panel, messages: [], pendingImages: [] }));
      clearPersistedHistory(panelId);
    },
    [patchPanel],
  );

  const clearVisiblePanels = useCallback(() => {
    clearPanel("single");
  }, [clearPanel]);

  const status: "idle" | "sending" = panel.sending ? "sending" : "idle";

  const value = useMemo(
    () => ({
      panel,
      status,
      capabilities,
      updateSettings,
      addImages,
      removeImage,
      sendMessage,
      sendObservation,
      clearPanel,
      clearVisiblePanels,
    }),
    [
      panel,
      status,
      capabilities,
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
