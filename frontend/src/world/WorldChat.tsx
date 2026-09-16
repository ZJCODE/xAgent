import { DragEvent, FormEvent, useEffect, useRef, useState } from "react";
import { FileIcon, MessageSquareText, Paperclip, Send, X } from "lucide-react";
import { Markdown } from "../components/Markdown";
import { Button, EmptyState } from "../components/ui";
import { classNames, formatBytes } from "../lib/format";
import {
  formatEventTime,
  isImageMime,
  worldFileUrl,
  type WorldAttachment,
  type WorldEvent,
} from "./protocol";
import { useWorld } from "./WorldContext";

function EventView({ event, memberId, displayOf }: { event: WorldEvent; memberId: string; displayOf: (id: string) => string }) {
  const kind = event.kind || "";
  const actor = event.actor_id || "";
  const name = displayOf(actor);
  const mine = actor === memberId;
  const time = formatEventTime(event.ts);
  const attachments = event.attachments || [];
  const images = attachments.filter((item) => isImageMime(item.mime, item.name));
  const text = event.text || "";

  if (kind === "utterance") {
    return (
      <div className={classNames("chat-message-group", mine && "from-user")}>
        {text ? (
          <div className={classNames("message-bubble", mine ? "user-bubble" : "assistant-bubble")}>
            <div className="message-label">
              {mine ? "You" : name}
              {time ? <span className="world-event-time">{time}</span> : null}
            </div>
            <Markdown content={text} />
          </div>
        ) : attachments.length ? (
          <div className="world-file-meta">
            {mine ? "你" : name} 发了文件
            {time ? <span className="world-event-time">{time}</span> : null}
          </div>
        ) : null}
        {images.map((attachment) => (
          <a
            key={`img-${attachment.id}`}
            href={worldFileUrl(attachment)}
            target="_blank"
            rel="noreferrer"
            className="message-image-link"
          >
            <img src={worldFileUrl(attachment)} alt={attachment.name} className="message-image-preview" />
          </a>
        ))}
        {attachments.map((attachment) => (
          <FileBubble key={attachment.id} attachment={attachment} />
        ))}
      </div>
    );
  }

  if (kind === "join") {
    return <div className="world-system">{name} 进来了</div>;
  }
  if (kind === "leave") {
    return <div className="world-system">{name} 离开了</div>;
  }
  if (kind === "scene") {
    return (
      <div className="chat-message-group">
        <div className="message-bubble observation-bubble">
          <div className="message-label">Scene{time ? <span className="world-event-time">{time}</span> : null}</div>
          <Markdown content={event.text || ""} />
        </div>
      </div>
    );
  }
  return <div className="world-system">[{kind}] {name}{event.text ? `: ${event.text}` : ""}</div>;
}

function FileBubble({ attachment }: { attachment: WorldAttachment }) {
  const url = worldFileUrl(attachment);
  const size = formatBytes(attachment.size);
  return (
    <a className="file-preview-bubble" href={url} target="_blank" rel="noreferrer" download={attachment.name}>
      <span className="file-preview-icon" aria-hidden="true">
        <FileIcon size={34} />
      </span>
      <span className="file-preview-meta">
        <span className="file-preview-name">{attachment.name}</span>
        {size ? <span className="file-preview-size">{size}</span> : null}
      </span>
    </a>
  );
}

export function WorldChat() {
  const {
    events,
    presentInRoom,
    memberId,
    displayOf,
    speakText,
    setSpeakText,
    speak,
    pendingFiles,
    attachError,
    addFiles,
    removeFile,
    sending,
  } = useWorld();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const areaRef = useRef<HTMLTextAreaElement | null>(null);
  const [dropping, setDropping] = useState(false);

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [events.length, events[events.length - 1]?.text, events[events.length - 1]?.attachments?.length]);

  useEffect(() => {
    const node = areaRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 140)}px`;
  }, [speakText]);

  const canSend = presentInRoom && !sending && Boolean(speakText.trim() || pendingFiles.length);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSend) return;
    void speak().then(() => areaRef.current?.focus());
  };

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDropping(false);
    if (!presentInRoom) return;
    if (event.dataTransfer.files?.length) addFiles(event.dataTransfer.files);
  };

  const onPasteFiles = (files: FileList | null) => {
    if (files && files.length) addFiles(files);
  };

  return (
    <section
      className={classNames("chat-panel", dropping && "world-chat-drop")}
      onDragEnter={(event) => {
        event.preventDefault();
        if (presentInRoom) setDropping(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => {
        if (event.currentTarget.contains(event.relatedTarget as Node)) return;
        setDropping(false);
      }}
      onDrop={onDrop}
      onPaste={(event) => {
        const files = event.clipboardData?.files;
        if (files && files.length) {
          event.preventDefault();
          onPasteFiles(files);
        }
      }}
    >
      <div ref={scrollRef} className="fade-mask flex-1 min-h-0 overflow-y-auto px-3 sm:px-6 py-4 sm:py-6 space-y-4">
        {events.length ? (
          events.map((event, index) => (
            <EventView
              key={event.seq || `${event.kind}-${event.actor_id}-${index}`}
              event={event}
              memberId={memberId}
              displayOf={displayOf}
            />
          ))
        ) : (
          <EmptyState icon={<MessageSquareText size={24} />} title={presentInRoom ? "还没有人说话" : "进入大厅"}>
            {presentInRoom ? "你说的话和文件会留在房间时间线上。" : "从左侧填好名字并进入。"}
          </EmptyState>
        )}
      </div>

      {pendingFiles.length ? (
        <div className="border-t border-black/5 dark:border-white/10 px-3 sm:px-6 py-3">
          {pendingFiles.map((item) => (
            <span key={item.id} className={isImageMime(item.mime, item.name) ? "image-chip" : "file-chip"}>
              {isImageMime(item.mime, item.name) ? (
                <img src={item.previewUrl} alt="" />
              ) : (
                <span className="pending-file-icon" aria-hidden="true">
                  <FileIcon size={18} />
                </span>
              )}
              {!isImageMime(item.mime, item.name) ? <span>{item.name}</span> : null}
              <button type="button" onClick={() => removeFile(item.id)} title="移除">
                <X size={14} />
              </button>
            </span>
          ))}
        </div>
      ) : null}
      {attachError ? <p className="world-error px-3 sm:px-6">{attachError}</p> : null}

      <form
        onSubmit={submit}
        className={classNames("composer-row", (!presentInRoom || sending) && "is-disabled")}
      >
        <textarea
          ref={areaRef}
          rows={1}
          placeholder={presentInRoom ? "说话或发送文件。Enter 发送，Shift+Enter 换行。用 @名字 点名。" : "进入大厅后即可说话"}
          value={speakText}
          disabled={!presentInRoom || sending}
          onChange={(event) => setSpeakText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit(event);
            }
          }}
        />
        <div className="composer-actions">
          <label
            className={classNames(
              "ui-button ui-button-ghost ui-icon-button composer-upload-button",
              (!presentInRoom || sending) && "is-disabled",
            )}
            title="发送文件"
          >
            <input
              type="file"
              className="world-file-input"
              multiple
              disabled={!presentInRoom || sending}
              onChange={(event) => {
                if (event.target.files) addFiles(event.target.files);
                event.currentTarget.value = "";
              }}
            />
            <Paperclip size={18} />
          </label>
          <Button type="submit" variant="primary" className="send-button" disabled={!canSend}>
            <Send size={16} />
            发送
          </Button>
        </div>
      </form>
    </section>
  );
}
