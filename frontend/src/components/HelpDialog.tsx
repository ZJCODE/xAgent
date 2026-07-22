import { Check, Copy, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button, IconButton } from "./ui";

interface HelpDialogProps {
  open: boolean;
  currentAgent: string;
  directApiUrl: string;
  onClose: () => void;
}

type CopyTarget = "http" | "websocket";
type CopyState = { target: CopyTarget; failed: boolean } | null;

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function websocketUrl(baseUrl: string): string {
  if (!baseUrl) return "DIRECT_AGENT_API_URL/ws/chat";
  try {
    const url = new URL(baseUrl);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `${url.pathname.replace(/\/+$/, "")}/ws/chat`;
    return url.toString().replace(/\/$/, "");
  } catch {
    return `${trimTrailingSlash(baseUrl)}/ws/chat`;
  }
}

export function HelpDialog({ open, currentAgent, directApiUrl, onClose }: HelpDialogProps) {
  const [copyState, setCopyState] = useState<CopyState>(null);
  const copyResetTimer = useRef<number | null>(null);
  const proxyUrl = window.location.origin;
  const exampleApiUrl = directApiUrl ? trimTrailingSlash(directApiUrl) : "DIRECT_AGENT_API_URL";
  const directWebsocketUrl = websocketUrl(directApiUrl);

  const httpExample = useMemo(() => `curl -X POST '${exampleApiUrl}/chat' \\
  -H 'Content-Type: application/json' \\
  -d '{
    "user_id": "api_demo",
    "user_message": "Hello, xAgent!"
  }'`, [exampleApiUrl]);

  const websocketExample = useMemo(() => `const socket = new WebSocket("${directWebsocketUrl}");

socket.addEventListener("open", () => {
  socket.send(JSON.stringify({
    user_id: "api_demo",
    user_message: "Hello, xAgent!",
    stream: true,
  }));
});

socket.addEventListener("message", ({ data }) => {
  const event = JSON.parse(data);

  if (event.type === "message_delta") console.log("delta:", event.delta);
  if (event.type === "message_done") console.log("message:", event.content);
  if (event.type === "error") console.error(event.error);
  if (event.type === "done") socket.close();
});`, [directWebsocketUrl]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  useEffect(() => () => {
    if (copyResetTimer.current !== null) window.clearTimeout(copyResetTimer.current);
  }, []);

  useEffect(() => {
    if (!open) setCopyState(null);
  }, [open]);

  const copyExample = async (target: CopyTarget, value: string) => {
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        copied = true;
      }
    } catch {
      copied = false;
    }
    if (!copied) {
      const textArea = document.createElement("textarea");
      textArea.value = value;
      textArea.setAttribute("readonly", "");
      textArea.style.position = "fixed";
      textArea.style.opacity = "0";
      document.body.appendChild(textArea);
      textArea.select();
      copied = document.execCommand("copy");
      textArea.remove();
    }
    setCopyState({ target, failed: !copied });
    if (copyResetTimer.current !== null) window.clearTimeout(copyResetTimer.current);
    copyResetTimer.current = window.setTimeout(() => setCopyState(null), 1800);
  };

  const copyButtonContent = (target: CopyTarget) => {
    if (copyState?.target !== target) return <><Copy size={14} /><span>Copy</span></>;
    if (copyState.failed) return <><X size={14} /><span>Copy failed</span></>;
    return <><Check size={14} /><span>Copied</span></>;
  };

  if (!open) return null;

  return (
    <div className="modal-overlay help-dialog-overlay" role="presentation" onClick={onClose}>
      <div
        className="modal-card help-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="api-help-title"
        aria-describedby="api-help-description"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header help-dialog-header">
          <div>
            <h3 id="api-help-title">API Help</h3>
            <p id="api-help-description">Talk to the selected Agent over HTTP or WebSocket.</p>
          </div>
          <IconButton type="button" onClick={onClose} title="Close API Help" aria-label="Close API Help" autoFocus>
            <X size={16} />
          </IconButton>
        </div>

        <div className="modal-body help-dialog-body">
          <p className="help-intro">
            Choose the direct address when your client should always talk to this Agent, or use the Web Proxy when it
            should follow whichever Agent is selected in this page.
          </p>

          <dl className="help-connection-details">
            <div>
              <dt>Current Agent</dt>
              <dd>{currentAgent || "No Agent selected"}</dd>
            </div>
            <div>
              <dt>Direct Agent API</dt>
              <dd>
                <code>{directApiUrl || "Unavailable"}</code>
                <span>Changes when you switch Agents. The examples below use this address.</span>
              </dd>
            </div>
            <div>
              <dt>Web Proxy</dt>
              <dd>
                <code>{proxyUrl}</code>
                <span>Stays fixed and forwards requests to the Agent currently selected in the Web UI.</span>
              </dd>
            </div>
          </dl>

          {!currentAgent ? (
            <div className="warning-strip help-agent-warning" role="status">
              Create or select an Agent before running these examples.
            </div>
          ) : null}

          <section className="help-api-section" aria-labelledby="http-example-title">
            <div className="help-section-heading">
              <div>
                <span className="help-method-badge">POST</span>
                <h4 id="http-example-title">HTTP request</h4>
              </div>
              <code>{exampleApiUrl}/chat</code>
            </div>
            <p>
              Send a JSON body with a stable <code>user_id</code> and the text in <code>user_message</code>. The
              successful JSON response contains the Agent result in <code>reply</code>.
            </p>
            <div className="help-code-block">
              <div className="help-code-toolbar">
                <span>Terminal</span>
                <Button type="button" variant="ghost" className="help-copy-button" aria-live="polite" onClick={() => void copyExample("http", httpExample)}>
                  {copyButtonContent("http")}
                </Button>
              </div>
              <pre><code>{httpExample}</code></pre>
            </div>
          </section>

          <section className="help-api-section" aria-labelledby="websocket-example-title">
            <div className="help-section-heading">
              <div>
                <span className="help-method-badge help-method-badge-ws">WS</span>
                <h4 id="websocket-example-title">Streaming WebSocket</h4>
              </div>
              <code>{directWebsocketUrl}</code>
            </div>
            <p>
              Run this in a browser or JavaScript client. Streamed replies arrive as
              <code> message_delta</code> events, completed messages as <code>message_done</code>, failures as
              <code> error</code>, and the turn ends with <code>done</code>.
            </p>
            <div className="help-code-block">
              <div className="help-code-toolbar">
                <span>Browser JavaScript</span>
                <Button type="button" variant="ghost" className="help-copy-button" aria-live="polite" onClick={() => void copyExample("websocket", websocketExample)}>
                  {copyButtonContent("websocket")}
                </Button>
              </div>
              <pre><code>{websocketExample}</code></pre>
            </div>
          </section>

          <div className="help-security-note">
            <strong>Security note</strong>
            <span>Neither address adds a separate authentication layer. Do not expose them directly to an untrusted network.</span>
          </div>
        </div>
      </div>
    </div>
  );
}
