# One persistent core per Agent

Each canonical Agent data directory has one operating-system lock and one runtime. The lock is acquired before constructing the Agent or recovering any work. API, Feishu, Weixin and local voice share its Agent, attention loop, task scheduler and memory maintenance. A channel failure appears in status and does not stop the other channels.

## Commands

```sh
xagent run --agent atlas
xagent start --agent atlas
xagent stop --agent atlas
xagent restart --agent atlas
xagent status --agent atlas
xagent logs --agent atlas --follow
```

`run` is foreground; `start` is background. The default channels come from configuration. `run/start --channels api,feishu` overrides that selection for one launch. Channel setup and voice device queries remain available, but channels do not have separate start/stop commands. Stop legacy channel processes before launching the new core. Different Agents can run independently.

CLI chat and observation connect to the local core and start it when necessary. Local Web chat uses the same core, including when the public HTTP API is disabled. Browsing history, memory, configuration and tasks offline does not construct an Agent or a model client. Passive Web notifications do not start it.

The private control socket is under `/tmp/xagent-<uid>/`, with a short name derived from the canonical data directory. Its directory is `0700`, and its socket is `0600`. It is separate from the optional public HTTP/WS interface. The public API does not add authentication; keep its existing network boundary.

## Admission and cancellation

Inputs retain the channel, user, room, source event ID, request ID and turn ID. Input history and acceptance are persisted before an `accepted` event is returned. Stable source IDs deduplicate within their channel, user and room. Explicit task retries use a new attempt ID.

One formal Agent turn runs at a time. Up to 32 turns may wait; queue and execution deadlines are 30 and 600 seconds. Observations are stored without immediately requesting a reply. Group messages already perceived through attention are not stored again when the Agent responds.

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

Unresolved recurring occurrences block subsequent execution. Retry creates a new attempt and preserves the previous receipt. Model errors are failures; success requires a final response or a valid attachment. API delivery succeeds only after session history is persisted. WebSocket events notify subscribers. Feishu and Weixin retain their send acknowledgements; uncertain external sending requires review and is not promised to execute exactly once.

## Diary recovery

First-person Markdown remains the durable long-term memory. Each maintenance window generates all fragments before preparing its commit receipt, including source cursors, dates, original checksums and replacement bytes. It atomically replaces and syncs diary files before advancing the existing cursor.

Recovery completes a prepared write once, or advances the cursor if the file already contains that write. A checksum conflict stops that window for review without overwriting manual edits. Relationship cards and notes are derived views; their failure does not cause the committed diary to be appended again. Lock waits and file commits retain their locks until cancelled workers have actually stopped.

## Shutdown and service management

The core stops accepting work and drains within a 30-second budget. Unfinished work retains recovery evidence. An unresponsive writer keeps ownership until it stops or the process is terminated; another core cannot overlap it. Configure the macOS/Linux service manager to allow at least 35 seconds before forced termination, and use it for startup and crash restart. Cancellation is cooperative; blocking external tools ultimately require the process boundary. The runtime targets macOS and Linux, including Raspberry Pi and Linux servers.

Settings and identity edits apply on restart. Web displays that pending state. Live mutations are routed through the owner; offline mutations acquire the same exclusive lock. Runtime status exposes connections, active turn, queue, task review count, diary backlog and recent maintenance. Logs include request, turn, task, run and attempt identifiers.

## Accelerated validation

```sh
python -m unittest discover -s tests
python scripts/runtime_soak.py --root /tmp/xagent-validation-fresh-directory
cd frontend && npm run build
```

The simulation advances the scheduler through 24 virtual hours, dispatches an hourly recurring task, submits four-channel traffic and duplicates, forces diary maintenance, simulates reconnects, and restarts the core twice. It checks persisted message counts, unique diary inputs, prepared task acknowledgements, single-turn execution and bounded file-handle use. The default has a 115-second total budget. It contacts no external model or bot. Separate fault regressions cover execution/delivery/archive interruption, diary conflicts and cursor failures, queue cancellation and cross-process ownership.

This is accelerated scenario validation; it does not measure actual 24-hour uptime or real device/network behaviour.
