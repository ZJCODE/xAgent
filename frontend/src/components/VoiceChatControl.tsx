import { Mic, Square } from "lucide-react";
import { FormEvent, useState, type ReactNode } from "react";
import { useVoiceChat } from "../lib/useVoiceChat";
import { classNames } from "../lib/format";
import { Button, StatusBadge } from "./ui";

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
  const [code, setCode] = useState("");
  const live = voice.phase === "live";
  const statusLabel = !enabled
    ? "Voice"
    : voice.phase === "connecting"
      ? "Connecting"
      : live
        ? voice.floorLabel || "Live"
        : voice.paired
          ? "Paired"
          : "Voice";

  const submitCode = (event: FormEvent) => {
    event.preventDefault();
    voice.connectCode(code);
    setCode("");
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
          title={enabled ? "Talk to the agent over the device voice channel" : "Start the Agent to use voice"}
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
            <span>Headset push-to-talk on the device voice channel. Replies stay out of this text chat.</span>
            {live ? (
              <StatusBadge tone={voice.floor === "speaking" ? "info" : voice.floor === "thinking" ? "info" : "good"}>
                {voice.floorLabel || "Live"}
              </StatusBadge>
            ) : voice.paired ? (
              <StatusBadge tone="muted">{voice.deviceId}</StatusBadge>
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
                  voice.beginTalk();
                }}
                onPointerUp={() => voice.endTalk()}
                onPointerCancel={() => voice.endTalk()}
                onContextMenu={(event) => event.preventDefault()}
                onKeyDown={(event) => {
                  if (event.repeat) return;
                  if (event.key === " " || event.key === "Enter") {
                    event.preventDefault();
                    voice.beginTalk();
                  }
                }}
                onKeyUp={(event) => {
                  if (event.key === " " || event.key === "Enter") {
                    event.preventDefault();
                    voice.endTalk();
                  }
                }}
              >
                <Mic size={16} />
                {voice.talking ? "Release to send" : "Hold to talk"}
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
              ) : (
                <p className="wizard-hint">Hold the button, speak, then release. Hold it again while the agent is talking to interrupt.</p>
              )}
            </>
          ) : voice.paired ? (
            <div className="voice-actions">
              <Button type="button" variant="primary" disabled={voice.phase === "connecting" || voice.busy} onClick={voice.connectSaved}>
                <Mic size={15} />
                {voice.phase === "connecting" ? "Connecting…" : "Connect"}
              </Button>
              <Button type="button" disabled={voice.busy} onClick={() => void voice.unpair()}>
                Unpair browser
              </Button>
            </div>
          ) : (
            <div className="voice-pair">
              <div className="voice-actions">
                <Button type="button" variant="primary" disabled={voice.busy || voice.phase === "connecting"} onClick={() => void voice.pairBrowser()}>
                  <Mic size={15} />
                  {voice.phase === "connecting" ? "Connecting…" : "Pair this browser"}
                </Button>
              </div>
              <form className="voice-pair-row" onSubmit={submitCode}>
                <input
                  value={code}
                  onChange={(event) => setCode(event.target.value)}
                  placeholder="Or paste a pairing code"
                  spellCheck={false}
                  autoComplete="off"
                  aria-label="Pairing code"
                  disabled={voice.busy || voice.phase === "connecting"}
                />
                <Button type="submit" disabled={voice.busy || voice.phase === "connecting" || !code.trim()}>
                  Connect
                </Button>
              </form>
              <p className="wizard-hint">Pairing uses one code immediately. Headphones work best.</p>
            </div>
          )}
          {live ? (
            <div className="voice-actions">
              <Button type="button" onClick={voice.disconnect}>
                <Square size={14} />
                Disconnect
              </Button>
              <Button type="button" disabled={voice.busy} onClick={() => void voice.unpair()}>
                Unpair browser
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
