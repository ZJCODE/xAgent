# One persistent core per Agent

Each canonical Agent data directory has one operating-system lock and one runtime. The lock is acquired before constructing the Agent or recovering any work. API, Weixin and local voice share its Agent, attention loop, task scheduler and memory maintenance. A channel failure appears in status and does not stop the other channels.

## Commands

```sh
xagent run --agent atlas
xagent start --agent atlas
xagent stop --agent atlas
xagent restart --agent atlas
xagent status --agent atlas
xagent logs --agent atlas --follow
xagent channel weixin connect --agent atlas
xagent channel weixin disconnect --agent atlas
```

`run` is foreground; `start` is background. Ordinary startup, including Stop then Start, opens only the public API, unless `channels.api.enabled: false`. Weixin and Voice stay disconnected even when their configuration has `enabled: true`; default startup does not construct those adapters or inspect audio devices. Explicit restart restores channels that were connected, connecting or reconnecting, including Voice. Disconnected, disconnecting and failed channels remain offline, including a manually disconnected API. New configuration with `enabled: false` skips automatic restoration. `run/start/restart --channels api,weixin` explicitly overrides the default or restored selection for one launch, retaining the existing override of `enabled: false`. Restarting an already stopped Agent uses ordinary startup. Different Agents can run independently. Stop legacy channel processes before launching the core.

The Web Channels page lists every channel as configured/available, connecting, connected, reconnecting, disconnecting or failed. Configuration readiness is not proof of a successful remote handshake. Use Connect / Disconnect or `xagent channel <api|weixin|voice> connect|disconnect --agent atlas`. Start the Agent before using these controls. Disconnecting preserves configuration, credentials and history. Restart requests new connections using the previous intent; channels report connected only after successful transport readiness. Handshake/transport failures appear in channel status and runtime logs without stopping the other channels.

The terminal launcher exposes the same controls under **Channel → API / Weixin / Voice**. Its list and submenus display the owner's published channel state and capabilities. Connect/Disconnect are disabled while the Agent is stopped or unavailable; configuration remains accessible. **Refresh** reloads state after a handshake or cleanup completes. Failed cleanup exposes Disconnect again instead of allowing a second connection. Actions target the Agent directory shown in that menu, even if another client changes the active Agent. Configuration changes require restarting the Agent before connecting with the new settings.

The channel list uses a fixed order: Chat, API, Weixin, Voice. A status badge beside each channel name keeps Connected visible in green, transitions in yellow, failures in red and disconnected channels in gray. Status includes text and a symbol so it is readable without color.

The launcher overview shows Runtime and Web first, followed by API, Weixin and Voice, then Model, Search, Image and Tasks. Unconfigured entries remain summarized at the bottom.

Menu goal check: control one shared Agent identity, retaining user distinction and private/group coverage. The menu reuses runtime operations, preserving first-person journal perspective, unified memory and diary authority, Agent-governed sharing, source attribution and cross-channel continuity. Presentation changes only ordering and status emphasis. It creates no separate Agent or memory store.

Connection operations run inside the owner. Each channel has an operation lock and connection generation; stale callbacks cannot replace current status. Disconnect stops new input/background dispatch, allows up to 10 seconds for accepted work, then allows 5 seconds for cancellation and cleanup. The shared Agent inbox, attention consumer, diary maintenance and other channels remain available. Public API disconnect closes only the public listener and its requests/subscriptions; the internal API delivery sink and local clients remain available. Cleanup failure retains resources and blocks another instance until Disconnect succeeds.

Disconnected external channels do not receive new scheduled or subconscious dispatches. Reconnection wakes the scheduler and follows existing overdue/recurrence rules. Interrupted conversations and attention triggers are not replayed. Unconfirmed execution or delivery retains task receipts for review rather than automatically re-running tools. Configuration and identity edits still take effect after a full Agent restart.

The local control API provides `POST /runtime/channels/{channel}/connect` and `/disconnect`; Web uses `/api/channels/{channel}/connect` and `/disconnect`. Responses contain channel status/capabilities and a runtime snapshot. Unknown channels return 404, invalid configuration 422, unavailable lifecycle states 409, and cleanup failures 503. Connect returns the initial state and clients poll for readiness; Disconnect has a 20-second client request timeout.

CLI chat and observation connect to the local core and start it when necessary. Local Web chat uses the same core, including when the public HTTP API is disabled. Browsing history, memory, configuration and tasks offline does not construct an Agent or a model client. Passive Web notifications do not start it.

The private control socket is under `/tmp/xagent-<uid>/`, with a short name derived from the canonical data directory. Its directory is `0700`, and its socket is `0600`. It is separate from the optional public HTTP/WS interface. The public API does not add authentication; keep its existing network boundary.

## Admission and cancellation

Inputs retain the channel, user, room, source event ID, request ID and turn ID. Input history and acceptance are persisted before an `accepted` event is returned. Stable source IDs deduplicate within their channel, user and room. Explicit task retries use a new attempt ID.

One formal Agent turn runs at a time. Up to 32 turns may wait; queue and execution deadlines are 30 and 600 seconds. Observations are stored without immediately requesting a reply. Group messages already perceived through attention are not stored again when the Agent responds.

Model output is bounded on Responses, Chat Completions and Anthropic Messages alike. Main turns default to 8192 tokens; `provider.max_tokens` overrides that ceiling. Diary generations use at most 4096 tokens, working summaries 2048, and subconscious generations 2048. These background budgets also respect a smaller configured main ceiling. Bounded subconscious calls use the existing maintenance reasoning policy without changing foreground reasoning or output settings.

ModelClient owns retries and disables provider SDK retries on its model-only client copy. Retryable network/server failures get at most three attempts, with the existing exponential backoff. Each attempt has a 120-second wall-clock deadline, including all chunks of a streamed response. Streaming retries stop as soon as any model event has been exposed, so visible text and tool calls cannot be replayed. Cancellation propagates without retrying, and streams are closed after completion, failure or cancellation. The 600-second core deadline still bounds the whole sequence of model and tool iterations. Search and image generation keep their independent feature-client policies.

HTTP/WebSocket and scheduled API turns follow the active core's queue/run deadlines, with five seconds for its terminal receipt. The legacy HTTP timeout setting remains the fallback for standalone callable agents. Weixin follows the same core deadlines and adds 120 seconds for outbound delivery, rather than cutting off at 300 seconds. Runtime status reads the active inbox limits instead of reporting hard-coded copies.

Voice speculative generation is disabled by default. An unconfirmed STT partial must not start a durable, tool-capable Agent turn; the final attributed utterance starts the normal turn. Re-enabling speculation requires a candidate-only path with tools and durable writes deferred until transcript, speaker and participation are confirmed.

Budget goal check: the independent Agent identity, multi-user distinctions, private/group attribution and Agent-governed sharing are retained. Memory remains one first-person, diary-authoritative stream; notes remain regenerable projections with provenance. Unconfirmed speech does not become formal experience, and timeout receipts preserve attribution and continuity without creating another memory store.

Existing HTTP and WebSocket paths remain. Successful HTTP `/chat` responses and content events include `turn_id`, `event_id` and `request_id`. Cancel only your accepted turn:

```json
{"turn_id":"the accepted turn ID"}
```

Send that body to `POST /chat/stop`. Voice steering also identifies its turn and channel. Ordinary interrupted conversations are marked interrupted at restart and are not replayed. Previously generated messages and tool records remain visible.

## Task recovery

Task files and atomic claims remain in use. Operational receipts are under `.runtime/task_runs/`; they are not another memory store.

| Persisted stage | Startup action |
| --- | --- |
| `result_ready` | Reuse the prepared result and begin delivery |
| `succeeded` | Complete archival or schedule reconciliation only |
| `executing` or `delivering` without confirmation | Mark `needs_review`; do not repeat execution or sending |
| Legacy `.running` without sufficient evidence | Mark `needs_review` |
| `failed` / `needs_review` | Wait for explicit review and retry |

Unresolved recurring occurrences block subsequent execution. Retry creates a new attempt and preserves the previous receipt. Model errors are failures; success requires a final response or a valid attachment. API delivery succeeds only after session history is persisted. WebSocket events notify subscribers. Weixin retains its send acknowledgements; uncertain external sending requires review and is not promised to execute exactly once.

## Diary recovery

First-person Markdown remains the durable long-term memory. Each maintenance window generates all fragments before preparing its commit receipt, including source cursors, dates, original checksums and replacement bytes. It atomically replaces and syncs diary files before advancing the existing cursor.

Recovery completes a prepared write once, or advances the cursor if the file already contains that write. A checksum conflict stops that window for review without overwriting manual edits. Relationship cards and notes are derived views; their failure does not cause the committed diary to be appended again. Lock waits and file commits retain their locks until cancelled workers have actually stopped.

## Shutdown and service management

The core stops accepting work and drains within a 30-second budget. Unfinished work retains recovery evidence. An unresponsive writer keeps ownership until it stops or the process is terminated; another core cannot overlap it. Configure the macOS/Linux service manager to allow at least 35 seconds before forced termination, and use it for startup and crash restart. Cancellation is cooperative; blocking external tools ultimately require the process boundary. The runtime targets macOS and Linux, including Raspberry Pi and Linux servers.

### Restart latency

CLI restart, Web restart, `processes restart` and `update --restart` share one restart flow. Agent restart sends `POST /runtime/stop` with `{"restart": true}`; the owner atomically captures connection intent and closes admission, returning `restart_channels`. Repeated stop requests reuse the first snapshot, and pending Disconnect operations are excluded. The launcher waits for ownership release before starting a replacement with that selection, preserving an empty list as all channels disconnected. Snapshots are held only in memory for that restart, never read from cached status on ordinary startup. Older owners without this response field use a live pre-stop snapshot; an unreachable owner uses signal-based recovery and default startup with a warning if no trustworthy snapshot is available. Restart responses may include `restart_warnings` for disabled channels or observed connection failures; restarting the Agent does not wait for remote handshakes.

Update executes the new installation's restart command in a fresh interpreter. An update invoked from a version predating this behavior still uses that older worker's restart policy for the initial upgrade. Update failure, an already-current version, and opting out of restart leave the existing update behavior unchanged.

Restart drains accepted work and finalizes turn receipts without requesting a new model-generated diary window. Persisted message sources and prepared diary commits remain intact: startup recovers prepared commits and subsequent memory maintenance follows the existing batch rules. Ordinary Stop still attempts its final forced diary maintenance.

Heartbeat/scheduler shutdown overlaps the accepted-turn drain, and channel transports stop concurrently. New attention decisions are paused before the drain; closed channels cannot keep the shared attention loop spinning. The 30-second core budget and single-owner lock remain in force, including when a writer refuses cancellation.

Idle runtime notification streams wake immediately on the stop event. Web notification proxies observe downstream WebSocket disconnects and close their upstream streams even when no notifications arrive. Web shutdown allows up to five seconds for its remaining requests; it never stops the Agent core. Web startup waits for a PID-matched `/api/ready` response instead of an unconditional two-second survival wait. Agent startup polls `/runtime/ready` without repeatedly reading task and journal details.

Runtime status and logs include `lifecycle_timings.startup_seconds` (core initialization after ownership acquisition, excluding Python imports) and `shutdown_seconds`. These describe the runtime lifecycle, not a guarantee for an entire command with active conversations, external devices or slow disk writes.

Goal check: stable Agent identity, user distinction and private/group attribution are preserved. Journal generation remains first-person, unified and diary-authoritative; raw pending sources and recovery receipts are durable operational evidence, not separate memory silos. Sharing stays Agent-governed. Restart changes scheduling and transport cleanup, not source attribution or cross-channel continuity.

Settings and identity edits apply on restart. Web displays that pending state. Live mutations are routed through the owner; offline mutations acquire the same exclusive lock. Runtime status exposes connections, active turn, queue, task review count, diary backlog and recent maintenance. Logs include request, turn, task, run and attempt identifiers.

## Accelerated validation

```sh
python -m unittest discover -s tests
python scripts/runtime_soak.py --root /tmp/xagent-validation-fresh-directory
cd frontend && npm run build
```

The simulation advances the scheduler through 24 virtual hours, dispatches an hourly recurring task, submits three-channel traffic and duplicates, forces diary maintenance, simulates reconnects, and restarts the core twice. It checks persisted message counts, unique diary inputs, prepared task acknowledgements, single-turn execution and bounded file-handle use. The default has a 115-second total budget. It contacts no external model or bot. Separate fault regressions cover execution/delivery/archive interruption, diary conflicts and cursor failures, queue cancellation and cross-process ownership.

This is accelerated scenario validation; it does not measure actual 24-hour uptime or real device/network behaviour.

## Goal check: on-demand channels

One Agent identity and unified first-person diary remain shared across connections. User identity and private/group attribution follow the existing message paths. Connection state is operational state, not a new memory carrier; diary-backed memory stays authoritative. Sharing remains governed by the Agent, with unavailable recipients excluded from new deliveries. Source messages, tool effects and interruption receipts preserve attribution and continuity across disconnect/reconnect.
