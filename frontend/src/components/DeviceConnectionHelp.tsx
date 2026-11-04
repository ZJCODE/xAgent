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

Write runnable device-side code for my hardware (ESP32, Raspberry Pi, or any WebSocket-capable platform), with dependencies and build/run instructions. Keep hardware pins and handlers configurable; ask for my target hardware, audio peripherals/pins, and desired capabilities if unspecified. Implement only the features my hardware needs.

**Choose a connection:** For commands, sensors, or file uploads, follow sections 2–5. For a microphone and speaker, follow section 6; a voice-only device can pair there directly without a control socket or capability registration. A device that needs both uses two sockets with the same device ID and saved credential; complete pairing on one socket before opening the other.

### 1. Connect to the Agent

- Selected Agent: ${JSON.stringify(agent || "Selected Agent")}
- Agent API base: \`${base}\`
- Control & sensor WebSocket: \`${base}/ws/devices/{device_id}\`
- Real-time voice WebSocket (optional): \`${base}/ws/devices/{device_id}/voice\`
- The device initiates outbound connections to the Agent's API, on the same port as chat. \`/ws/devices/{device_id}\` carries commands, results, and environmental events; \`/ws/devices/{device_id}/voice\` carries audio protocol 2 (PCM16LE or Opus) and voice control frames. It shares the Voice tab’s audio protocol and server voice pipeline, with persistent device pairing.
- Use an address reachable from the device. \`127.0.0.1\`/\`localhost\` only work on the Agent's own machine; use its reachable LAN IP or domain for external hardware. Public connections use WSS with certificate verification. Network access to the Agent API must be available.

### 2. Device control: pair once, register on every connection

Choose a stable unique \`device_id\` (1–128 ASCII letters/digits or \`_ . : -\`, starting with a letter/digit). Start the Agent and connect its API, then open **Devices → Add device** for a pairing code (valid for 5 minutes, one use).

Set the WebSocket request header to \`Authorization: Bearer <PAIRING_CODE>\` on the first connection. Within 5 seconds of connecting, send this JSON registration, using the same ID as the URL:

\`\`\`json
${registration}
\`\`\`

Declare the actual device capabilities with descriptions and object-parameter JSON Schema; replace \`example.echo\` with matching handlers as needed (or pass \`"capabilities": []\` for sensor-only devices). Capability names are arbitrary and need no framework changes.

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

### 6. Device voice: microphone and speaker (optional)

Speech recognition, Agent replies/tools, and speech synthesis run on the server. The device handles audio capture, transport, and playback. Configure the Agent’s voice provider in **Agent** and start its runtime/API first; provider API keys stay on the server. This endpoint uses the same audio protocol and voice service as the Voice tab. Hardware uses persistent pairing on the device endpoint, rather than the browser’s temporary session tickets.

#### Pair and negotiate

Connect to \`${base}/ws/devices/{device_id}/voice\` with \`Authorization: Bearer <DEVICE_CREDENTIAL>\`. A voice-only device can instead use a one-time pairing code from **Devices → Add device** on its first connection, following the device ID and network rules in sections 1–2. Send this JSON text frame within 5 seconds; do not send a \`register\` frame on the voice socket:

\`\`\`json
{"type":"hello","audio_protocol":2,"codec":"pcm_s16le","name":"Living Room Speaker","location":"living_room","profile":"room","echo_managed":false,"interruptions":false}
\`\`\`

Wait for \`ready\` before starting capture. Example response for first-time voice pairing:

\`\`\`json
{"type":"ready","device_id":"my-device","room_name":"living_room","audio_protocol":2,"codec":"pcm_s16le","input_sample_rate":16000,"input_channels":1,"output_sample_rate":24000,"output_channels":1,"credential":"<DEVICE_CREDENTIAL>"}
\`\`\`

Save \`credential\` durably immediately, before streaming audio or opening the control socket. Subsequent connections send the saved credential and a fresh \`hello\`; \`ready\` has no new credential. Name/location seed device details; existing user-edited details take precedence. A second voice connection with the same device ID replaces the first, so use a unique ID per device.

#### Select one audio format per connection

| Codec | Microphone uplink | Speaker downlink | Payload per full 20 ms frame |
| --- | --- | --- | --- |
| \`pcm_s16le\` | 16 kHz mono signed PCM16 little-endian | 24 kHz mono signed PCM16 little-endian | 640 bytes up / 960 bytes down |
| \`opus\` | 48 kHz mono | 48 kHz mono | One raw Opus packet; variable bytes |

Start with PCM to validate capture and playback; use Opus when the target has a compatible encoder/decoder and bandwidth requires compression. The server sends Opus at 32 kbps; the browser’s uplink uses 24 kbps. Do not send WAV headers, Ogg/WebM containers, Base64, or bare PCM without the protocol header. Use the negotiated rates from \`ready\`; convert locally if the audio hardware runs at a different rate. Codec cannot change during a connection. Arbitrary rates, stereo, and protocol 1 are unsupported.

#### Capture and binary packet layout

Send \`{"type":"input_start","stream_id":1}\`, followed by binary audio messages paced to real capture time. End that capture with \`{"type":"input_end","stream_id":1}\`. Each new capture on this connection uses a strictly increasing nonzero uint32 stream ID. Sequence and cumulative sample counts reset to zero for each capture. Continuous listening can keep one input stream open; \`input_end\` ends capture, not the voice session, and does not force an immediate recognition result.

Each binary WebSocket message contains a 28-byte header followed by one audio payload. All header integers are **unsigned big-endian** (network byte order); PCM payload samples are **little-endian**. Header format: \`!4sIIQII\`.

| Offset | Bytes | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | magic | ASCII \`XAV2\` |
| 4 | 4 | stream | Input stream ID, or downlink \`stream_id\` from \`playback_start\` |
| 8 | 4 | sequence | Starts at 0; increases by 1 per packet |
| 12 | 8 | timestamp | Microseconds: \`floor(previous_valid_samples * 1000000 / sample_rate)\` |
| 20 | 4 | samples | Valid mono samples in this packet, excluding discarded samples |
| 24 | 4 | discard | Leading decoded samples to discard; always 0 for PCM |
| 28 | remaining | payload | PCM16LE bytes or one raw Opus packet |

Use 20 ms frames (320 input samples for PCM, 960 for Opus); a final partial frame is allowed. For uplink, \`samples + discard\` must not exceed 20 ms. PCM payload length must equal \`samples * 2\`. For Opus, account for encoder priming/tail padding: decode, discard the leading \`discard\` samples, and retain exactly \`samples\`; timestamps advance by valid samples only. Start a fresh Opus encoder per capture and a fresh decoder per downlink playback stream.

Example PCM uplink: first packet has \`stream=1, sequence=0, timestamp=0, samples=320, discard=0\` plus 640 payload bytes; the next has \`sequence=1, timestamp=20000\`. Send live audio continuously rather than uploading a recording in a burst: the server rejects more than 500 ms of queued input. Reassemble fragmented WebSocket messages before parsing packets.

#### Playback, progress, and interruption

Server control messages are JSON text; audio messages are binary. A normal downlink looks like:

\`\`\`json
{"type":"playback_start","playback_id":"p1","stream_id":1,"codec":"pcm_s16le","sample_rate":24000,"channels":1}
{"type":"playback_done","playback_id":"p1","stream_id":1,"codec":"pcm_s16le"}
\`\`\`

Binary audio arrives between these messages. On \`playback_start\`, reset the decoder, playback queue, and cumulative played-sample counter for that playback. Match audio headers to its stream ID. Continue receiving WebSocket messages while feeding the speaker through a bounded queue; do not block the network receive loop on audio output.

Report cumulative **audio samples actually consumed by the speaker**, excluding silence inserted for underruns, padding, and dropped audio. Send progress periodically (for example every 100 ms) and once more when drained:

\`\`\`json
{"type":"playback_progress","playback_id":"p1","played_samples":2400,"sample_rate":24000}
\`\`\`

Use \`sample_rate:48000\` for Opus. Count from zero per playback; never infer progress from bytes received, decoded buffers, or elapsed wall time. After \`playback_done\` **and** the last valid sample has drained through the speaker/DMA output, send final progress and then \`{"type":"playback_finished","playback_id":"p1"}\`. The server waits for this confirmation before completing playback; a missing confirmation ends the session with a playback timeout.

On \`{"type":"clear_playback","playback_id":"p1","stream_id":1,"codec":"pcm_s16le"}\`, immediately stop that playback and clear its software and hardware buffers. Ignore late packets and controls for cleared/old playbacks so cancelled speech cannot resume. Input and output stream IDs are independent; track them separately. Optional \`transcript\` and \`state\` messages can drive a display; they do not replace playback controls. Handle \`error\` messages visibly and ignore unknown server message types for forward compatibility.

#### Room audio and echo

Use \`profile:"room"\` for a room speaker: room attention/wake rules apply, so background speech does not necessarily trigger a reply. \`profile:"headset"\` selects a directed speech attention policy; it does not provide echo cancellation.

For initial hardware bring-up, explicitly send \`echo_managed:false, interruptions:false\`, and stop forwarding microphone audio while the speaker is playing. End capture at playback start, then resume with a new input stream ID after output drains and a short acoustic settling interval. This avoids feeding the Agent’s speech back into recognition. \`echo_managed\` only describes the client; setting it does not enable server-side acoustic echo cancellation. Enable \`interruptions:true\` only after the device’s echo cancellation/acoustic isolation works during simultaneous capture and playback. Handle \`clear_playback\` promptly when the server interrupts speech.

#### Voice reconnect and acceptance checks

Keep voice control JSON under 16 KiB and binary messages under 256 KiB (normal audio packets are much smaller). Use WebSocket ping/pong or \`{"type":"ping"}\` → \`{"type":"pong"}\`. To end the session, send \`{"type":"stop"}\` and release capture, playback, and socket resources; \`stop\` ends the conversation rather than just interrupting one reply.

After a transient disconnect or 1013, clear all audio queues, reconnect with bounded exponential backoff and jitter, authenticate with the saved credential, and repeat \`hello\` → \`ready\` before capturing fresh audio. Never replay old microphone buffers. A 1008 close can mean an authentication **or protocol** error: inspect the close reason and fix the credential/frame instead of retrying indefinitely. Codec errors or playback timeouts need diagnosis before retrying. Reconnect voice with \`hello\`; only the separate control socket uses \`register\`.

Validate on the target hardware: first pairing and saved-credential reconnect; multiple conversation turns; an audible reply longer than one second with sample-accurate completion; disconnect during playback without stale audio on reconnect; no self-trigger from speaker echo; and prompt clearing of audio if interruptions are enabled. Provide build/run instructions and identify any board-specific audio assumptions. Hardware validation is separate from server protocol tests.

### 7. Control socket transport and reconnect

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
          <p id="device-help-description">Connect ESP32, Raspberry Pi, and other hardware for commands, sensors, and voice. Copy the guide to ask another Agent for device code.</p>
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
