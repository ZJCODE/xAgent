import { Check, Copy, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { copyText } from "../lib/clipboard";
import { Markdown } from "./Markdown";
import { Button, IconButton } from "./ui";

interface DeviceConnectionHelpProps {
  open: boolean;
  currentAgent: string;
  apiUrl: string;
  onClose: () => void;
}

function websocketBase(value: string): string {
  try {
    const url = new URL(value);
    if (!["ws:", "wss:", "http:", "https:"].includes(url.protocol)) return "AGENT_WS_URL";
    url.protocol = ["https:", "wss:"].includes(url.protocol) ? "wss:" : "ws:";
    url.username = url.password = url.search = url.hash = "";
    return url.toString().replace(/\/+$/, "");
  } catch {
    return "AGENT_WS_URL";
  }
}

function connectionGuide(agent: string, base: string): string {
  const httpBase = base.replace(/^wss:/, "https:").replace(/^ws:/, "http:");
  const registration = JSON.stringify({
    type: "register", device_id: "my-device", name: "My device", location: "office",
    capabilities: [{
      name: "example.echo", description: "Return the supplied text",
      parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"], additionalProperties: false },
    }],
  }, null, 2);
  return `## Build a device endpoint for xAgent

Write runnable device-side code for my hardware (ESP32, Raspberry Pi, or any WebSocket-capable platform), with dependencies and build/run instructions. Keep hardware pins and handlers configurable; ask for my target hardware and desired capabilities if unspecified.

### 1. Connect to the Agent

- Selected Agent: ${JSON.stringify(agent || "Selected Agent")}
- Agent API base: \`${base}\`
- Device WebSocket: \`${base}/ws/devices/{device_id}\`
- The device initiates one outbound connection to the Agent's API, on the same port as chat. This socket carries commands, results, and environmental events; it does not use \`/ws/chat\`.
- Use an address reachable from the device. \`127.0.0.1\`/\`localhost\` only work on the Agent's own machine; use its reachable LAN IP or domain for external hardware. Public connections use WSS with certificate verification. Network access to the Agent API must be available.

### 2. Pair once, register on every connection

Choose a stable unique \`device_id\` (1–128 ASCII letters/digits or \`_ . : -\`, starting with a letter/digit). Start the Agent and connect its API in Channels, then open Agent → Devices → Add device for a pairing code (valid for 5 minutes, one use).

Set the WebSocket request header to \`Authorization: Bearer <PAIRING_CODE>\` on the first connection. Within 5 seconds of connecting, send this JSON registration, using the same ID as the URL:

\`\`\`json
${registration}
\`\`\`

Declare the actual device capabilities with descriptions and object-parameter JSON Schema; replace \`example.echo\` with matching handlers as needed. Capability names are arbitrary and need no framework changes.

On first pairing, receive:
\`\`\`json
{"type":"paired","device_id":"my-device","credential":"<DEVICE_CREDENTIAL>"}
\`\`\`
Save the credential durably before handling commands (ESP32: NVS; Raspberry Pi: a private file). Subsequent connections use \`Authorization: Bearer <DEVICE_CREDENTIAL>\` and send registration again. They receive no new \`paired\` message. Keep credentials out of logs and metadata. Lost/revoked credentials require removing the device in the page and pairing again.

### 3. Execute and return the result

Agent → device:
\`\`\`json
{"type":"command","command_id":"cmd_123","capability":"example.echo","params":{"text":"hello"}}
\`\`\`
Device → Agent: return one result per command, success or failure (\`ok\` must be a JSON boolean):
\`\`\`json
{"type":"result","command_id":"cmd_123","ok":true,"data":{"text":"hello"}}
{"type":"result","command_id":"cmd_123","ok":false,"error":"Invalid parameters or execution failed"}
\`\`\`

### 4. Report environmental events (optional)

After registration, send an event on this same socket when a sensor detects something:
\`\`\`json
{"type":"event","event_id":"evt_a7f4","event_type":"touch","context":"The head touch sensor detected a tap.","data":{"area":"head","gesture":"tap"}}
\`\`\`

Use a unique nonempty \`event_id\` of at most 128 characters per occurrence (prefer a UUID); reuse that ID only when resending the same event. \`event_type\` is a nonempty name of at most 128 characters, \`context\` is required nonblank readable information, and \`data\` is an optional JSON object. The server takes the device identity from this connection; do not supply a device ID, user ID, room, or attribution metadata in the event.

Events are stored once per device and event ID as observations in this Agent's shared context. They do not start a chat turn or notify users; no chat client or subscription is needed. Future context assembly can use them, while a model request already in progress is unchanged. There is no event acknowledgement or automatic reply. Events can be interleaved with command results; events reported while the API is draining are ignored.

### 5. Upload photos, recordings, or files (optional)

After pairing, upload the raw file bytes directly to:
\`PUT ${httpBase}/api/device-media/{device_id}/{attachment_id}?filename=photo.jpg\`

Generate a UUID \`attachment_id\` per file. Use \`Authorization: Bearer <DEVICE_CREDENTIAL>\`, the matching \`Content-Type\`, and the exact \`Content-Length\`; optionally send the SHA-256 hex digest in \`X-Content-SHA256\`. Pairing codes cannot upload files. Do not put file bytes or Base64 into WebSocket JSON. Public uploads use HTTPS with certificate verification.

Wait for HTTP 201 (created) or 200 (identical retry). The response contains \`attachment_id\`, \`file_name\`, \`mime_type\`, \`size_bytes\`, and \`sha256\`. A lost response may be retried using the same UUID, content, and metadata; changed content or metadata returns 409. Retrying an upload must never repeat the physical capture. Use a bounded retry budget for transport errors, 408, 429, or transient server failures.

Then include the completed reference in a result or event:
\`\`\`json
{"type":"result","command_id":"cmd_123","ok":true,"data":{"captured":true},"attachments":[{"attachment_id":"550e8400-e29b-41d4-a716-446655440000"}]}
\`\`\`

Defaults: PNG/JPEG/WebP images up to 10 MiB, other files up to 50 MiB; at most 8 attachments and 200 MiB per result/event; one upload per device, four per Agent. Uploads have a 60-second total deadline and 15-second idle deadline. Limits can be configured in \`devices.media\`. References must belong to this device's current pairing. Devices cannot choose a workspace path or arbitrary attachment URL.

The Agent receives stored attachment metadata and can choose to inspect images with \`see_image\` or deliver files with \`attach_artifact\`. Uploading and reporting an environmental event do not start a chat turn or automatically send anything to users. Ask the Agent to use \`timeout_seconds=90\` when calling a capture capability so its deadline covers capture, upload, and the final result.

### 6. Transport and reconnect

Validate parameters on the device; serialize its command handlers and preserve the exact \`command_id\`. Use UTF-8 JSON text messages of at most 64 KiB and WebSocket ping/pong. Reconnect with backoff after disconnection (including normal closure or temporary-unavailability code 1013), then register again. An authentication rejection needs a fresh valid credential/pairing code.

Never replay or automatically retry actions or old results after reconnecting. The Agent's default command timeout is 10 seconds; timeout does not stop an operation already running on the device.

All users talking to this Agent can discover and call its online paired devices through the generic \`device\` tool. Implement only the device endpoint; pairing and routing are already provided by xAgent.`;
}

export function DeviceConnectionHelp({ open, currentAgent, apiUrl, onClose }: DeviceConnectionHelpProps) {
  const guide = useMemo(() => connectionGuide(currentAgent, websocketBase(apiUrl)), [currentAgent, apiUrl]);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);
  useEffect(() => { setCopyState("idle"); }, [open, guide]);
  useEffect(() => {
    if (copyState !== "copied") return;
    const timer = window.setTimeout(() => setCopyState("idle"), 1800);
    return () => window.clearTimeout(timer);
  }, [copyState]);
  if (!open) return null;

  return <div className="modal-overlay help-dialog-overlay" role="presentation" onClick={onClose}>
    <div className="modal-card help-dialog device-connection-help" role="dialog" aria-modal="true"
      aria-labelledby="device-help-title" aria-describedby="device-help-description" onClick={(event) => event.stopPropagation()}>
      <div className="modal-header help-dialog-header">
        <div>
          <h3 id="device-help-title">Device Connection Help</h3>
          <p id="device-help-description">One protocol for ESP32, Raspberry Pi, and other hardware. Copy the guide to ask another Agent for device code.</p>
        </div>
        <IconButton type="button" onClick={onClose} title="Close Device Connection Help" aria-label="Close Device Connection Help" autoFocus><X size={16} /></IconButton>
      </div>
      <div className="modal-body help-dialog-body">
        <Markdown content={guide} renderImages={false} />
      </div>
      <div className="modal-footer">
        <span className="wizard-hint" role="status">{copyState === "failed" ? "Copy failed. Select the guide and copy it manually." : "Paste the full guide into another Agent."}</span>
        <Button type="button" variant="primary" aria-live="polite" onClick={() => void copyText(guide).then((copied) => setCopyState(copied ? "copied" : "failed"))}>
          {copyState === "copied" ? <Check size={14} /> : <Copy size={14} />}
          {copyState === "copied" ? "Copied" : "Copy for Agent"}
        </Button>
      </div>
    </div>
  </div>;
}
