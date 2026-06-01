import { FileIcon, Paperclip, Send, X } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { Markdown } from "../components/Markdown";
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
  const { updateSettings, addAttachments, removeAttachment, sendMessage, sendObservation } = useChat();
  const [messageText, setMessageText] = useState("");
  const [observeText, setObserveText] = useState("");
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
  };

  return (
    <section className="chat-panel">
      <div className="panel-settings-bar">
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
          <label className="setting-toggle">
            <span>Memory</span>
            <input
              type="checkbox"
              checked={panel.settings.memory}
              onChange={(event) => updateSettings(panel.id, { memory: event.target.checked })}
            />
            <span className="toggle-track" />
          </label>
        </div>
      </div>

      <div ref={scrollRef} className="fade-mask flex-1 min-h-0 overflow-y-auto px-3 sm:px-6 py-4 sm:py-6 space-y-4">
        {panel.messages.length ? (
          panel.messages.map((message) => <ChatMessageView key={message.id} message={message} />)
        ) : (
          <div className="h-full min-h-[320px] flex items-center justify-center text-center text-sm text-zinc-500 dark:text-zinc-400">
            <div>
              <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl panel-muted">
                <Send size={24} />
              </div>
              <p className="font-semibold text-base text-zinc-800 dark:text-zinc-100">Start a message stream</p>
              <p className="mt-2">Type a message and press Enter to send.</p>
            </div>
          </div>
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

      <form onSubmit={submitObservation} className="observe-row">
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
        <button type="submit" disabled={panel.sending || !observeText.trim()}>
          Observe
        </button>
      </form>

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
          <button
            type="button"
            className="icon-button composer-upload-button"
            onClick={() => fileInputRef.current?.click()}
            title="Attach files"
            disabled={panel.sending}
          >
            <Paperclip size={18} />
          </button>
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
          <button type="submit" className="send-button" disabled={panel.sending || (!messageText.trim() && !panel.pendingAttachments.length)}>
            <Send size={16} />
            Send
          </button>
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
