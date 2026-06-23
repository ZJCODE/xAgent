import { Eye, FileIcon, Paperclip, Send, Trash2, X } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { Markdown } from "../components/Markdown";
import { Button, EmptyState, IconButton } from "../components/ui";
import { useChat } from "../context/ChatContext";
import { classNames, formatBytes } from "../lib/format";
import type { AttachmentAsset, ChatPanelState } from "../types";

function attachmentUrl(attachment: AttachmentAsset): string {
  return attachment.blob_url || (attachment.path ? `/api/workspace/blob?path=${encodeURIComponent(attachment.path)}` : "");
}

function attachmentLabel(attachment: AttachmentAsset): string {
  return attachment.caption || attachment.original_name || attachment.file_name || attachment.path?.split("/").pop() || "Attachment";
}

function isImageAttachment(attachment: AttachmentAsset): boolean {
  return attachment.kind === "image" || Boolean(attachment.mime_type?.startsWith("image/"));
}

function imageUrlsFromAttachments(attachments?: AttachmentAsset[]): string[] {
  return (attachments || []).filter(isImageAttachment).map(attachmentUrl).filter(Boolean);
}

function imageUrlsForMessage(message: ChatPanelState["messages"][number]): string[] {
  const urls = [...(message.images || []), ...imageUrlsFromAttachments(message.attachments)];
  return urls.filter((url, index) => url && urls.indexOf(url) === index);
}

function fileAttachments(attachments?: AttachmentAsset[]): AttachmentAsset[] {
  return (attachments || []).filter((attachment) => !isImageAttachment(attachment));
}

function FilePreviewBubble({ attachment }: { attachment: AttachmentAsset }) {
  const url = attachmentUrl(attachment);
  const label = attachmentLabel(attachment);
  const size = formatBytes(attachment.size_bytes);
  return (
    <a className="file-preview-bubble" href={url} target="_blank" rel="noreferrer" download={label}>
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

function ChatMessageView({ message }: { message: ChatPanelState["messages"][number] }) {
  const isUser = message.role === "user";
  const isObservation = message.role === "observation";
  const images = imageUrlsForMessage(message);
  const files = fileAttachments(message.attachments);
  const hasTextBubble = Boolean(message.content || message.pending);
  const persistedImageCount = !images.length ? message.imageCount || 0 : 0;
  const persistedAttachmentCount = !message.attachments?.length ? message.attachmentCount || 0 : 0;
  return (
    <div className={classNames("chat-message-group", isUser && "from-user")}>
      {hasTextBubble ? (
        <div
          className={classNames(
            "message-bubble",
            isUser ? "user-bubble" : isObservation ? "observation-bubble" : "assistant-bubble",
            message.error && "error-bubble",
          )}
        >
          <div className="message-label">{isUser ? "You" : isObservation ? "Observation" : "xAgent"}</div>
          {message.pending && !message.content ? (
            <div className="text-zinc-400">Thinking...</div>
          ) : (
            <Markdown content={message.content} renderImages={false} />
          )}
          {message.pending && message.content ? <span className="typing-cursor" /> : null}
        </div>
      ) : null}
      {images.map((src, index) => (
        <a key={`${message.id}-${index}`} href={src} target="_blank" rel="noreferrer" className="message-image-link">
          <img src={src} alt="" className="message-image-preview" />
        </a>
      ))}
      {files.map((attachment, index) => (
        <FilePreviewBubble key={`${attachmentUrl(attachment)}-${index}`} attachment={attachment} />
      ))}
      {persistedImageCount ? (
        <div
          className={classNames(
            "message-bubble attachment-count-bubble",
            isUser ? "user-bubble" : isObservation ? "observation-bubble" : "assistant-bubble",
          )}
        >
          Attached {persistedImageCount} {persistedImageCount === 1 ? "image" : "images"}
        </div>
      ) : null}
      {persistedAttachmentCount ? (
        <div
          className={classNames(
            "message-bubble attachment-count-bubble",
            isUser ? "user-bubble" : isObservation ? "observation-bubble" : "assistant-bubble",
          )}
        >
          Attached {persistedAttachmentCount} {persistedAttachmentCount === 1 ? "file" : "files"}
        </div>
      ) : null}
    </div>
  );
}

function PendingAttachmentPreview({ attachment, onRemove }: { attachment: AttachmentAsset; onRemove: () => void }) {
  const url = attachmentUrl(attachment);
  const isImage = isImageAttachment(attachment);
  return (
    <span className={isImage ? "image-chip" : "file-chip"}>
      {isImage ? (
        <img src={url} alt="" />
      ) : (
        <span className="pending-file-icon" aria-hidden="true">
          <FileIcon size={18} />
        </span>
      )}
      {!isImage ? <span>{attachmentLabel(attachment)}</span> : null}
      <button type="button" onClick={onRemove} title="Remove attachment">
        <X size={14} />
      </button>
    </span>
  );
}

function ChatPanel({ panel }: { panel: ChatPanelState }) {
  const { updateSettings, addAttachments, removeAttachment, sendMessage, sendObservation, clearVisiblePanels } = useChat();
  const [messageText, setMessageText] = useState("");
  const [observeText, setObserveText] = useState("");
  const [observeOpen, setObserveOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [panel.messages.length, panel.messages[panel.messages.length - 1]?.content]);

  const submitMessage = (event?: FormEvent) => {
    event?.preventDefault();
    const text = messageText.trim();
    if (!text && !panel.pendingAttachments.length) return;
    setMessageText("");
    void sendMessage(panel.id, text);
  };

  const submitObservation = (event?: FormEvent) => {
    event?.preventDefault();
    const text = observeText.trim();
    if (!text) return;
    setObserveText("");
    void sendObservation(panel.id, text);
    setObserveOpen(false);
  };

  return (
    <section className="chat-panel">
      <div className="chat-toolbar">
        <div className="panel-settings-left">
          <label className="inline-field">
            <span className="inline-label">User</span>
            <input
              value={panel.settings.userId}
              onChange={(event) => updateSettings(panel.id, { userId: event.target.value })}
              className="inline-input"
            />
          </label>
        </div>
        <div className="panel-settings-right">
          <label className="setting-toggle">
            <span>Stream</span>
            <input
              type="checkbox"
              checked={panel.settings.stream}
              onChange={(event) => updateSettings(panel.id, { stream: event.target.checked })}
            />
            <span className="toggle-track" />
          </label>
          <IconButton
            type="button"
            onClick={clearVisiblePanels}
            title="Clear chat"
            aria-label="Clear chat"
            disabled={!panel.messages.length}
          >
            <Trash2 size={15} />
          </IconButton>
        </div>
      </div>

      <div ref={scrollRef} className="fade-mask flex-1 min-h-0 overflow-y-auto px-3 sm:px-6 py-4 sm:py-6 space-y-4">
        {panel.messages.length ? (
          panel.messages.map((message) => <ChatMessageView key={message.id} message={message} />)
        ) : (
          <EmptyState icon={<Send size={24} />} title="Start a message stream">
            Type a message and press Enter to send.
          </EmptyState>
        )}
      </div>

      {panel.pendingAttachments.length ? (
        <div className="border-t border-black/5 dark:border-white/10 px-3 sm:px-6 py-3">
          {panel.pendingAttachments.map((attachment, index) => (
            <PendingAttachmentPreview
              key={`${attachmentUrl(attachment).slice(0, 24)}-${index}`}
              attachment={attachment}
              onRemove={() => removeAttachment(panel.id, index)}
            />
          ))}
        </div>
      ) : null}

      {observeOpen ? (
        <form onSubmit={submitObservation} className="observe-panel">
          <textarea
            rows={1}
            placeholder="Observe context..."
            value={observeText}
            disabled={panel.sending}
            onChange={(event) => setObserveText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) submitObservation(event);
            }}
          />
          <div className="observe-actions">
            <Button type="submit" variant="secondary" disabled={panel.sending || !observeText.trim()}>
              Observe
            </Button>
            <IconButton
              type="button"
              title="Close observation"
              aria-label="Close observation"
              onClick={() => {
                setObserveText("");
                setObserveOpen(false);
              }}
            >
              <X size={15} />
            </IconButton>
          </div>
        </form>
      ) : null}

      <form onSubmit={submitMessage} className="composer-row">
        <textarea
          rows={1}
          placeholder="Message xAgent..."
          value={messageText}
          disabled={panel.sending}
          onChange={(event) => setMessageText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) submitMessage(event);
          }}
        />
        <div className="composer-actions">
          <IconButton
            type="button"
            className="observe-toggle-button"
            onClick={() => setObserveOpen((value) => !value)}
            title="Add observation"
            aria-label="Add observation"
            disabled={panel.sending}
          >
            <Eye size={18} />
          </IconButton>
          <IconButton
            type="button"
            className="composer-upload-button"
            onClick={() => fileInputRef.current?.click()}
            title="Attach files"
            disabled={panel.sending}
          >
            <Paperclip size={18} />
          </IconButton>
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            multiple
            onChange={(event) => {
              if (event.target.files) addAttachments(panel.id, event.target.files);
              event.currentTarget.value = "";
            }}
          />
          <Button type="submit" variant="primary" className="send-button" disabled={panel.sending || (!messageText.trim() && !panel.pendingAttachments.length)}>
            <Send size={16} />
            Send
          </Button>
        </div>
      </form>
    </section>
  );
}

export function ChatPage() {
  const { panel } = useChat();

  return (
    <div className="h-full min-h-0 single-chat-grid">
      <ChatPanel panel={panel} />
    </div>
  );
}
