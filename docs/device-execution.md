# External devices as Agent execution endpoints

Hardware and software devices connect directly to the Agent's existing API and
execute capabilities they declare. Each device uses its own WebSocket; chat
continues on `/ws/chat` with its existing protocol. One Agent process and one
API port host both routes. There is no separate Gateway or HTTP execution client.

New devices pair at runtime. Pairing makes a device eligible to connect and
execute capabilities. Every user who interacts with this Agent can discover
and use its paired devices. No device-access switch, caller list, or per-device
configuration is needed; adding a device does not require an Agent restart.

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

## First connection: pair once

For hardware implementation, open **Agent → Devices → Connection Help**.
It uses the same dialog interaction as API Help and includes the selected
Agent's device WebSocket address, pairing steps, and registration/command/result
examples. **Copy for Agent** copies the complete guide for another Agent to
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

The page lists paired device IDs even while offline. Names, locations, and
capabilities are shown when devices are online. **Remove** revokes a credential
and closes the current connection. Remove an existing ID before pairing it
again; a new pairing code cannot take over an already paired ID.

Pairing-code creation, cancellation, and removal use the existing
trusted management entry point. A local Web client forwards live operations to
the owning Runtime over its private socket, even when the public API is off.
Offline administration only reads configuration and paired IDs. It cannot
create a service or pair a device. Pairing requires a connected Agent API.

## Optional execution deadline

Normal device use needs no `devices` section in `config.yaml`. The only optional
setting is the command deadline; its default is ten seconds. Manual deadline
edits take effect after restart:

```yaml
devices:
  timeout_seconds: 10
```

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
within five seconds:

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
pong handle connection health. Device sockets carry no chat messages; a terminal
that also chats opens a separate `/ws/chat` connection on the same API port.

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
device discovery scans the online sessions. The separate `.devices.json` in the
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
