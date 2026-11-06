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
import { getAgentInfo, stopChat, uploadWorkspaceFile, workspaceBlobUrl } from "../lib/api";
import { insertUserBeforeRunningReply, placeQueuedFollowup, placeSteeredUser } from "../lib/chatOrder";
import type { AgentCapabilities, AttachmentAsset, ChatEvent, ChatMessage, ChatPanelState, ChatSettings } from "../types";
import { makeId } from "../lib/format";

export const DEFAULT_WEB_USER_ID = "web_user";

const GLOBAL_SETTINGS_KEY = "xagent_web_settings";
const LEGACY_HISTORY_KEY = "xagent_chat_history_single";
const DEFAULT_CAPABILITIES: AgentCapabilities = {
  vision: true,
  vision_input: true,
  web_search: false,
  generate_image: false,
};
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_ATTACHMENTS_PER_MESSAGE = 10;
const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
const ACCEPTED_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
type SocketOptions = {
  followup?: boolean;
  userMessageId?: string;
};

interface ChatContextValue {
  panel: ChatPanelState;
  status: "idle" | "sending";
  capabilities: AgentCapabilities;
  currentTurnId: string | null;
  updateSettings: (settings: Partial<ChatSettings>) => void;
  addAttachments: (files: FileList | File[]) => void;
  removeAttachment: (index: number) => void;
  sendMessage: (text: string) => Promise<void>;
  stopTurn: () => Promise<void>;
  sendObservation: (text: string) => Promise<void>;
}

const ChatContext = createContext<ChatContextValue | null>(null);

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function clearLegacyHistory() {
  try {
    localStorage.removeItem(LEGACY_HISTORY_KEY);
  } catch {
    // Browser storage is best-effort; stale chat history should not block the UI.
  }
}

function canUseVision(capabilities: AgentCapabilities): boolean {
  return capabilities.vision_input ?? capabilities.vision;
}

function isImageAttachment(attachment: AttachmentAsset): boolean {
  return attachment.kind === "image" || Boolean(attachment.mime_type?.startsWith("image/"));
}

function attachmentBlobUrl(attachment: AttachmentAsset): string {
  return (
    attachment.blob_url ||
    (attachment.path ? workspaceBlobUrl(attachment.path) : "") ||
    (attachment.workspace_path ? workspaceBlobUrl(attachment.workspace_path) : "") ||
    ((attachment as { external_url?: string }).external_url || "")
  );
}

function attachmentImageUrls(attachments: AttachmentAsset[] = []): string[] {
  return attachments.filter(isImageAttachment).map(attachmentBlobUrl).filter(Boolean);
}

function imageAssetUrls(images: AttachmentAsset[] = []): string[] {
  return images.map(attachmentBlobUrl).filter(Boolean);
}

type ScheduledMessagePayload = {
  content?: unknown;
  attachments?: AttachmentAsset[];
  images?: AttachmentAsset[];
  image_count?: number;
  attachment_count?: number;
};

function scheduledMessagePayload(message: unknown): ScheduledMessagePayload | undefined {
  return message && typeof message === "object" ? (message as ScheduledMessagePayload) : undefined;
}

function pushMessageMeta(event: ChatEvent): string {
  if (event.type === "subconscious_message") return "Subconscious";
  return event.task?.title || "Scheduled";
}

function appendPushMessage(setPanel: (updater: (panel: ChatPanelState) => ChatPanelState) => void, event: ChatEvent) {
  if (event.type !== "scheduled_message" && event.type !== "subconscious_message") return;
  const scheduledMessage = scheduledMessagePayload(event.message);
  const fallbackMessageContent = typeof event.message === "string" ? event.message : scheduledMessage?.content;
  const content = String(event.content ?? fallbackMessageContent ?? "").trim();
  const attachments = Array.isArray(event.attachments)
    ? event.attachments
    : Array.isArray(scheduledMessage?.attachments)
      ? scheduledMessage.attachments
      : undefined;
  const imageUrls = attachments
    ? attachmentImageUrls(attachments)
    : Array.isArray(scheduledMessage?.images)
      ? imageAssetUrls(scheduledMessage.images)
      : undefined;
  const imageCount = imageUrls ? imageUrls.length || undefined : scheduledMessage?.image_count;
  const attachmentCount = attachments ? attachments.length || undefined : scheduledMessage?.attachment_count;
  if (!content && !imageUrls?.length && !attachments?.length && !imageCount && !attachmentCount) return;
  setPanel((current) => ({
    ...current,
    messages: [
      ...current.messages,
      {
        id: makeId(event.type === "subconscious_message" ? "subconscious" : "scheduled"),
        role: "assistant",
        content,
        images: imageUrls,
        imageCount,
        attachments,
        attachmentCount,
        meta: pushMessageMeta(event),
      },
    ],
  }));
}

const TASKS_WS_MAX_RECONNECT_MS = 30_000;

function createPanel(): ChatPanelState {
  const savedSettings = readJson<Partial<ChatSettings>>(GLOBAL_SETTINGS_KEY, {});
  return {
    messages: [],
    pendingAttachments: [],
    settings: {
      userId: DEFAULT_WEB_USER_ID,
      stream: true,
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
    }),
  );
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const [panel, setPanel] = useState<ChatPanelState>(createPanel);
  const [capabilities, setCapabilities] = useState<AgentCapabilities>(DEFAULT_CAPABILITIES);
  const [currentTurnId, setCurrentTurnId] = useState<string | null>(null);
  const sendingRef = useRef(false);
  const turnIdRef = useRef<string | null>(null);

  useEffect(() => {
    clearLegacyHistory();
  }, []);

  useEffect(() => {
    const userId = new URLSearchParams(window.location.search).get("user_id")?.trim();
    if (!userId) return;
    setPanel((current) => {
      const updatedPanel = {
        ...current,
        settings: { ...current.settings, userId },
      };
      persistSettings(updatedPanel);
      return updatedPanel;
    });
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

  useEffect(() => {
    const userId = panel.settings.userId || DEFAULT_WEB_USER_ID;
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let reconnectAttempt = 0;

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== undefined) return;
      const delay = Math.min(1000 * 2 ** reconnectAttempt, TASKS_WS_MAX_RECONNECT_MS);
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined;
        connect();
      }, delay);
    };

    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(webSocketUrl(`/ws/tasks?user_id=${encodeURIComponent(userId)}`));

      socket.addEventListener("open", () => {
        reconnectAttempt = 0;
      });

      socket.addEventListener("message", (event) => {
        let parsed: ChatEvent;
        try {
          parsed = JSON.parse(event.data) as ChatEvent;
        } catch {
          return;
        }
        appendPushMessage(setPanel, parsed);
      });

      socket.addEventListener("close", () => {
        socket = null;
        if (!disposed) scheduleReconnect();
      });

      socket.addEventListener("error", () => undefined);
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) {
        window.clearTimeout(reconnectTimer);
      }
      if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
        socket.close(1000);
      }
    };
  }, [panel.settings.userId]);

  const updateSettings = useCallback(
    (settings: Partial<ChatSettings>) => {
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

  const addAttachments = useCallback((files: FileList | File[]) => {
    const remainingSlots = Math.max(0, MAX_ATTACHMENTS_PER_MESSAGE - panel.pendingAttachments.length);
    const selectedFiles = Array.from(files)
      .filter((file) => {
        if (file.type.startsWith("image/")) {
          return ACCEPTED_IMAGE_TYPES.has(file.type) && file.size <= MAX_IMAGE_BYTES;
        }
        return file.size <= MAX_ATTACHMENT_BYTES;
      })
      .slice(0, remainingSlots);
    if (!selectedFiles.length) return;

    void Promise.all(
      selectedFiles.map(async (file) => {
        const uploaded = await uploadWorkspaceFile(file);
        const blobUrl = uploaded.blob_url || workspaceBlobUrl(uploaded.path);
        const mimeType = uploaded.mime_type || file.type || "application/octet-stream";
        const attachment: AttachmentAsset = {
          kind: mimeType.startsWith("image/") ? "image" : "file",
          path: uploaded.path,
          workspace_path: uploaded.path,
          blob_url: blobUrl,
          mime_type: mimeType,
          size_bytes: uploaded.size,
          file_name: uploaded.name || file.name,
          original_name: file.name,
          client: "web",
        };
        setPanel((panel) => {
          if (panel.pendingAttachments.length >= MAX_ATTACHMENTS_PER_MESSAGE) return panel;
          return {
            ...panel,
            pendingAttachments: [...panel.pendingAttachments, attachment],
          };
        });
      }),
    ).catch(() => undefined);
  }, [panel.pendingAttachments.length]);

  const removeAttachment = useCallback(
    (index: number) => {
      setPanel((panel) => ({
        ...panel,
        pendingAttachments: panel.pendingAttachments.filter((_, itemIndex) => itemIndex !== index),
      }));
    },
    [],
  );

  const runSocket = useCallback((payload: Record<string, unknown>, assistantId: string, options?: SocketOptions) => {
    return new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(webSocketUrl("/ws/chat"));
      let settled = false;
      let aborted = false;
      const textByMessageId = new Map<string, string>();
      const localMessageIds = new Set<string>([assistantId]);
      const remoteToLocalMessageId = new Map<string, string>();
      let claimedPlaceholder = false;
      let steeredStream = false;
      let activeToolCalls = 0;
      const followup = Boolean(options?.followup);
      const followupUserId = options?.userMessageId;

      const ensureAssistantMessage = (event: ChatEvent): string => {
        const remoteMessageId = event.message_id;
        if (remoteMessageId) {
          const existingLocalId = remoteToLocalMessageId.get(remoteMessageId);
          if (existingLocalId) return existingLocalId;

          const localId = claimedPlaceholder ? makeId("assistant") : assistantId;
          claimedPlaceholder = true;
          remoteToLocalMessageId.set(remoteMessageId, localId);
          localMessageIds.add(localId);

          setPanel((panel) => {
            if (panel.messages.some((message) => message.id === localId)) return panel;
            return {
              ...panel,
              messages: [...panel.messages, { id: localId, role: "assistant", content: "", pending: true }],
            };
          });

          return localId;
        }

        claimedPlaceholder = true;
        return assistantId;
      };

      const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        if (!followup) {
          turnIdRef.current = null;
          sendingRef.current = false;
          setCurrentTurnId(null);
        }
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
          socket.close(1000);
        }
        setPanel((panel) => {
          let messages = panel.messages;
          if (followup && error && !steeredStream && followupUserId) {
            messages = placeQueuedFollowup(messages, followupUserId, {
              id: assistantId,
              role: "assistant",
              content: "",
              pending: true,
            });
          }
          return {
            ...panel,
            sending: followup ? panel.sending : false,
            progress: followup ? panel.progress : undefined,
            messages: messages.flatMap((message) => {
              if (!localMessageIds.has(message.id) || !message.pending) return [message];
              if ((aborted || steeredStream) && !message.content) return [];
              return [{
                ...message,
                pending: false,
                error: Boolean(error),
                content: error && !message.content ? `Error: ${error.message}` : message.content,
              }];
            }),
          };
        });
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

        if (parsed.type === "accepted") {
          if (!followup) {
            turnIdRef.current = parsed.turn_id || null;
            setCurrentTurnId(parsed.turn_id || null);
          } else if (followupUserId) {
            const queuedAssistant: ChatMessage = {
              id: assistantId,
              role: "assistant",
              content: "",
              pending: true,
            };
            setPanel((panel) => ({
              ...panel,
              messages: placeQueuedFollowup(panel.messages, followupUserId, queuedAssistant),
            }));
          }
          return;
        }
        if (parsed.type === "steered") {
          steeredStream = true;
          if (followupUserId) {
            setPanel((panel) => ({
              ...panel,
              messages: placeSteeredUser(panel.messages, followupUserId),
            }));
          }
          return;
        }
        if (parsed.type === "aborted") {
          aborted = true;
          return;
        }

        if (!followup && parsed.type === "tool_call") {
          activeToolCalls += 1;
          setPanel((panel) => ({ ...panel, progress: "Working with tools…" }));
          return;
        }
        if (!followup && parsed.type === "tool_result") {
          activeToolCalls = Math.max(0, activeToolCalls - 1);
          if (!activeToolCalls) {
            setPanel((panel) => ({ ...panel, progress: "Reviewing results…" }));
          }
          return;
        }

        if (parsed.type === "error" || parsed.error) {
          finish(new Error(parsed.error || "WebSocket chat failed."));
          return;
        }

        if (parsed.type === "message_start") {
          if (!followup) setPanel((panel) => ({ ...panel, progress: "Writing a response…" }));
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
          setPanel((panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === localId ? { ...message, content: nextText } : message,
            ),
          }));
          return;
        }

        if (parsed.type === "message_done") {
          if (!followup && parsed.phase === "preface") {
            setPanel((panel) => ({ ...panel, progress: "Continuing…" }));
          }
          const localId = ensureAssistantMessage(parsed);
          const nextText = parsed.content == null ? (textByMessageId.get(localId) || "") : String(parsed.content);
          const attachments = Array.isArray(parsed.attachments) ? parsed.attachments : undefined;
          const imageUrls = attachments ? attachmentImageUrls(attachments) : undefined;
          textByMessageId.set(localId, nextText);
          setPanel((panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === localId
                ? {
                    ...message,
                    content: nextText,
                    pending: false,
                    attachments: attachments ?? message.attachments,
                    attachmentCount: attachments ? attachments.length || undefined : message.attachmentCount,
                    images: imageUrls ?? message.images,
                    imageCount: imageUrls ? imageUrls.length || undefined : message.imageCount,
                  }
                : message,
            ),
          }));
          return;
        }

        if (parsed.message != null) {
          const localId = ensureAssistantMessage(parsed);
          const nextText = typeof parsed.message === "string" ? parsed.message : JSON.stringify(parsed.message, null, 2);
          textByMessageId.set(localId, nextText);
          setPanel((panel) => ({
            ...panel,
            messages: panel.messages.map((message) =>
              message.id === localId ? { ...message, content: nextText, pending: false } : message,
            ),
          }));
          return;
        }

        if (parsed.type === "done") {
          if (parsed.steered && !followup) return;
          finish();
        }
      });

      socket.addEventListener("error", () => finish(new Error("WebSocket connection failed.")));
      socket.addEventListener("close", () => {
        if (!settled) finish(new Error("WebSocket connection closed before completion."));
      });
    });
  }, []);

  const sendMessage = useCallback(
    async (rawText: string) => {
      const text = rawText.trim();
      const currentPanel = panel;
      const attachments = [...currentPanel.pendingAttachments];
      const images = attachmentImageUrls(attachments);
      if (!text && !attachments.length) return;
      const userMessage: ChatMessage = {
        id: makeId("user"),
        role: "user",
        content: text,
        images,
        imageCount: images.length || undefined,
        attachments,
        attachmentCount: attachments.length || undefined,
      };
      const payload: Record<string, unknown> = {
        user_id: currentPanel.settings.userId,
        user_message: text,
        stream: currentPanel.settings.stream,
        event_id: userMessage.id,
      };
      if (attachments.length) payload.attachments = attachments;
      if (canUseVision(capabilities)) {
        if (images.length === 1) payload.image_source = images[0];
        if (images.length > 1) payload.image_source = images;
      }
      persistSettings(currentPanel);

      if (sendingRef.current) {
        const assistantMessage: ChatMessage = {
          id: makeId("assistant"),
          role: "assistant",
          content: "",
          pending: true,
        };
        setPanel((current) => ({
          ...current,
          pendingAttachments: [],
          messages: insertUserBeforeRunningReply(current.messages, userMessage),
        }));
        await runSocket(payload, assistantMessage.id, {
          followup: true,
          userMessageId: userMessage.id,
        }).catch(() => undefined);
        return;
      }

      const assistantMessage: ChatMessage = {
        id: makeId("assistant"),
        role: "assistant",
        content: "",
        pending: true,
      };
      sendingRef.current = true;
      setPanel((current) => ({
        ...current,
        pendingAttachments: [],
        sending: true,
        progress: "Thinking…",
        messages: [...current.messages, userMessage, assistantMessage],
      }));
      await runSocket(payload, assistantMessage.id).catch(() => undefined);
    },
    [capabilities, panel, runSocket],
  );

  const stopTurn = useCallback(async () => {
    const turnId = turnIdRef.current;
    if (!sendingRef.current || !turnId) return;
    await stopChat(turnId).catch(() => undefined);
  }, []);

  const sendObservation = useCallback(
    async (rawText: string) => {
      const text = rawText.trim();
      if (!text || panel.sending) return;
      const observationMessage: ChatMessage = {
        id: makeId("observation"),
        role: "observation",
        content: text,
      };
      setPanel((panel) => ({
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
            socket.send(JSON.stringify({ context: text, source: "web", event_type: "observation", event_id: observationMessage.id }));
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
        setPanel((panel) => ({
          ...panel,
          messages: [...panel.messages, { id: makeId("error"), role: "assistant", content: `Error: ${message}`, error: true }],
        }));
      } finally {
        setPanel((panel) => ({ ...panel, sending: false }));
      }
    },
    [panel.sending],
  );

  const status: "idle" | "sending" = panel.sending ? "sending" : "idle";

  const value = useMemo(
    () => ({
      panel,
      status,
      capabilities,
      currentTurnId,
      updateSettings,
      addAttachments,
      removeAttachment,
      sendMessage,
      stopTurn,
      sendObservation,
    }),
    [
      panel,
      status,
      capabilities,
      currentTurnId,
      updateSettings,
      addAttachments,
      removeAttachment,
      sendMessage,
      stopTurn,
      sendObservation,
    ],
  );

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export function useChat() {
  const value = useContext(ChatContext);
  if (!value) throw new Error("useChat must be used inside ChatProvider");
  return value;
}
