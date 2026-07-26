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
import { makeId } from "../lib/format";
import { getMessages } from "../lib/api";
import type { ChatEvent, ChatMessage, ChatPanelState } from "../types";
import { useAgentSession } from "./AgentSessionContext";

interface ChatContextValue {
  panel: ChatPanelState;
  sendMessage: (text: string) => Promise<void>;
  sendObservation: (text: string) => Promise<void>;
  clear: () => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

function socketUrl(path: string): string {
  const url = new URL(path, window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function initialPanel(): ChatPanelState {
  return {
    id: "single",
    messages: [],
    sending: false,
  };
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const { selectedAgent } = useAgentSession();
  const [panel, setPanel] = useState<ChatPanelState>(initialPanel);
  const activeSocket = useRef<WebSocket | null>(null);
  const previousAgent = useRef(selectedAgent);
  const seenTaskMessages = useRef<Set<number>>(new Set());

  useEffect(() => {
    if (previousAgent.current === selectedAgent) return;
    previousAgent.current = selectedAgent;
    activeSocket.current?.close(1000);
    activeSocket.current = null;
    seenTaskMessages.current.clear();
    setPanel((current) => ({ ...current, messages: [], sending: false }));
  }, [selectedAgent]);

  const syncTaskResults = useCallback(async () => {
    if (!selectedAgent) return;
    try {
      const result = await getMessages({
        limit: 20,
        role: "assistant",
        source: "scheduler",
      });
      const fresh = result.messages
        .slice()
        .reverse()
        .filter((message) => {
          if (seenTaskMessages.current.has(message.id)) return false;
          seenTaskMessages.current.add(message.id);
          return true;
        })
        .map<ChatMessage>((message) => ({
          id: `task-result-${message.id}`,
          role: "assistant",
          content: message.content,
          source: "scheduler",
        }));
      if (fresh.length) {
        setPanel((current) => ({
          ...current,
          messages: [...current.messages, ...fresh],
        }));
      }
    } catch {
      // Runtime availability is already surfaced by the global connectivity state.
    }
  }, [selectedAgent]);

  useEffect(() => {
    void syncTaskResults();
    const interval = window.setInterval(() => void syncTaskResults(), 3000);
    return () => window.clearInterval(interval);
  }, [syncTaskResults]);

  const run = useCallback((path: string, payload: Record<string, unknown>, assistantId?: string) => {
    return new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(socketUrl(path));
      activeSocket.current = socket;
      let settled = false;
      let text = "";

      const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        if (activeSocket.current === socket) activeSocket.current = null;
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
          socket.close(1000);
        }
        setPanel((current) => ({
          ...current,
          sending: false,
          messages: assistantId
            ? current.messages.map((message) =>
                message.id === assistantId
                  ? {
                      ...message,
                      pending: false,
                      error: Boolean(error),
                      content: error && !message.content ? `Error: ${error.message}` : message.content,
                    }
                  : message,
              )
            : current.messages,
        }));
        error ? reject(error) : resolve();
      };

      socket.addEventListener("open", () => socket.send(JSON.stringify(payload)));
      socket.addEventListener("message", (raw) => {
        let event: ChatEvent;
        try {
          event = JSON.parse(raw.data) as ChatEvent;
        } catch {
          finish(new Error("Invalid runtime response."));
          return;
        }
        if (event.type === "error" || event.error) {
          finish(new Error(event.error || "Runtime request failed."));
          return;
        }
        if (assistantId && event.type === "message_delta") {
          text += event.delta || "";
        }
        if (assistantId && event.type === "message_done") {
          text = event.content == null ? text : String(event.content);
        }
        if (assistantId && (event.type === "message_delta" || event.type === "message_done")) {
          setPanel((current) => ({
            ...current,
            messages: current.messages.map((message) =>
              message.id === assistantId ? { ...message, content: text } : message,
            ),
          }));
        }
        if (event.type === "done") finish();
      });
      socket.addEventListener("error", () => finish(new Error("Cannot connect to the local runtime.")));
      socket.addEventListener("close", () => {
        if (!settled) finish(new Error("Runtime connection closed before completion."));
      });
    });
  }, []);

  const sendMessage = useCallback(async (rawText: string) => {
    const text = rawText.trim();
    if (!text || panel.sending) return;
    const user: ChatMessage = { id: makeId("user"), role: "user", content: text };
    const assistant: ChatMessage = {
      id: makeId("assistant"),
      role: "assistant",
      content: "",
      pending: true,
    };
    setPanel((current) => ({
      ...current,
      sending: true,
      messages: [...current.messages, user, assistant],
    }));
    await run(
      "/ws/chat",
      {
        user_message: text,
        stream: true,
      },
      assistant.id,
    ).catch(() => undefined);
  }, [panel.sending, run]);

  const sendObservation = useCallback(async (rawText: string) => {
    const text = rawText.trim();
    if (!text || panel.sending) return;
    setPanel((current) => ({
      ...current,
      sending: true,
      messages: [
        ...current.messages,
        { id: makeId("observation"), role: "observation", content: text },
      ],
    }));
    await run("/ws/observe", {
      context: text,
      source: "web",
      event_type: "observation",
    }).catch(() => undefined);
  }, [panel.sending, run]);

  const clear = useCallback(() => {
    activeSocket.current?.close(1000);
    activeSocket.current = null;
    setPanel((current) => ({ ...current, messages: [], sending: false }));
  }, []);

  const value = useMemo(
    () => ({ panel, sendMessage, sendObservation, clear }),
    [clear, panel, sendMessage, sendObservation],
  );

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export function useChat() {
  const value = useContext(ChatContext);
  if (!value) throw new Error("useChat must be used inside ChatProvider");
  return value;
}
