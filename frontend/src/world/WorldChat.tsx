import { DragEvent, FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { FileIcon, MessageSquareText, Paperclip, Send, X } from "lucide-react";
import { Markdown } from "../components/Markdown";
import { Button, EmptyState } from "../components/ui";
import { classNames, formatBytes } from "../lib/format";
import {
  escapeRegExp,
  filterMentionPeople,
  findMentionQuery,
  insertMention,
  isImageMime,
  MAX_SPEAK_TEXT_LENGTH,
  mentionLabel,
  worldFileUrl,
  type MentionPerson,
  type WorldAttachment,
  type WorldEvent,
} from "./protocol";
import { useWorld } from "./WorldContext";

function UtteranceText({
  text,
  tokens,
}: {
  text: string;
  tokens: string[];
}) {
  const unique = [...new Set(tokens.filter(Boolean))].sort((a, b) => b.length - a.length);
  if (!unique.length) return <Markdown content={text} />;
  const pattern = new RegExp(
    `(^|\\s)(@(?:${unique.map(escapeRegExp).join("|")}))(?=$|\\s|[.,!?，。！？])`,
    "gi",
  );
  const nodes: Array<{ kind: "text" | "mention"; value: string }> = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    const lead = match[1] || "";
    const token = match[2] || "";
    const start = index + lead.length;
    if (start > last) nodes.push({ kind: "text", value: text.slice(last, start) });
    nodes.push({ kind: "mention", value: token });
    last = start + token.length;
  }
  if (last < text.length) nodes.push({ kind: "text", value: text.slice(last) });
  if (!nodes.some((node) => node.kind === "mention")) return <Markdown content={text} />;
  return (
    <p className="world-utterance">
      {nodes.map((node, index) =>
        node.kind === "mention" ? (
          <span key={`${node.value}-${index}`} className="world-mention">
            {node.value}
          </span>
        ) : (
          <span key={`t-${index}`}>{node.value}</span>
        ),
      )}
    </p>
  );
}

function EventView({
  event,
  memberId,
  displayOf,
  people,
}: {
  event: WorldEvent;
  memberId: string;
  displayOf: (id: string) => string;
  people: MentionPerson[];
}) {
  const kind = event.kind || "";
  const actor = event.actor_id || "";
  const name = displayOf(actor);
  const mine = actor === memberId;
  const attachments = event.attachments || [];
  const images = attachments.filter((item) => isImageMime(item.mime, item.name));
  const text = event.text || "";
  const mentioned = event.mentions || [];
  const tokens = [
    ...mentioned.flatMap((id) => [id, displayOf(id)]),
    ...people.flatMap((person) => [person.member_id, mentionLabel(person)]),
  ];

  if (kind === "utterance") {
    return (
      <div className={classNames("chat-message-group", mine && "from-user")}>
        {text ? (
          <div className={classNames("message-bubble", mine ? "user-bubble" : "assistant-bubble")}>
            <div className="message-label">{mine ? "you" : name}</div>
            <UtteranceText text={text} tokens={tokens} />
          </div>
        ) : attachments.length ? (
          <div className="world-file-meta">{mine ? "you" : name} shared a file</div>
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
    return <div className="world-system">{mine ? "you" : name} joined</div>;
  }
  if (kind === "leave") {
    return <div className="world-system">{mine ? "you" : name} left</div>;
  }
  return <div className="world-system">[{kind}] {mine ? "you" : name}{event.text ? `: ${event.text}` : ""}</div>;
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
    joined,
    memberId,
    displayOf,
    speakText,
    setSpeakText,
    speak,
    pendingFiles,
    attachError,
    composeError,
    addFiles,
    removeFile,
    sending,
    present,
    neighbors,
  } = useWorld();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const areaRef = useRef<HTMLTextAreaElement | null>(null);
  const [dropping, setDropping] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [activeIndex, setActiveIndex] = useState(0);
  const [dismissedStart, setDismissedStart] = useState<number | null>(null);

  const people = useMemo<MentionPerson[]>(() => {
    const presentIds = new Set(present.map((item) => item.member_id));
    const list: MentionPerson[] = present.map((item) => ({
      member_id: item.member_id,
      display_name: item.display_name || item.member_id,
      here: true,
    }));
    for (const agent of neighbors) {
      if (presentIds.has(agent.name)) continue;
      list.push({
        member_id: agent.name,
        display_name: agent.title || agent.name,
        here: false,
      });
    }
    return list;
  }, [neighbors, present]);

  const mentionQuery = joined ? findMentionQuery(speakText, cursor) : null;
  const mentionMatches = mentionQuery ? filterMentionPeople(people, mentionQuery.query) : [];
  const mentionOpen = Boolean(mentionQuery) && mentionMatches.length > 0 && mentionQuery?.start !== dismissedStart;

  useEffect(() => {
    if (!mentionQuery) setDismissedStart(null);
  }, [mentionQuery]);

  useEffect(() => {
    setActiveIndex(0);
  }, [mentionQuery?.query, mentionQuery?.start, mentionMatches.length]);

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [events.length, events[events.length - 1]?.text, events[events.length - 1]?.attachments?.length]);

  const textLen = speakText.length;
  const overLimit = textLen > MAX_SPEAK_TEXT_LENGTH;
  const showCharCount = joined && textLen > MAX_SPEAK_TEXT_LENGTH * 0.75;
  const canSend =
    joined && !sending && !overLimit && Boolean(speakText.trim() || pendingFiles.length);

  const applyMention = (person: MentionPerson) => {
    const next = insertMention(speakText, cursor, person);
    setSpeakText(next.text);
    setCursor(next.cursor);
    setDismissedStart(null);
    requestAnimationFrame(() => {
      const node = areaRef.current;
      if (!node) return;
      node.focus();
      node.setSelectionRange(next.cursor, next.cursor);
    });
  };

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSend) return;
    void speak().then(() => areaRef.current?.focus());
  };

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    setCursor(event.currentTarget.selectionStart);
    if (mentionOpen) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => (index + 1) % mentionMatches.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => (index - 1 + mentionMatches.length) % mentionMatches.length);
        return;
      }
      if (event.key === "Tab" || event.key === "Enter") {
        event.preventDefault();
        const person = mentionMatches[activeIndex] || mentionMatches[0];
        if (person) applyMention(person);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setDismissedStart(mentionQuery?.start ?? null);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit(event);
    }
  };

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDropping(false);
    if (!joined) return;
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
        if (joined) setDropping(true);
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
              people={people}
            />
          ))
        ) : (
          <EmptyState icon={<MessageSquareText size={24} />} title={joined ? "No messages yet" : "Not in a world"}>
            {joined
              ? "Speech and files stay on this world's timeline."
              : "Select or create a world from the sidebar."}
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
              <button type="button" onClick={() => removeFile(item.id)} title="Remove">
                <X size={14} />
              </button>
            </span>
          ))}
        </div>
      ) : null}
      {attachError ? <p className="world-error px-3 sm:px-6">{attachError}</p> : null}
      {composeError ? (
        <p className="world-compose-error mx-3 sm:mx-6 mb-2" role="alert">
          {composeError}
        </p>
      ) : null}

      <form
        onSubmit={submit}
        className={classNames("composer-row", "world-composer", (!joined || sending) && "is-disabled")}
      >
        <div className="world-composer-field">
          {mentionOpen ? (
            <ul className="world-mention-menu" role="listbox">
              {mentionMatches.map((person, index) => (
                <li key={person.member_id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={index === activeIndex}
                    className={classNames("world-mention-option", index === activeIndex && "is-active")}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => applyMention(person)}
                  >
                    <span className="world-mention">@{mentionLabel(person)}</span>
                    {person.here === false ? <span className="world-mention-away">not here</span> : null}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <textarea
            ref={areaRef}
            rows={1}
            placeholder={joined ? "Say something. Type @ to mention someone." : "Join a world to speak"}
            value={speakText}
            disabled={!joined || sending}
            onChange={(event) => {
              setSpeakText(event.target.value);
              setCursor(event.target.selectionStart);
            }}
            onClick={(event) => setCursor(event.currentTarget.selectionStart)}
            onKeyUp={(event) => setCursor(event.currentTarget.selectionStart)}
            onKeyDown={onComposerKey}
          />
          {showCharCount ? (
            <p className={classNames("world-char-count", overLimit && "is-over")}>
              {textLen} / {MAX_SPEAK_TEXT_LENGTH}
            </p>
          ) : null}
        </div>
        <div className="composer-actions">
          <label
            className={classNames(
              "ui-button ui-button-ghost ui-icon-button composer-upload-button",
              (!joined || sending) && "is-disabled",
            )}
            title="Attach file"
          >
            <input
              type="file"
              className="world-file-input"
              multiple
              disabled={!joined || sending}
              onChange={(event) => {
                if (event.target.files) addFiles(event.target.files);
                event.currentTarget.value = "";
              }}
            />
            <Paperclip size={18} />
          </label>
          <Button type="submit" variant="primary" className="send-button" disabled={!canSend}>
            <Send size={16} />
            Send
          </Button>
        </div>
      </form>
    </section>
  );
}
