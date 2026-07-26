import { Eye, Send, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Markdown } from "../components/Markdown";
import { Button, EmptyState, IconButton, PageShell, PageToolbar } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { useChat } from "../context/ChatContext";
import { classNames } from "../lib/format";

export function ChatPage() {
  const { selectedAgent } = useAgentSession();
  const { panel, sendMessage, sendObservation, clear } = useChat();
  const [message, setMessage] = useState("");
  const [observation, setObservation] = useState("");
  const [showObservation, setShowObservation] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [panel.messages]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!message.trim() || panel.sending) return;
    const value = message;
    setMessage("");
    void sendMessage(value);
  };

  const submitObservation = (event: FormEvent) => {
    event.preventDefault();
    if (!observation.trim() || panel.sending) return;
    const value = observation;
    setObservation("");
    void sendObservation(value);
  };

  return (
    <PageShell className="chat-page">
      <PageToolbar
        eyebrow="Direct conversation"
        title="Chat"
        subtitle={selectedAgent ? `Talk to ${selectedAgent} on the shared Agent timeline` : "One durable timeline"}
        actions={
          <IconButton type="button" title="Clear local view" aria-label="Clear local view" onClick={clear}>
            <Trash2 size={15} />
          </IconButton>
        }
      />

      <div className="chat-scroll" ref={scrollRef}>
        {panel.messages.length ? (
          panel.messages.map((item) => (
            <div
              key={item.id}
              className={classNames("chat-message-group", item.role === "user" && "from-user")}
            >
              <div
                className={classNames(
                  "message-bubble",
                  item.role === "user"
                    ? "user-bubble"
                    : item.role === "observation"
                      ? "observation-bubble"
                      : "assistant-bubble",
                  item.error && "error-bubble",
                )}
              >
                <div className="message-label">
                  {item.role === "user"
                    ? "You"
                    : item.role === "observation"
                      ? "Observation"
                      : item.source === "scheduler"
                        ? "Task result"
                        : "xAgent"}
                </div>
                {item.pending && !item.content ? <span>Thinking…</span> : <Markdown content={item.content} />}
                {item.pending && item.content ? <span className="typing-cursor" /> : null}
              </div>
            </div>
          ))
        ) : (
          <EmptyState title={`Talk to ${selectedAgent || "your Agent"}`}>
            This is a direct Web conversation. The Agent can recall the same experiences it receives from every other channel.
          </EmptyState>
        )}
      </div>

      {showObservation ? (
        <form className="chat-observe-form" onSubmit={submitObservation}>
          <Eye size={15} />
          <input
            value={observation}
            placeholder="Add an observation without asking for a reply"
            onChange={(event) => setObservation(event.target.value)}
          />
          <Button type="submit" disabled={panel.sending || !observation.trim()}>Observe</Button>
        </form>
      ) : null}

      <form className="chat-composer" onSubmit={submit}>
        <IconButton
          type="button"
          title="Add observation"
          aria-label="Add observation"
          onClick={() => setShowObservation((value) => !value)}
        >
          <Eye size={16} />
        </IconButton>
        <textarea
          value={message}
          rows={1}
          placeholder={`Message ${selectedAgent || "xAgent"}…`}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit(event);
            }
          }}
        />
        <Button type="submit" variant="primary" disabled={panel.sending || !message.trim()}>
          <Send size={15} />
          Send
        </Button>
      </form>
    </PageShell>
  );
}
