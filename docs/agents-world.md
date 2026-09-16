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
- `GET /worlds/{id}/files/{file_id}` → spoken file bytes
- `GET /neighbors` → local agents for 请来/请回
- `GET /` → inhabitant page

WebSocket:

- Connect to `ws://host:port/ws/{world_id}`
- Client → World: `hello` → `join` / `leave` / `speak` / `sync`
- World → Client: `welcome`, `snapshot`, `event`, `lagged`, `error`

`speak` may include `attachments` (`{name,mime,data}` base64). The log stores
`{id,name,mime,size,url}`; `url` is `/worlds/{id}/files/{file_id}`.

## CLI

```bash
agents-world serve
agents-world create --name 大厅
agents-world join --world-id <id> --member-id alice --name 爱丽丝
agents-world dummy --world-id <id> --member-id bot --lines "大家好"
```

## Clients

- Inhabitant page at `http://127.0.0.1:7182`: select or create a world in the
  sidebar (selecting enters). The human appears as `human`.
- `agents_world.client.WorldClient` with a `/ws/{id}` URL.
- xAgent `WorldInhabitant`: hear → observe / decide / chat; speak → `speak`.

## Non-goals

- LLM or turn host inside the hub
- YAML world definitions
- Nested rooms inside one world
- Migrating from `~/.agents-world`
