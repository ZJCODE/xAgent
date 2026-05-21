import { ImagePlus, Send, X } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { Markdown } from "../components/Markdown";
import { useChat } from "../context/ChatContext";
import { classNames } from "../lib/format";
import type { ChatPanelState } from "../types";

function ChatBubble({ message }: { message: ChatPanelState["messages"][number] }) {
  const isUser = message.role === "user";
  const isObservation = message.role === "observation";
  const persistedImageCount = isUser && !message.images?.length ? message.imageCount || 0 : 0;
  return (
    <div className={classNames("flex", isUser && "justify-end")}>
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
          <Markdown content={message.content} />
        )}
        {message.pending && message.content ? <span className="typing-cursor" /> : null}
        {isUser && message.images?.length ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {message.images.map((src, index) => (
              <img key={`${message.id}-${index}`} src={src} alt="" className="h-20 rounded-lg border border-white/30 object-cover" />
            ))}
          </div>
        ) : null}
        {persistedImageCount ? (
          <div className="mt-3 text-xs text-zinc-500 dark:text-zinc-400">
            Attached {persistedImageCount} {persistedImageCount === 1 ? "image" : "images"}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ChatPanel({ panel }: { panel: ChatPanelState }) {
  const { updateSettings, addImages, removeImage, sendMessage, sendObservation, capabilities } = useChat();
  const [messageText, setMessageText] = useState("");
  const [observeText, setObserveText] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const canUploadImages = capabilities.vision;

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [panel.messages.length, panel.messages[panel.messages.length - 1]?.content]);

  const submitMessage = (event?: FormEvent) => {
    event?.preventDefault();
    const text = messageText.trim();
    if (!text) return;
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
          panel.messages.map((message) => <ChatBubble key={message.id} message={message} />)
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

      {panel.pendingImages.length ? (
        <div className="border-t border-black/5 dark:border-white/10 px-3 sm:px-6 py-3">
          {panel.pendingImages.map((src, index) => (
            <span key={`${src.slice(0, 24)}-${index}`} className="image-chip">
              <img src={src} alt="" />
              <button type="button" onClick={() => removeImage(panel.id, index)} title="Remove image">
                <X size={14} />
              </button>
            </span>
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
        <button
          type="button"
          className="icon-button"
          onClick={() => {
            if (canUploadImages) fileInputRef.current?.click();
          }}
          title={canUploadImages ? "Upload image" : "Image input is not available for this provider"}
          disabled={panel.sending || !canUploadImages}
        >
          <ImagePlus size={18} />
        </button>
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          accept="image/jpeg,image/png"
          multiple
          disabled={!canUploadImages}
          onChange={(event) => {
            if (canUploadImages && event.target.files) addImages(panel.id, event.target.files);
            event.currentTarget.value = "";
          }}
        />
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
        <button type="submit" className="send-button" disabled={panel.sending || !messageText.trim()}>
          <Send size={16} />
          Send
        </button>
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
