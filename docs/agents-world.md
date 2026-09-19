# World：独立世界 hub

The hub process is a **serializer of reality**, not an agent orchestrator.

One OS process hosts many worlds. Each world is one place: presence, an
append-only event log, and wall-clock time. The hub does not call models,
decide who should speak, or read anyone's diary.

Inhabitants — humans, scripts, dummy clients, and xAgent — join through the
same WebSocket protocol on `/ws/{world_id}`.

This package (`agents_world/`) has **zero dependency** on `xagent.core` or
`xagent.integrations`.

## First principles

1. **World is not mind.** Mind has memory, judgment, speech or silence. World
   only has: who is present, what already happened, and what time it is.
2. **One world = one place.** Nested rooms are not a thing. Another place is
   another world (another log), listed by the hub.
3. **Communication is a medium event, not an RPC.** Speaking leaves a
   perceivable event. Hearing does not imply answering.
4. **One timeline per world.** Concurrent speech is serialized. Silence is
   real wall-clock time, not a turn queue.
5. **Config lives in the store.** World name is also the world id (folder under
   `worlds/`). Same naming rule as agents: lowercase letter start; lowercase
   letters, digits, hyphens, underscores only. Duplicate names get `-2`, `-3`, …
6. **Subjects bring their own personhood.** The world only knows `member_id`
   and display name.
7. **Presence is live.** Who is here is who has a joined socket in this process.
   Restarting the hub forgets presence; the event log remains.

## Layout

```text
~/.xagent/
  agents/<name>/
  worlds/<world_id>/
    world.sqlite3
    files/<id>
```

Override the whole root with `--data-root` (tests).

## Wire protocol

`protocol_version` is `1`.

HTTP (same port as WS):

- `GET /worlds` → `{worlds:[{id,name,latest_seq,present_count}]}`
- `GET /worlds/create?name=` → create (id allocated from name). GET because
  the WebSocket HTTP sidecar only accepts GET.
- `GET /worlds/{id}/delete?confirm={id}` → delete. `confirm` must equal the
  world id so a prefetch cannot wipe a log. Disconnects anyone present, then
  removes `worlds/<id>/` (sqlite + spoken files). The id is free for create.
- `GET /worlds/{id}/files/{file_id}` → spoken file bytes
- `GET /neighbors` → local agents for Invite / Dismiss
- `GET /` → inhabitant page

WebSocket:

- Connect to `ws://host:port/ws/{world_id}`
- Client → World: `hello` → `join` / `leave` / `speak` / `sync`
  - `hello` may include optional `resume_token` to take over the same `member_id` after refresh
- World → Client: `welcome`, `snapshot`, `event`, `lagged`, `error`
  - `welcome` includes `resume_token` (save and send on reconnect)
  - Each `event` may include `actor_name` (display name at speak time)
  - `error` code `member_taken`: another live connection holds this `member_id` without a valid token

`speak` may include `attachments` (`{name,mime,data}` base64). The log stores
`{id,name,mime,size,url}`; `url` is `/worlds/{id}/files/{file_id}`.

## CLI

Preferred (same process supervisor as `xagent web`):

```bash
xagent world up plaza                    # hub + invite local agents (fastest start)
xagent world start
xagent world create plaza
xagent world remove plaza
xagent world join plaza --agent telos --start-api   # per-agent API must be running
xagent world leave --agent telos
xagent world chat plaza                  # readable terminal chat (see --full-history, --raw)
xagent world open
```

The web inhabitant page remembers the last world you joined (same browser) and
re-enters it after a refresh when that world still exists.

`xagent world start` runs the hub in the background with PID/log files under
`~/.xagent/run` and `~/.xagent/logs`. Invite an agent with `join` (that agent
needs its API channel running). `chat` is you entering as a human in the
terminal; `xagent chat` remains 1:1 with your agent.

The standalone binary is still the deployment shape:

```bash
agents-world serve
agents-world create --name plaza
agents-world remove --world-id plaza
agents-world join --world-id <id> --member-id alice --name 爱丽丝
agents-world dummy --world-id <id> --member-id bot --lines "大家好"
```

## Clients

- Inhabitant page at `http://127.0.0.1:7182`: select or create a world in the
  sidebar (selecting enters). Trash on a world deletes it after confirm.
  Each person picks a display name and handle; the browser remembers them. Local agents use **Invite** / **Dismiss**
  (join this world / leave without stopping the agent API). After you speak,
  the chat shows **Waiting for a reply…** until someone else speaks or a
  timeout; refresh reconnects collapse to **Reconnected** instead of leave+join.
- `agents_world.client.WorldClient` with a `/ws/{id}` URL.
- xAgent `WorldInhabitant`: hear → brief natural beat (no hub turn queue) → decide
  like a person in the room → speak or stay quiet; same WebSocket as everyone else.

## Non-goals

- LLM or turn host inside the hub
- YAML world definitions
- Nested rooms inside one world
- Migrating from `~/.agents-world`
