# External devices as Agent execution endpoints

Hardware and software devices connect directly to the Agent's existing API,
execute capabilities they declare, and report environmental events. Each device
uses its own WebSocket; chat continues on `/ws/chat` with its existing protocol. One Agent process and one
API port host both routes. There is no separate Gateway or HTTP execution client.

New devices pair at runtime. Pairing makes a device eligible to connect and
execute capabilities. Every user who interacts with this Agent can discover
and use its paired devices. No device-access switch, caller list, or per-device
access configuration is needed; adding a device does not require an Agent restart.
Optional user-facing labels can be edited in **Agent → Devices → Edit details**.

## Execution flow

```text
Chat client ↔ /ws/chat ↔ Agent → device tool → in-process DeviceService
                                               ↕
External device ↔ /ws/devices/{device_id} ↔ online session
```

The generic `device` tool supports `action="list"` and `action="call"`. Listing
returns this Agent's online devices and capability descriptions, filtered by
optional `device_id`, `location`, or `capability`. Calls require an explicit
`device_id` and a registered capability; selectors never silently choose a
physical device or broadcast a command.

```python
device(action="list", location="office")
device(action="call", device_id="desk", capability="light.set", params={"on": True})
```

Capability names are arbitrary. Devices describe object parameters with JSON
Schema and validate them before executing. New capabilities require no new
Agent tools or framework code. Reconnect to replace a device's complete manifest.
Device metadata and capability descriptions are external data, not instructions.
Active device events enter the same Agent's shared environmental context without
a user, room, subscription, or immediate chat reply.

## First connection: pair once

For hardware implementation, open **Agent → Devices → Connection Help**.
It uses the same dialog interaction as API Help and includes the selected
Agent's device WebSocket address, pairing steps, registration/command/result
examples, and the complete hardware voice contract (section 6). API Help also
opens this same Device Connection Help directly. **Copy for Agent** copies the complete guide for another Agent to
write ESP32, Raspberry Pi, or other device code. Pairing codes and credentials
remain placeholders; supply the real code directly on the device when pairing.

1. Start the Agent Runtime and connect its API in **Channels**.
2. Open **Agent → Devices**, choose **Add device**, and copy the pairing code. Each code lasts five minutes,
   authorizes one new device, and replaces any previous unused code.
3. On the device, supply the Agent API address, a stable device ID of its own,
   and the code. Connect and send the normal `register` message.
4. Save the `paired` response's credential on the device. Future connections use
   that credential; they require no further pairing or configuration edits.

The `device` tool is always present. With no paired online devices, discovery
returns an empty list and execution returns an unavailable result. After
pairing, CLI, Web chat, and other channels use the same discovery and execution
path. Private conversations, groups, and new users all have the same device
access. User IDs, channel, and room context remain in the existing call trace
for attribution; they do not decide device access.

The page lists paired device IDs and saved basic details even while offline.
**Edit details** saves a device name, optional location, and optional description
of what it captures. These labels are Agent-local and survive runtime restarts
and device reconnections. Device-reported names and locations provide initial
defaults; subsequent registrations never overwrite user choices. Capabilities
remain device-reported and are available while online. The device tool lists
the effective name/location and the description, and filters by the effective
location. New environmental events include configured details as user-provided
labels, not live observations or instructions; historical events are unchanged.

`PATCH /api/devices/{device_id}` accepts only `name`, `location` (up to 256
characters each), and `description` (up to 2048). Omitted fields are preserved;
empty strings clear fields, and `null` restores device defaults. Profiles are
saved atomically in `.device_profiles.json`, separately from credentials and
live manifests. They are bound to the pairing identity, so a removed/re-paired
device cannot inherit old user labels. Offline editing uses the existing
management ownership boundary; live edits are forwarded to the Runtime.

**Remove** revokes a credential
and closes the current connection. Remove an existing ID before pairing it
again; a new pairing code cannot take over an already paired ID.

Pairing-code creation, cancellation, and removal use the existing
trusted management entry point. A local Web client forwards live operations to
the owning Runtime over its private socket, even when the public API is off.
Offline administration can read paired IDs and edit basic device details. It
cannot create a service or pair a device. Pairing requires a connected Agent API.

## Optional execution deadline

Normal device use needs no `devices` section in `config.yaml`. The only optional
setting is the command deadline; its default is ten seconds. Manual deadline
edits take effect after restart:

```yaml
devices:
  timeout_seconds: 10
```

Calls may override this deadline with `timeout_seconds` (0.1–120 seconds),
without changing the default for other capabilities. For capture followed by
media upload, use `device(action="call", device_id="camera", capability="camera.capture",
params={}, timeout_seconds=90)`. This deadline includes waiting for the device,
capture, upload, and result delivery. A deadline does not cancel a remote action.

Every paired device gets a distinct, generated credential bound to its ID and
this Agent. The Runtime creates one device service before assembling tools.
That service and the generic tool remain present for the lifetime of the Agent.

The existing API address, port, and connect/disconnect controls remain in use.
The default API port is 8010; use your configured port, such as 8013. If the API
is disabled or disconnected, device connections are unavailable. The Agent still
runs, and local chat and other channels retain their normal behavior.

All chat users can request device actions through the Agent. The Agent decides
which capabilities to invoke and what to share through its normal conversation
flow. Pairing authenticates execution devices. The existing chat and management
access boundaries remain in use; this feature does not add public chat authentication.

For public device access, expose **only** the device WebSocket route over WSS and
the `/api/device-media/` upload route over HTTPS, and
verify the server certificate. Keep chat and administration behind their existing
trusted access boundary. Device management rejects browser requests from other
hostnames, but this is not a substitute for administration authentication.
`127.0.0.1` only works for a device on the same machine;
an ESP32 needs the Agent machine's reachable IP or domain. The framework does
not change the API's listening address automatically.

## Device protocol

Connect to `/ws/devices/{device_id}` with
`Authorization: Bearer <pairing-code>` for the first connection, or
`Authorization: Bearer <saved-device-credential>` afterward. Send registration
within five seconds.

Browsers cannot set an `Authorization` header on a WebSocket. A browser may
pass the same pairing code (`pair_…`) or device credential (`dvc_…`) as
`?token=` on the control socket or on `/ws/devices/{device_id}/voice`. When
both are present, the header is used and the query value is ignored. Native
clients, including ESP devices, should keep using the header: a query token
can appear in proxy and server access logs.

The admin web client page at `/voice` prepares a hold-to-talk browser session automatically.
Continuous conversation waits for an explicit Start conversation action before connecting.
It requests a short-lived, single-use ticket from `POST /api/devices/voice-session`
on the trusted management origin, then opens
`wss://<agent-api>/ws/voice/{session_id}?token=…` on `web.api_url`.
Tickets expire after 60 seconds and authorize only that voice handshake; they
cannot enroll or control devices or upload media. Tickets remain in memory,
are invalidated when the API stops, and are never saved in browser storage.
Browser sessions do not create credentials or profiles and are excluded from
device lists. Independent tabs use separate session IDs.

The hello profile is `headset` with `echo_managed: true`. The page requests
microphone permission only when holding the talk button or starting continuous
conversation, sends microphone audio and plays TTS downlink over the same WebSocket. Continuous conversation
supports mute/unmute, ends on mode switches, and pauses when the page is hidden.
Returning requires Continue conversation; ordinary window blur does not pause it.
It retries failed connections three times with increasing delays, restoring the
continuous session's mute state. Permission errors stop without automatic retry.
Leaving the page closes the session and audio resources. Browser speech is treated
as directed conversation, independently of the room attention window.

If the Agent is stopped, Start Agent prepares it before connecting. There is no
separate Host voice channel to start. Shared voice settings live in Agent;
microphone and speaker selection belongs to the browser or device.

### Audio protocol 2

Browser and paired-device voice use one explicit protocol. Send
`{"type":"hello","audio_protocol":2,"codec":"opus","profile":"headset"}`
(or `codec: "pcm_s16le"`). The codec selects a complete mono profile:

| Codec | Microphone | Playback |
| --- | --- | --- |
| `opus` | 48 kHz, 20 ms, 24 kbps | 48 kHz, 20 ms, 32 kbps |
| `pcm_s16le` | 16 kHz, 20 ms | 24 kHz, 20 ms |

`ready` confirms `audio_protocol`, `codec`, both sample rates and both channel
counts before audio starts. Standard installations require PyAV 14–16; a server
codec initialization failure ends the session explicitly. Soniox and the internal
voice runtime continue using PCM16. No REST or configuration fields are added.

The browser self-tests native WebCodecs encoding and decoding at startup, including
Opus priming and tail trimming. It selects Opus only when the complete contract
works; otherwise it selects the PCM profile. Runtime codec failures end the session
and require user retry. A session never changes codec. Transient network failures
retain the three retries after 1, 2 and 4 seconds. Permission errors and playback
confirmation timeouts stop and require user action.

`ready` is sent only after the recognition sender begins consuming microphone
audio. Browser recognition disconnects and input overload close with retryable
code 1013, releasing capture before the next connection. Explicit browser
conversations keep recognition awake until the session ends or the page hides;
Paired room devices retain their idle sleep policy. Host audio capture is retired;
use the browser Voice page or a paired voice device.

### Hardware voice implementation

Hardware connects to `/ws/devices/{device_id}/voice`, sharing the browser Voice
tab's audio protocol and server speech/Agent pipeline. It uses a persistent device
credential instead of a temporary browser ticket. A voice-only device can pair
directly on this socket: authenticate with a pairing code, send `hello` within
five seconds, and durably save the credential returned in `ready`. No `register`
or capability manifest is needed on the voice socket. Subsequent connections
authenticate with that saved credential and repeat `hello` → `ready`. A device
with control and voice uses two sockets with the same ID/credential; complete
pairing on one before opening the other. A second voice connection replaces the
first for that device ID.

For initial hardware bring-up, use:

```json
{"type":"hello","audio_protocol":2,"codec":"pcm_s16le","profile":"room","echo_managed":false,"interruptions":false}
```

Wait for `ready` before sending microphone audio. PCM requires 16 kHz input and
24 kHz output, both mono signed 16-bit little-endian. A full 20 ms input payload
is 320 samples / 640 bytes; output is 480 samples / 960 bytes. Convert locally
when the audio hardware uses another rate. PCM needs no audio codec on the device;
the server installation still requires its normal voice codec dependencies.

Start each capture with `{"type":"input_start","stream_id":1}` and end it with
`{"type":"input_end","stream_id":1}`. Use increasing nonzero uint32 input stream
IDs per connection. Each binary WebSocket message is one 28-byte header plus one
payload. The header uses unsigned big-endian integers, `!4sIIQII`:

| Offset | Bytes | Field |
| --- | --- | --- |
| 0 | 4 | ASCII `XAV2` |
| 4 | 4 | Stream ID |
| 8 | 4 | Packet sequence, starting at zero per stream |
| 12 | 8 | Timestamp: `floor(previous_valid_samples * 1000000 / sample_rate)` |
| 20 | 4 | Valid mono sample count |
| 24 | 4 | Leading decoded samples to discard (zero for PCM) |
| 28 | remaining | PCM16LE bytes or one raw Opus packet |

For uplink, valid samples plus discard must not exceed 20 ms; a final partial
frame is allowed. Sequence increments by one, timestamps advance by valid samples,
and PCM payload length equals twice the valid sample count. For Opus, trim priming
and tail padding using `discard` and the valid sample count. Start a fresh encoder
per input capture and a fresh decoder per downlink playback stream. Audio is raw
payload, without WAV/Ogg/WebM containers or Base64. Pace uplink to real capture time;
more than 500 ms of queued microphone audio causes a retryable 1013 close.

On `playback_start`, use its `playback_id`, `stream_id`, codec, sample rate and
channels; reset playback counters and route matching binary packets into a bounded
speaker queue. Input/output stream ID counters are independent. Keep the network
receive loop running while playing. Report cumulative actual speaker-consumed
media samples using `playback_progress` with `playback_id`, `played_samples`, and
`sample_rate` (24000 for PCM, 48000 for Opus), periodically and after draining.
Exclude underrun silence, discarded samples and unplayed queued audio. Do not
estimate playback from reception, decoding or elapsed time. After `playback_done`
and hardware/DMA output has drained, send final progress followed by
`{"type":"playback_finished","playback_id":"..."}`. Missing completion causes
a playback timeout. Paired hardware currently supports confirmation at end of
playback; periodic progress also keeps the server's spoken-history accounting
current. The browser additionally uses a 300 ms progress-driven send window.

On `clear_playback`, stop the matching playback immediately and flush both
software/hardware queues. Ignore stale packets and controls. `transcript` and
`state` messages are optional display data, not playback completion signals.

For hardware without validated echo cancellation/isolation, explicitly disable
interruptions and stop forwarding microphone audio during playback. End capture
on playback start and resume with a new input stream ID after the speaker drains
and the acoustic tail settles. `echo_managed` is client metadata; it does not
activate server acoustic echo cancellation. Enable interruptions only after
simultaneous capture/playback works without self-triggering. `room` uses room
attention/wake rules; `headset` selects directed speech attention and does not
provide echo cancellation.

Voice control JSON is limited to 16 KiB and binary messages to 256 KiB. Application
`ping` receives `pong`; `stop` ends the whole voice session. After transient
disconnect/1013, clear queues and reconnect with bounded backoff/jitter, then
negotiate again before fresh capture. Never replay old audio. A 1008 close may
indicate invalid credentials or malformed protocol; inspect the reason. Diagnose
codec failures/playback timeouts instead of retrying indefinitely. Hardware
acceptance should cover persistent pairing, multiple turns, long reply completion,
network loss during playback, speaker echo, and audio clearing on interruption.

Binary messages consist of a 28-byte network-byte-order header followed by one
raw Opus packet or PCM16 little-endian payload. The header layout is `!4sIIQII`:
magic `XAV2`, uint32 stream ID, uint32 sequence, uint64 timestamp in microseconds,
uint32 valid mono samples and uint32 leading samples to discard. Timestamps refer
to valid media time from the beginning of the stream. Opus initialization metadata
supplies priming trim; final padding is excluded from valid samples. Packets have
no WebM/Ogg container. Each packet covers at most 20 ms, including leading discard.

`input_start` / `input_end` carry `stream_id`; IDs increase each time the microphone
opens. Each new stream resets decoder and resampler state. Older/ended-stream
audio is ignored; active-stream sequence/time gaps are rejected. `playback_start`
includes `playback_id`, numeric `stream_id`, `codec`, `sample_rate` and `channels`.
`playback_done` and `clear_playback` carry the same playback identity. Cleared
stream audio and callbacks cannot affect the next reply.

Playback confirmation is required. Clients send `playback_progress` with
`playback_id`, `played_samples` and `sample_rate`; the server clamps progress to
sent media. Browser progress also carries `rendered_samples`, the monotonic
count actually consumed by the render worklet before output-device latency.
It releases send capacity; `played_samples` remains the latency-adjusted spoken
ledger input. Neither network arrival nor decoding earns render credit. Counts
from old playback IDs are ignored; rendered counts beyond sent media are rejected.
After draining the renderer, clients send `playback_finished` with
the matching ID. Network delivery and decoder completion do not confirm playback.
The browser accounts for reported audio output latency plus one render quantum;
this is an estimate of output completion, not a hardware audibility guarantee.
The server waits for confirmation before ending playback and applying microphone
cooldown. Late IDs are ignored; interruption/disconnect cancels the wait. Missing
confirmation closes the connection after sent media duration plus five seconds.

A dedicated Worker owns the socket and codecs, with direct MessagePorts to capture
and playback AudioWorklets. The page owns resource lifecycle and conversation
intent through one session controller. Both worklets use fixed-capacity sample
rings and persistent resampling. Playback starts with a 60 ms jitter buffer;
underruns output silence without advancing media progress. Capture uses a pool of
eight reusable 20 ms buffers. Incoming/outgoing queues are bounded to 500 ms;
browser output uses a 300 ms consumption window across the network, decoder and
render queue. The worklet reports progress every 50 ms. A paused renderer earns
no capacity from wall time; network stalls cannot cause catch-up bursts exceeding
the window. Missing render progress for five seconds ends the session explicitly.
Existing paired devices retain 200 ms wall-clock pacing and their existing
playback confirmation contract. Overload still ends the session explicitly.

Voice requires HTTPS/localhost, AudioWorklet, module Workers and current Web Audio
support. There is no ScriptProcessor or prefixed AudioContext path. Protocol 1,
raw PCM messages, independent per-direction codec lists, `playback_ack` and
`played_bytes` are removed: existing device clients must migrate to protocol 2.
Update the server and shipped browser assets together. Both conversation modes
share this transport.


```json
{
  "type": "register",
  "device_id": "desk",
  "name": "Office desk",
  "location": "office",
  "metadata": {"model": "ESP32"},
  "capabilities": [
    {
      "name": "light.set",
      "description": "Set the desk light state",
      "parameters": {
        "type": "object",
        "properties": {"on": {"type": "boolean"}},
        "required": ["on"],
        "additionalProperties": false
      }
    }
  ]
}
```

On successful first registration, the server sends this message before commands:

```json
{"type":"paired","device_id":"desk","credential":"dvc_generated-secret"}
```

Save the credential durably before processing commands. ESP32 implementations
can use NVS; software devices can use a private local file. Do not write it to
logs or include it in metadata. Authenticated reconnections receive no `paired`
message and follow the original command/result protocol.

The server validates registration before consuming a pairing code. Consuming
the code and reserving the ID occur together, so two devices cannot both use
one code. If the server issued a credential but the device lost it before
saving, remove that paired ID in the page and pair again; credentials are not
redisplayed through the management API.

The server sends a command and the device returns its correlated result:

```json
{"type":"command","command_id":"cmd_123","capability":"light.set","params":{"on":true}}
```

```json
{"type":"result","command_id":"cmd_123","ok":true,"data":{"on":true}}
```

```json
{"type":"result","command_id":"cmd_123","ok":false,"error":"Invalid on parameter"}
```

Devices report success after execution and reject invalid parameters without
executing. Messages contain JSON only, with a 64 KiB limit. Transport ping and
pong handle connection health. Device sockets carry commands, results, and
environmental events, with no chat messages; a terminal
that also chats opens a separate `/ws/chat` connection on the same API port.
The public API sends a transport ping every 10 seconds and closes a connection
if the pong does not arrive within 10 seconds. The Devices page polls every
5 seconds, so an abrupt network loss normally appears offline within roughly
25 seconds. Devices must respond to WebSocket transport pings, even when idle.

### Upload a photo, recording, or file

Media travels as raw binary over HTTP(S); the existing device WebSocket carries
only the completed attachment reference. Both use the existing Agent API port.
No upload-session request, temporary token, Base64, or separate file server is
needed. Public devices must use HTTPS/WSS and verify the server certificate.
Loopback and trusted development environments may use HTTP/WS.

After pairing, generate a UUID for each file and send:

```http
PUT /api/device-media/desk/550e8400-e29b-41d4-a716-446655440000?filename=photo.jpg
Authorization: Bearer <saved-device-credential>
Content-Type: image/jpeg
Content-Length: <exact file length>
X-Content-SHA256: <optional SHA-256 hex digest>

<raw file bytes>
```

`filename` is optional and defaults to `attachment.bin`. The server sanitizes
the display filename and chooses the workspace path. `Content-Type` and an
exact, positive `Content-Length` are required; compressed request bodies are
not supported. The server measures actual received bytes and computes SHA-256,
checking `X-Content-SHA256` when supplied. Image headers, decodability and image
resource limits are checked; a filename or MIME declaration alone does not
make a file a valid image.

Only the durable device credential authenticates uploads, never a pairing
code. A registered WebSocket is not required for the upload itself. Ownership
is bound to this Agent, device ID, and the current pairing; removing and pairing
the same ID again does not grant access to its earlier uploads.

A completed upload returns HTTP 201 and metadata:

```json
{
  "attachment_id": "550e8400-e29b-41d4-a716-446655440000",
  "file_name": "photo.jpg",
  "mime_type": "image/jpeg",
  "size_bytes": 245760,
  "sha256": "<64 hexadecimal characters>"
}
```

Retry with the same ID, content, filename, MIME and size if the response was
lost. Identical retries return HTTP 200 with the original metadata; changes
return 409 and never overwrite the existing attachment. A concurrent upload
may return 429 (admission limit) or 409 (ID still uploading). Retransmitting the
file must not repeat the capture action. Bound retries within the command's
remaining deadline; retry transport errors, 408, 429 and transient server errors,
but do not retry authentication or validation errors.

Wait for successful upload confirmation, then include references in a result:

```json
{"type":"result","command_id":"cmd_123","ok":true,"data":{"captured":true},"attachments":[{"attachment_id":"550e8400-e29b-41d4-a716-446655440000"}]}
```

An environmental event can use the same optional `attachments` list. Each
reference contains only `attachment_id`: paths, URLs and source identities are
not accepted from devices. References must identify completed files from the
current pairing. Older devices can omit the field. Invalid event references
are rejected as protocol errors. An invalid command-result reference produces
`attachment_error` alongside the device's reported execution status and `data`;
an attachment failure does not turn a known execution into an unknown one.
Late results and replaced sessions are ignored before looking up attachments.

The `device` tool resolves references into the shared `WorkspaceAttachment`
format, with `path`, `blob_url`, MIME, filename, size, SHA-256, and device source.
Files reside in `assets/inbound/devices/` within the Agent workspace. Original
files are preserved. The Agent can use `see_image(path=...)` to inspect a stored
photo on its current turn, and `attach_artifact(path=...)` when it chooses to
deliver a file through Web, Weixin, or another supported channel.
Receiving an attachment does not automatically inject a photo into vision or
send it to users. Audio/video are stored attachments; transcription and video
understanding are separate capabilities. Events still only update shared
environmental context, without starting a conversation or sending a reply.

Default upload limits are configurable without changing the WebSocket frame
limit:

```yaml
devices:
  timeout_seconds: 10
  media:
    max_image_bytes: 10485760       # 10 MiB; PNG, JPEG, WebP
    max_file_bytes: 52428800        # 50 MiB; recordings and other files
    max_attachments: 8
    max_message_bytes: 209715200    # 200 MiB per result/event
    max_device_uploads: 1
    max_uploads: 4
    upload_timeout_seconds: 60
    idle_timeout_seconds: 15
    device_quota_bytes: 1073741824  # 1 GiB
    quota_bytes: 5368709120         # 5 GiB across this Agent's device media
```

Quotas count completed files and declared-size reservations for uploads in
progress. Uploading an identical existing file does not add a second quota
charge. Deleted workspace files release their index entries and quota on the
next reconciliation. API admission closure rejects new uploads; accepted ones
may finish within the Runtime's existing drain deadline. Revocation or expiry
of the drain cancels outstanding uploads and removes temporary files.

The private `.device-media/index.sqlite3` beside the Agent configuration stores
ownership and completion metadata; it is not a workspace attachment and is not
available through the workspace file API. A file is referenceable only after
durable storage and index commit. Startup recovery removes incomplete uploads
and temporary files. Completed files follow the normal workspace retention
policy, including unreferenced uploads, so a late attachment is never deleted
merely because no user message referenced it.

The upload route exists only on a Runtime-backed API and deliberately avoids
the `/api/devices` management forwarding path. Do not expose management or
general workspace routes to make device upload work. At a reverse proxy,
disable upload request buffering and set body/deadline limits to match the
application. The upload endpoint grants no public file-read capability.

| HTTP status | Meaning |
| --- | --- |
| 200 / 201 | Identical retry / newly committed attachment |
| 400 / 411 / 422 | Invalid body or checksum / missing length / invalid identifier or digest |
| 401 | Missing, invalid, revoked, or pairing-only credential |
| 408 / 409 | Upload deadline / attachment ID or metadata conflict |
| 413 / 415 | Size limit / unsupported encoding, MIME, or invalid image |
| 429 | Upload concurrency or media storage quota exceeded |
| 500 / 503 | Storage failure / device media API unavailable or closing |

Error responses contain `code` and `detail`; upload responses use
`Cache-Control: no-store`. Authentication credentials never appear in response
metadata or upload logs.

### Report an environmental event

After registering, a device may send an event on its existing connection,
including while a command is in progress:

```json
{
  "type": "event",
  "event_id": "evt_a7f4",
  "event_type": "touch",
  "context": "The head touch sensor detected a tap.",
  "data": {"area": "head", "gesture": "tap"}
}
```

`event_id` is a nonempty string of at most 128 characters, unique to an occurrence
on this device. Prefer a UUID rather than a counter that resets on restart.
`event_type` is a nonempty name of at most 128 characters. `context` is required,
nonblank readable information; `data` is an optional JSON object. The complete
UTF-8 frame remains limited to 64 KiB. Device identity comes from the authenticated
connection; do not include `device_id`, `user_id`, room, or attribution metadata
in the event. Registration and execution capabilities remain unchanged.

The API records events through `Agent.observe()` as environmental observations
with `source="device"`, the device ID, event type, original event ID, and optional
data. They have no human sender, recipient, or room. The model's recent experience
includes the device ID and event type alongside the observation, without claiming
who caused a touch. Duplicate IDs on the same device are stored once, including
after reconnect or Agent restart; different devices have separate event identities.

No chat client needs to be connected. Recording an event does not start a chat
turn, subscribe users, broadcast notifications, or alter an in-flight model request.
Subsequent context assembly uses the existing recent-event budget, working summary,
and memory maintenance. The normal background memory policy still applies.
This protocol sends no event acknowledgement or automatic reply, and does not
automatically replay device events after reconnect.

## Try the software execution endpoint

`examples/device_executor.py` implements the protocol with `counter.read` and
`counter.set`. Generate a pairing code in the page, then run:

```bash
XAGENT_PAIRING_CODE=code-from-page python examples/device_executor.py --api-url ws://127.0.0.1:8010 --device-id desk
```

The example saves credentials under `~/.local/share/xagent/device-credentials/`,
in a file per Agent address and device ID. Use `--credentials-file` to choose a
different path. Restart the same command without `XAGENT_PAIRING_CODE` and it
automatically uses the saved credential. Authentication rejection exits instead
of repeatedly attempting an expired code or revoked credential. For a removed
device, delete its local credential file and pair again.

Use your actual API port. Ask the Agent to list devices in the office, set the
counter to 41, and read it back. Any user can make these requests. The demo
counter itself is in memory; the credential persists across executor
restarts. Normal connection closure and API drain
use reconnect backoff rather than being treated as bad credentials. Other
devices implement the same protocol with their own handlers.

To exercise media upload with an existing photo or recording:

```bash
XAGENT_PAIRING_CODE=code-from-page python examples/device_executor.py --api-url ws://127.0.0.1:8010 --device-id camera --capture-file /absolute/path/photo.jpg --media-file /absolute/path/recording.wav
```

The demo declares `camera.capture` for the configured photo (a simulated capture)
and `media.read` for the configured recording/file. Replace the photo handler
with the actual camera implementation on hardware. It streams files over
HTTP(S), checks the response checksum, and retries uploads with the same UUID
within a bounded budget. It never repeats a capture or command on reconnect.
Ask the Agent to call the capability with `timeout_seconds=90`, view the photo,
and send it only when requested.

## Minimal management API

These routes belong to the trusted management API, not a public pairing server:

| Method and route | Purpose |
| --- | --- |
| `GET /api/devices` | Runtime/API availability, paired IDs and online manifests; no credentials |
| `POST /api/devices/pairing` | Create one new code; returns `pairing_code`, `expires_in`, `expires_at` |
| `DELETE /api/devices/pairing` | Cancel the unused code |
| `DELETE /api/devices/{device_id}` | Revoke a device credential and disconnect it |

Management responses use `Cache-Control: no-store`. Agent tools cannot generate
pairing codes, retrieve credentials, or revoke devices.

## State and lifecycle

One DeviceService belongs to each Agent Runtime. Its tools and API routes share
that exact instance. Online connections and pending commands are held in memory;
device discovery scans the online sessions. Upload work is tracked by the same
service and its Runtime drain. The separate `.devices.json` in the
Agent configuration directory contains only device IDs and credential digests.
Writes are atomic with file mode `0600`, owned by the Runtime; no raw credential,
manifest, command, or memory is stored there. Offline administration reads only
configuration and paired IDs and creates no device service or connections.

The Runtime opens device admission when the API is ready. Disconnecting the API
rejects new device registrations and commands, drains already accepted commands
within the existing channel deadline, and closes the sessions. Idle device
connections do not add a command-drain delay. API reconnection requires devices
to register again. Runtime shutdown keeps connections available during its
existing accepted-work drain and then explicitly cleans them up; no FastAPI
lifespan hook owns device cleanup.
New environmental events are ignored once device admission closes; command
results can still finish accepted work. Replaced or revoked connections cannot
submit new events.

Pairing codes are memory-only and invalidated by cancellation, replacement,
API disconnection, or Runtime shutdown. Device credentials
survive these events until explicitly removed. Credentials authenticate only
their originating Agent and device ID. Neither paired IDs nor credential
digests appear in the device tool's discovery results while offline.

Reconnection replaces the old session. Old cleanup and results cannot remove or
complete work on the new session. Results must match both the command ID and
its originating device session. Commands run one at a time per device; different
devices can run concurrently. The configured command deadline includes waiting
for a device's execution slot and transport delivery. At most 256 commands may
be pending. Devices should also serialize their handlers: a timeout does not
stop a remote operation that already started.

Results distinguish `success`, `error` (including rejection before sending),
and `unknown` (possibly sent, with no definitive outcome). Late results are
ignored, and commands are never automatically retried or replayed after a
reconnect or restart. Prefer explicit state changes over toggles, and read back
state after an uncertain result where supported. There are no durable execution
receipts or exactly-once guarantees.

## Goal check

Connection Help exports only connection instructions and protocol examples.
It does not export credentials, conversation history, identity instructions, or
memory; the following identity, sharing, and attribution boundaries still apply.

| Project principle | Effect |
| --- | --- |
| Identity | Devices extend the Agent's actions without changing its identity. |
| Multi-user distinction | All users can use paired devices; caller identity is still recorded without merging users. |
| Private and group interaction | The same tool and execution path serve both; room context does not select a physical device. |
| First-person memory and journal | Tool results enter the existing experience flow; device descriptions do not become identity instructions. |
| Unified memory | No separate memory per device or user is introduced. |
| Agent-governed sharing | Tools discover this Agent's paired devices; the Agent retains its execution and sharing judgment. |
| Diary as memory carrier | Connection state is operational data, not a competing long-term memory store. |
| Attribution and continuity | Device IDs and command IDs identify actions; saved credentials preserve device binding across restarts without replaying uncertain work. |


### Playback flow-control goal check

The Agent's identity, multi-user attribution, 1:1/group behavior, unified memory,
sharing rules and diary authority are unchanged. Render credit controls transport
only; latency-adjusted played samples still govern the first-person spoken ledger.
Playback IDs isolate sessions, and interruption/disconnect cancels blocked sends.
This preserves attribution and continuity without recording queued audio as heard.
