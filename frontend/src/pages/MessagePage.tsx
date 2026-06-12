import { FileIcon, RefreshCw, Search, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Markdown } from "../components/Markdown";
import { getAgentInfo, getMessages, searchMessages } from "../lib/api";
import { classNames, formatBytes, formatTimestamp } from "../lib/format";
import type { AttachmentAsset, MessageItem, MessageSearchResult } from "../types";

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

function isSearchResult(message: MessageItem | MessageSearchResult): message is MessageSearchResult {
  return Array.isArray((message as MessageSearchResult).matched_in);
}

function messageImageUrls(message: MessageItem | MessageSearchResult): string[] {
  const attachmentImages = (message.attachments || [])
    .filter((attachment) => attachment.kind === "image" || Boolean(attachment.mime_type?.startsWith("image/")))
    .map((attachment) => attachment.blob_url || (attachment.path ? workspaceBlobUrlFromPath(attachment.path) : ""));
  return [...(message.images || [])
    .map((image) => image.blob_url || image.external_url || "")
    .filter(Boolean), ...attachmentImages]
    .filter((url, index, urls) => url && urls.indexOf(url) === index);
}

function workspaceBlobUrlFromPath(path: string): string {
  return `/api/workspace/blob?path=${encodeURIComponent(path)}`;
}

function attachmentUrl(attachment: AttachmentAsset): string {
  return attachment.blob_url || (attachment.path ? workspaceBlobUrlFromPath(attachment.path) : "");
}

function attachmentLabel(attachment: AttachmentAsset): string {
  return attachment.caption || attachment.original_name || attachment.file_name || attachment.path?.split("/").pop() || "Attachment";
}

function messageFileAttachments(message: MessageItem | MessageSearchResult): AttachmentAsset[] {
  return (message.attachments || []).filter((attachment) => !(attachment.kind === "image" || attachment.mime_type?.startsWith("image/")));
}

function FileArchivePreview({ attachment }: { attachment: AttachmentAsset }) {
  const url = attachmentUrl(attachment);
  const label = attachmentLabel(attachment);
  const size = formatBytes(attachment.size_bytes);
  return (
    <a className="file-preview-bubble archive-file-preview" href={url} target="_blank" rel="noreferrer" download={label}>
      <span className="file-preview-icon" aria-hidden="true">
        <FileIcon size={34} />
      </span>
      <span className="file-preview-meta">
        <span className="file-preview-name">{label}</span>
        {size ? <span className="file-preview-size">{size}</span> : null}
      </span>
    </a>
  );
}

export function MessagePage() {
  const [messages, setMessages] = useState<MessageItem[]>([]);
  const [storagePath, setStoragePath] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MessageSearchResult[]>([]);
  const [searchActive, setSearchActive] = useState(false);
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

  const runSearch = async () => {
    const text = query.trim();
    if (!text) return;

    setLoading(true);
    setError("");
    setSearchActive(true);
    try {
      const data = await searchMessages(text);
      setResults(data.results || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const activeMessages: Array<MessageItem | MessageSearchResult> = searchActive ? results : messages;

  return (
    <div className="console-page">
      <section className="console-toolbar">
        <div className="min-w-0">
          <h2>Messages</h2>
          <p>{storagePath || "Message storage"}</p>
        </div>
        <div className="search-control">
          <input
            className="search-input"
            placeholder="Search messages"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void runSearch();
            }}
          />
          <button type="button" className="ghost-button icon-text-button" onClick={runSearch}>
            <Search size={15} />
            Search
          </button>
          <button
            type="button"
            className="ghost-button icon-button"
            onClick={() => {
              setQuery("");
              setResults([]);
              setSearchActive(false);
            }}
            title="Clear search"
          >
            <X size={16} />
          </button>
          <button
            type="button"
            className="ghost-button icon-button"
            onClick={() => {
              if (searchActive && query.trim()) {
                void runSearch();
                return;
              }
              void load(false);
            }}
            title="Refresh"
          >
            <RefreshCw size={15} />
          </button>
        </div>
      </section>
      {error ? <div className="error-strip">{error}</div> : null}
      <div className="message-stream">
        {activeMessages.length ? (
          activeMessages.map((message, index) => {
            const imageUrls = messageImageUrls(message);
            const files = messageFileAttachments(message);
            return (
              <article key={`${message.timestamp}-${index}`} className={classNames("message-row", roleClass(message.role))}>
                <div className="message-row-meta">
                  <span className={classNames("meta-chip", roleClass(message.role))}>{message.role}</span>
                  <span className="meta-chip">{message.type}</span>
                  {message.sender_id ? <span className="meta-chip">{message.sender_id}</span> : null}
                  <span className="meta-chip">{formatTimestamp(message.timestamp)}</span>
                  {isSearchResult(message)
                    ? message.matched_in.map((match) => (
                        <span key={`${message.timestamp}-${match}`} className="meta-chip">
                          {match}
                        </span>
                      ))
                    : null}
                </div>
                {message.content ? <Markdown content={message.content} renderImages={false} /> : null}
                {imageUrls.length ? (
                  <div className="message-archive-media">
                    {imageUrls.map((url, imageIndex) => (
                      <a
                        key={`${message.timestamp}-${imageIndex}-${url}`}
                        href={url}
                        target="_blank"
                        rel="noreferrer"
                        className="message-image-link archive-image-link"
                      >
                        <img src={url} alt="" className="message-image-preview" />
                      </a>
                    ))}
                  </div>
                ) : null}
                {files.length ? (
                  <div className="message-archive-media">
                    {files.map((attachment, attachmentIndex) => (
                      <FileArchivePreview
                        key={`${message.timestamp}-${attachmentIndex}-${attachmentUrl(attachment)}`}
                        attachment={attachment}
                      />
                    ))}
                  </div>
                ) : null}
                {message.tool_call ? (
                  <pre>{JSON.stringify(message.tool_call, null, 2)}</pre>
                ) : null}
              </article>
            );
          })
        ) : (
          <div className="empty-state">{loading ? "Loading..." : searchActive ? "No matching messages" : "No messages found"}</div>
        )}
        {!searchActive && hasMore ? (
          <button type="button" className="ghost-button icon-text-button mx-auto" onClick={() => void load(true)} disabled={loading}>
            Load more
          </button>
        ) : null}
      </div>
    </div>
  );
}
