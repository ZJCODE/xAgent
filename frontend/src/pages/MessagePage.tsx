import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { Markdown } from "../components/Markdown";
import { getAgentInfo, getMessages } from "../lib/api";
import { classNames, formatTimestamp } from "../lib/format";
import type { MessageItem } from "../types";

const PAGE_SIZE = 50;

function storageDirectory(path: unknown): string {
  if (typeof path !== "string" || !path.trim()) return "";
  const normalized = path.trim();
  const slashIndex = normalized.lastIndexOf("/");
  if (slashIndex === -1) return normalized;
  if (slashIndex === normalized.length - 1) return normalized;
  return `${normalized.slice(0, slashIndex + 1)}`;
}

function roleClass(role: string): string {
  const normalized = role.toLowerCase();
  if (normalized.includes("user")) return "role-user";
  if (normalized.includes("assistant")) return "role-assistant";
  if (normalized.includes("system")) return "role-system";
  if (normalized.includes("tool")) return "role-tool";
  return "role-observation";
}

export function MessagePage() {
  const [messages, setMessages] = useState<MessageItem[]>([]);
  const [storagePath, setStoragePath] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = async (append = false) => {
    setLoading(true);
    setError("");
    try {
      const nextOffset = append ? offset : 0;
      const [data, info] = await Promise.all([
        getMessages(PAGE_SIZE, nextOffset),
        append ? Promise.resolve(null) : getAgentInfo().catch(() => null),
      ]);
      setMessages((current) => (append ? [...current, ...data.messages] : data.messages));
      setOffset(nextOffset + data.count);
      setHasMore(data.has_more);
      setStoragePath(storageDirectory(info?.message_storage?.path));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(false);
  }, []);

  return (
    <div className="console-page">
      <section className="console-toolbar">
        <div>
          <h2>Messages</h2>
          <p>{storagePath || "Message storage"}</p>
        </div>
        <div className="toolbar-actions">
          <button type="button" className="ghost-button icon-text-button" onClick={() => void load(false)}>
            <RefreshCw size={15} />
            Refresh
          </button>
        </div>
      </section>
      {error ? <div className="error-strip">{error}</div> : null}
      <div className="message-stream">
        {messages.length ? (
          messages.map((message, index) => (
            <article key={`${message.timestamp}-${index}`} className={classNames("message-row", roleClass(message.role))}>
              <div className="message-row-meta">
                <span className={classNames("meta-chip", roleClass(message.role))}>{message.role}</span>
                <span className="meta-chip">{message.type}</span>
                {message.sender_id ? <span className="meta-chip">{message.sender_id}</span> : null}
                <span className="meta-chip">{formatTimestamp(message.timestamp)}</span>
              </div>
              <Markdown content={message.content || ""} />
              {message.tool_call ? (
                <pre>{JSON.stringify(message.tool_call, null, 2)}</pre>
              ) : null}
            </article>
          ))
        ) : (
          <div className="empty-state">{loading ? "Loading..." : "No messages found"}</div>
        )}
        {hasMore ? (
          <button type="button" className="ghost-button icon-text-button mx-auto" onClick={() => void load(true)} disabled={loading}>
            Load more
          </button>
        ) : null}
      </div>
    </div>
  );
}
