import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { Markdown } from "../components/Markdown";
import { getMessages } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { MessageItem } from "../types";

const PAGE_SIZE = 50;

export function MessagePage() {
  const [messages, setMessages] = useState<MessageItem[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = async (append = false) => {
    setLoading(true);
    setError("");
    try {
      const nextOffset = append ? offset : 0;
      const data = await getMessages(PAGE_SIZE, nextOffset);
      setMessages((current) => (append ? [...current, ...data.messages] : data.messages));
      setOffset(nextOffset + data.count);
      setTotal(data.total);
      setHasMore(data.has_more);
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
          <p>{total ? `${total} stored events` : "Short-term message stream"}</p>
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
            <article key={`${message.timestamp}-${index}`} className="message-row">
              <div className="message-row-meta">
                <span>{message.role}</span>
                <span>{message.type}</span>
                <span>{message.sender_id}</span>
                <span>{formatTimestamp(message.timestamp)}</span>
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
