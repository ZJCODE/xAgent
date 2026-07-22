import { Activity, Eye, FileIcon, Paperclip, Play, RadioTower, Send, Square, UserRound, Wifi, WifiOff, X } from "lucide-react";
import { FormEvent, useEffect, useRef, useState, type ReactNode } from "react";
import { Markdown } from "../components/Markdown";
import { Button, EmptyState, IconButton, StatusBadge } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { DEFAULT_WEB_USER_ID, useChat } from "../context/ChatContext";
import { useApiChannel } from "../lib/useApiChannel";
import { useApiHealth } from "../lib/useApiHealth";
import { classNames, formatBytes } from "../lib/format";
import type { AttachmentAsset, ChannelStatus, ChatPanelState } from "../types";

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

function ChatChannelBlocked({
  channel,
  starting,
  loading,
  error,
  onStart,
  onRetry,
  variant,
}: {
  channel: ChannelStatus | null;
  starting: boolean;
  loading: boolean;
  error: string;
  onStart: () => void;
  onRetry: () => void;
  variant: "empty" | "banner";
}) {
  if (!channel && loading) {
    const connecting = (
      <EmptyState icon={<RadioTower size={24} />} title="Connecting to API channel">
        Loading channel status...
      </EmptyState>
    );
    if (variant === "banner") {
      return <div className="chat-channel-banner" role="status">{connecting}</div>;
    }
    return connecting;
  }

  if (!channel && error) {
    const unreachable = (
      <div className="empty-state chat-channel-empty">
        <div className="empty-state-icon" aria-hidden="true">
          <RadioTower size={24} />
        </div>
        <p>Cannot reach channel service</p>
        <span>{error}</span>
        <div className="chat-channel-actions">
          <Button type="button" variant="primary" onClick={onRetry}>
            Retry
          </Button>
          <Button type="button" variant="secondary" disabled={starting} onClick={onStart}>
            <Play size={14} />
            {starting ? "Starting..." : "Start API channel"}
          </Button>
        </div>
      </div>
    );
    if (variant === "banner") {
      return <div className="chat-channel-banner" role="status">{unreachable}</div>;
    }
    return unreachable;
  }

  const needsSetup = Boolean(channel && !channel.ready);
  const isError = channel?.status === "error";
  const title = needsSetup
    ? "API channel needs setup"
    : isError
      ? "API channel needs attention"
      : "API channel is stopped";
  const description = needsSetup
    ? "Open the Channels page to finish setup, then start the API channel here."
    : isError
      ? channel?.detail || "Try starting the channel again."
      : "Start the API channel to send messages in Chat.";
  const canStart = starting ? false : channel == null || channel.can_start;

  const actions = (
    <div className="chat-channel-actions">
      {needsSetup ? (
        <a className="chat-channel-link" href="/channels">
          Open Channels
        </a>
      ) : (
        <Button
          type="button"
          variant="primary"
          disabled={!canStart}
          onClick={onStart}
        >
          <Play size={14} />
          {starting ? "Starting..." : "Start API channel"}
        </Button>
      )}
    </div>
  );

  if (variant === "banner") {
    return (
      <div className="chat-channel-banner" role="status">
        <div className="chat-channel-banner-copy">
          <p>{title}</p>
          <span>{description}</span>
          {error ? <span className="chat-channel-error">{error}</span> : null}
        </div>
        <div className="chat-channel-banner-actions">
          {needsSetup ? (
            <a className="chat-channel-link" href="/channels">
              Open Channels
            </a>
          ) : (
            <Button
              type="button"
              variant="primary"
              disabled={!canStart}
              onClick={onStart}
            >
              <Play size={13} />
              {starting ? "Starting..." : "Start"}
            </Button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="empty-state chat-channel-empty">
      <div className="empty-state-icon" aria-hidden="true">
        <RadioTower size={24} />
      </div>
      <p>{title}</p>
      <span>{description}</span>
      {error ? <span className="chat-channel-error">{error}</span> : null}
      {actions}
    </div>
  );
}

function ChatPanel({
  panel,
  chatEnabled,
  statusReady,
  channelBlock,
}: {
  panel: ChatPanelState;
  chatEnabled: boolean;
  statusReady: boolean;
  channelBlock?: ReactNode;
}) {
  const { updateSettings, addAttachments, removeAttachment, sendMessage, stopGeneration, sendObservation, status: chatStatus } = useChat();
  const health = useApiHealth();
  const [messageText, setMessageText] = useState("");
  const [observeText, setObserveText] = useState("");
  const [observeOpen, setObserveOpen] = useState(false);
  const [userIdOpen, setUserIdOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const customUserId = panel.settings.userId !== DEFAULT_WEB_USER_ID;

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [panel.messages.length, panel.messages[panel.messages.length - 1]?.content]);

  useEffect(() => {
    if (!chatEnabled) setUserIdOpen(false);
  }, [chatEnabled]);

  const submitMessage = (event?: FormEvent) => {
    event?.preventDefault();
    if (!chatEnabled || panel.sending) return;
    const text = messageText.trim();
    if (!text && !panel.pendingAttachments.length) return;
    setMessageText("");
    void sendMessage(panel.id, text);
  };

  const stopMessage = () => {
    stopGeneration(panel.id);
  };

  const submitObservation = (event?: FormEvent) => {
    event?.preventDefault();
    if (!chatEnabled) return;
    const text = observeText.trim();
    if (!text) return;
    setObserveText("");
    void sendObservation(panel.id, text);
    setObserveOpen(false);
  };

  return (
    <section className="chat-panel">
      <div className="chat-toolbar">
        <div className="panel-settings-right chat-status-cluster">
          {customUserId ? (
            <StatusBadge tone="info" className="chat-status-badge" title="Non-default API user ID">
              <UserRound size={14} />
              <span className="status-badge-label">as {panel.settings.userId}</span>
            </StatusBadge>
          ) : null}
          <StatusBadge
            tone={health === "online" ? "good" : health === "offline" ? "danger" : "muted"}
            className="chat-status-badge"
            title="Reflects the selected agent's api channel (chat). Other tabs work independently."
          >
            {health === "online" ? <Wifi size={14} /> : <WifiOff size={14} />}
            <span className="status-badge-label">
              {health === "checking" ? "Checking" : health === "online" ? "API Online" : "API Offline"}
            </span>
          </StatusBadge>
          <StatusBadge
            tone={chatStatus === "sending" ? "info" : "muted"}
            className="chat-status-badge"
            title={chatStatus === "sending" ? "Chat running" : "Chat idle"}
          >
            <Activity size={14} />
            <span className="status-badge-label">{chatStatus === "sending" ? "Chat running" : "Chat idle"}</span>
          </StatusBadge>
        </div>
      </div>

      <div ref={scrollRef} className="fade-mask flex-1 min-h-0 overflow-y-auto px-3 sm:px-6 py-4 sm:py-6 space-y-4">
        {panel.messages.length ? (
          panel.messages.map((message) => <ChatMessageView key={message.id} message={message} />)
        ) : !statusReady ? (
          <EmptyState icon={<RadioTower size={24} />} title="Checking API channel">
            Loading channel status...
          </EmptyState>
        ) : chatEnabled ? (
          <EmptyState icon={<Send size={24} />} title="Start a message stream">
            Type a message and press Enter to send.
          </EmptyState>
        ) : (
          channelBlock
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

      {userIdOpen && chatEnabled ? (
        <div className="observe-panel">
          <input
            id={`user-id-${panel.id}`}
            value={panel.settings.userId}
            onChange={(event) => updateSettings(panel.id, { userId: event.target.value })}
            className="user-id-panel-input"
            placeholder="web_user by default — change only to test another API user."
            spellCheck={false}
            autoComplete="off"
            disabled={!statusReady || panel.sending}
          />
          <div className="observe-actions">
            <Button
              type="button"
              variant="secondary"
              disabled={!statusReady || panel.sending || !customUserId}
              title={`Reset to ${DEFAULT_WEB_USER_ID}`}
              onClick={() => updateSettings(panel.id, { userId: DEFAULT_WEB_USER_ID })}
            >
              Reset
            </Button>
            <IconButton
              type="button"
              title="Close user ID"
              aria-label="Close user ID"
              onClick={() => setUserIdOpen(false)}
            >
              <X size={15} />
            </IconButton>
          </div>
        </div>
      ) : null}

      {observeOpen ? (
        <form onSubmit={submitObservation} className="observe-panel">
          <textarea
            rows={1}
            placeholder="Observe context..."
            value={observeText}
            disabled={!statusReady || !chatEnabled || panel.sending}
            onChange={(event) => setObserveText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) submitObservation(event);
            }}
          />
          <div className="observe-actions">
            <Button type="submit" variant="secondary" disabled={!statusReady || !chatEnabled || panel.sending || !observeText.trim()}>
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

      {!chatEnabled && statusReady && panel.messages.length ? channelBlock : null}

      <form onSubmit={submitMessage} className={classNames("composer-row", (!statusReady || !chatEnabled) && "is-disabled")}>
        <textarea
          rows={1}
          placeholder={!statusReady ? "Checking API channel..." : chatEnabled ? "Message xAgent..." : "Start the API channel to chat..."}
          value={messageText}
          disabled={!statusReady || !chatEnabled}
          onChange={(event) => setMessageText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              if (panel.sending) {
                event.preventDefault();
                return;
              }
              submitMessage(event);
            }
          }}
        />
        <div className="composer-actions">
          <IconButton
            type="button"
            className={classNames("user-id-toggle-button", userIdOpen && "is-active")}
            onClick={() => {
              setObserveOpen(false);
              setUserIdOpen((value) => !value);
            }}
            title="User ID"
            aria-label="User ID"
            disabled={!statusReady || !chatEnabled || panel.sending}
          >
            <UserRound size={18} />
          </IconButton>
          <IconButton
            type="button"
            className={classNames("observe-toggle-button", observeOpen && "is-active")}
            onClick={() => {
              setUserIdOpen(false);
              setObserveOpen((value) => !value);
            }}
            title="Add observation"
            aria-label="Add observation"
            disabled={!statusReady || !chatEnabled || panel.sending}
          >
            <Eye size={18} />
          </IconButton>
          <IconButton
            type="button"
            className="composer-upload-button"
            onClick={() => fileInputRef.current?.click()}
            title="Attach files"
            disabled={!statusReady || !chatEnabled || panel.sending}
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
          {panel.sending ? (
            <Button
              type="button"
              variant="primary"
              className="send-button stop-button"
              onClick={stopMessage}
              title="Stop generating"
              aria-label="Stop generating"
            >
              <Square size={14} fill="currentColor" />
              Stop
            </Button>
          ) : (
            <Button
              type="submit"
              variant="primary"
              className="send-button"
              disabled={
                !statusReady || !chatEnabled || (!messageText.trim() && !panel.pendingAttachments.length)
              }
            >
              <Send size={16} />
              Send
            </Button>
          )}
        </div>
      </form>
    </section>
  );
}

export function ChatPage() {
  const { loading: agentsLoading } = useAgentSession();
  const { panel } = useChat();
  const { apiChannel, loading: channelLoading, starting, error, start, refresh } = useApiChannel();

  const statusReady = !agentsLoading && !channelLoading;
  const chatEnabled = apiChannel?.status === "running";

  const channelBlock = statusReady && !chatEnabled ? (
    <ChatChannelBlocked
      channel={apiChannel}
      starting={starting}
      loading={channelLoading}
      error={error}
      onStart={() => void start()}
      onRetry={() => void refresh()}
      variant="empty"
    />
  ) : null;

  const channelBanner = statusReady && !chatEnabled ? (
    <ChatChannelBlocked
      channel={apiChannel}
      starting={starting}
      loading={channelLoading}
      error={error}
      onStart={() => void start()}
      onRetry={() => void refresh()}
      variant="banner"
    />
  ) : null;

  return (
    <div className="h-full min-h-0 single-chat-grid">
      <ChatPanel
        panel={panel}
        chatEnabled={chatEnabled}
        statusReady={statusReady}
        channelBlock={panel.messages.length ? channelBanner : channelBlock}
      />
    </div>
  );
}
