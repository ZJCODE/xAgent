import {
  Bot,
  CircleUserRound,
  Eye,
  Filter,
  MessageSquareText,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Markdown } from "../components/Markdown";
import {
  Button,
  EmptyState,
  IconButton,
  PageShell,
  PageToolbar,
  StatusBadge,
} from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { getMessages } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { PersistedMessage } from "../types";

const PAGE_SIZE = 40;

function roleLabel(message: PersistedMessage): string {
  if (message.role === "assistant") return "Agent";
  if (message.role === "environment") return "Observation";
  const sender = message.sender_id || "";
  if (sender === "owner") return "You";
  return sender && !/^[a-f0-9-]{24,}$/i.test(sender) ? sender : "Person";
}

function roleIcon(message: PersistedMessage) {
  if (message.role === "assistant") return <Bot size={16} />;
  if (message.role === "environment") return <Eye size={16} />;
  return <CircleUserRound size={16} />;
}

export function MessagesPage() {
  const { selectedAgent } = useAgentSession();
  const [messages, setMessages] = useState<PersistedMessage[]>([]);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [role, setRole] = useState("");
  const [source, setSource] = useState("");
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async ({
    append = false,
    offset = 0,
  }: {
    append?: boolean;
    offset?: number;
  } = {}) => {
    setLoading(true);
    setError("");
    try {
      const data = await getMessages({
        limit: PAGE_SIZE,
        offset: append ? offset : 0,
        query: appliedQuery,
        role,
        source,
      });
      setMessages((current) => append ? [...current, ...data.messages] : data.messages);
      setTotal(data.total);
      setHasMore(data.has_more);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      if (!append) setMessages([]);
    } finally {
      setLoading(false);
    }
  }, [appliedQuery, role, source]);

  useEffect(() => {
    void load();
  }, [load, selectedAgent]);

  const submitSearch = (event?: FormEvent) => {
    event?.preventDefault();
    setAppliedQuery(query.trim());
  };

  const clearSearch = () => {
    setQuery("");
    setAppliedQuery("");
  };

  return (
    <PageShell className="messages-page">
      <PageToolbar
        eyebrow="Durable timeline"
        title="Messages"
        subtitle={`${total.toLocaleString()} persisted records from every source`}
        actions={
          <IconButton type="button" aria-label="Refresh messages" title="Refresh messages" onClick={() => void load()}>
            <RefreshCw size={16} />
          </IconButton>
        }
      />

      <div className="page-body archive-page-body">
        <form className="archive-filterbar" onSubmit={submitSearch}>
          <div className="archive-search">
            <Search size={16} />
            <input
              value={query}
              aria-label="Search message content"
              placeholder="Search the message timeline"
              onChange={(event) => setQuery(event.target.value)}
            />
            {query ? (
              <IconButton type="button" aria-label="Clear search" title="Clear search" onClick={clearSearch}>
                <X size={14} />
              </IconButton>
            ) : null}
          </div>
          <label className="filter-select">
            <Filter size={14} />
            <select aria-label="Filter by role" value={role} onChange={(event) => setRole(event.target.value)}>
              <option value="">All roles</option>
              <option value="user">People</option>
              <option value="assistant">Agent</option>
              <option value="environment">Observations</option>
            </select>
          </label>
          <label className="filter-select">
            <MessageSquareText size={14} />
            <select aria-label="Filter by source" value={source} onChange={(event) => setSource(event.target.value)}>
              <option value="">All sources</option>
              <option value="web">Web</option>
              <option value="cli">CLI</option>
              <option value="scheduler">Scheduler</option>
              <option value="api">API</option>
              <option value="feishu">Feishu</option>
              <option value="weixin">Weixin</option>
              <option value="voice">Voice</option>
            </select>
          </label>
          <Button type="submit" variant="primary"><Search size={14} />Search</Button>
        </form>

        {error ? <div className="error-strip">{error}</div> : null}

        {messages.length ? (
          <div className="timeline-list">
            {messages.map((message) => (
              <article className={`timeline-message role-${message.role}`} key={message.id}>
                <div className="timeline-avatar">{roleIcon(message)}</div>
                <div className="timeline-message-body">
                  <header>
                    <div>
                      <strong>{roleLabel(message)}</strong>
                      <StatusBadge tone={message.role === "assistant" ? "info" : message.role === "environment" ? "muted" : "neutral"}>
                        {message.role}
                      </StatusBadge>
                      {message.source ? <span className="data-chip">{message.source}</span> : null}
                      {message.room_name ? <span className="data-chip">{message.room_name}</span> : null}
                    </div>
                    <time>{formatTimestamp(message.timestamp)}</time>
                  </header>
                  <Markdown content={message.content || "—"} renderImages={false} />
                  {message.images?.length ? (
                    <div className="message-asset-note">
                      {message.images.length} image {message.images.length === 1 ? "attachment" : "attachments"}
                    </div>
                  ) : null}
                </div>
              </article>
            ))}
            {hasMore ? (
              <Button
                type="button"
                className="load-more-button"
                disabled={loading}
                onClick={() => void load({ append: true, offset: messages.length })}
              >
                {loading ? "Loading…" : "Load older messages"}
              </Button>
            ) : null}
          </div>
        ) : (
          <EmptyState icon={<MessageSquareText size={22} />} title={loading ? "Loading messages…" : appliedQuery ? "No matching messages" : "No messages yet"}>
            {error ? "The Runtime must be running to read its timeline." : "Messages from every source will appear here in newest-first order."}
          </EmptyState>
        )}
      </div>
    </PageShell>
  );
}
