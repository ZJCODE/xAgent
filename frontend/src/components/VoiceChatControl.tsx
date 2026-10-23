import { Mic, Square } from "lucide-react";
import { useRef, type ReactNode } from "react";
import { useVoiceChat } from "../lib/useVoiceChat";
import { classNames } from "../lib/format";
import { Button, StatusBadge } from "./ui";

const HOLD_MS = 450;

export function VoiceChatControl({
  userId,
  enabled,
  children,
}: {
  userId: string;
  enabled: boolean;
  children?: ReactNode;
}) {
  const voice = useVoiceChat(userId);
  const pressAt = useRef(0);
  const live = voice.phase === "live";
  const statusLabel = !enabled
    ? "Voice"
    : voice.phase === "connecting"
      ? "Connecting"
      : live
        ? voice.talking
          ? "Listening"
          : voice.floorLabel || "Voice"
        : "Voice";
  const hint = voice.talking
    ? voice.heard
      ? "Heard you. Pause, or click again to send."
      : "Listening. Speak, then pause."
    : voice.sending
      ? "Sending…"
      : live
        ? "Click once and speak. A short pause sends it."
        : "Click once to start.";

  const pressTalk = () => {
    pressAt.current = performance.now();
    voice.setHolding(true);
    if (voice.talking) voice.endTalk();
    else voice.beginTalk();
  };

  const releaseTalk = () => {
    voice.setHolding(false);
    if (performance.now() - pressAt.current >= HOLD_MS) voice.endTalk();
  };

  return (
    <>
      <div className="chat-toolbar">
      <div className="chat-voice-slot">
        <Button
          type="button"
          variant={live ? "primary" : "secondary"}
          className={classNames("voice-toggle", voice.open && "is-active")}
          disabled={!enabled}
          aria-expanded={voice.open}
          title={enabled ? "Talk to the agent" : "Start the Agent to use voice"}
          onClick={() => voice.setOpen((open) => !open)}
        >
          <Mic size={15} />
          {statusLabel}
        </Button>
      </div>
      {children}
      </div>
      {voice.open ? (
        <div className="voice-chat-panel" role="region" aria-label="Voice chat">
          <div className="voice-chat-summary">
            <strong>Voice</strong>
            <span>{hint}</span>
            {live ? (
              <StatusBadge tone={voice.floor === "speaking" ? "info" : voice.floor === "thinking" ? "info" : "good"}>
                {voice.talking ? "Listening" : voice.floorLabel || "Live"}
              </StatusBadge>
            ) : null}
          </div>
          {voice.error ? <div className="error-strip" role="alert">{voice.error}</div> : null}
          {live ? (
            <>
              <button
                type="button"
                className={classNames("ui-button", "voice-hold", voice.talking ? "ui-button-danger is-talking" : "ui-button-primary")}
                aria-pressed={voice.talking}
                onPointerDown={(event) => {
                  event.preventDefault();
                  event.currentTarget.setPointerCapture(event.pointerId);
                  pressTalk();
                }}
                onPointerUp={releaseTalk}
                onPointerCancel={releaseTalk}
                onContextMenu={(event) => event.preventDefault()}
                onKeyDown={(event) => {
                  if (event.repeat) return;
                  if (event.key === " " || event.key === "Enter") {
                    event.preventDefault();
                    pressTalk();
                  }
                }}
                onKeyUp={(event) => {
                  if (event.key === " " || event.key === "Enter") {
                    event.preventDefault();
                    releaseTalk();
                  }
                }}
              >
                <Mic size={16} />
                {voice.talking ? "Listening…" : "Click to talk"}
              </button>
              {voice.lines.length ? (
                <div className="voice-transcript" aria-live="polite">
                  {voice.lines.map((line) => (
                    <p key={line.id} className={line.role === "user" ? "voice-line-user" : "voice-line-assistant"}>
                      <span>{line.role === "user" ? "You" : "xAgent"}</span>
                      {line.text}
                      {line.pending ? <span className="typing-cursor" /> : null}
                    </p>
                  ))}
                </div>
              ) : null}
            </>
          ) : (
            <div className="voice-actions">
              {voice.paired ? (
                <Button type="button" variant="primary" disabled={voice.phase === "connecting" || voice.busy} onClick={voice.connectSaved}>
                  <Mic size={15} />
                  {voice.phase === "connecting" ? "Connecting…" : "Connect"}
                </Button>
              ) : (
                <Button type="button" variant="primary" disabled={voice.busy || voice.phase === "connecting"} onClick={() => void voice.pairBrowser()}>
                  <Mic size={15} />
                  {voice.phase === "connecting" ? "Connecting…" : "Start"}
                </Button>
              )}
              {voice.paired ? (
                <Button type="button" disabled={voice.busy} onClick={() => void voice.unpair()}>
                  Forget
                </Button>
              ) : null}
            </div>
          )}
          {live ? (
            <div className="voice-actions">
              <Button type="button" onClick={voice.disconnect}>
                <Square size={14} />
                Disconnect
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
