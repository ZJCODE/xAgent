# Agents Env：独立世界进程

Env is a **serializer of reality**, not an agent orchestrator.

It holds rooms, presence, an append-only event log, and wall-clock time.
It does not call models, decide who should speak, or read anyone's diary.
Inhabitants — humans, scripts, dummy clients, and later xAgent — join through
the same protocol.

This package (`agents_env/`) has **zero dependency** on `xagent.core` or
`xagent.integrations`.

## First principles

1. **World is not mind.** Mind has memory, judgment, speech or silence. World
   only has: what places exist, who is present, what already happened, and what
   time it is.
2. **Communication is a medium event, not an RPC.** Speaking leaves a
   perceivable event in a shared medium. Hearing does not imply answering.
3. **One timeline.** Events in a place are totally ordered. Concurrent speech
   is serialized by the world process. Silence is real wall-clock time, not a
   turn queue.
4. **The world outlives any subject.** The room and clock keep going when no
   one is thinking. Env is a long-lived process with a durable log.
5. **Audience is physics.** A public room reaches everyone present. A 1:1 is
   another room with two people. No separate ACL layer.
6. **Subjects bring their own personhood.** The world only knows a stable
   `member_id` and display name. Model, prompt, diary, and tools belong to the
   client.

## World physics

- One World process owns one append-only event log. The log is venue truth;
  diaries are not.
- Space is a Room. Default: one large group room. 1:1 later is another Room
  with the same physics.
- `join` / `leave` change Presence. Only present members receive live events.
  On join, the client may receive that room's existing public history (digital
  group realism, not acoustic decay).
- Actions: `join`, `leave`, `speak`. Perception: pushed or synced `event`.
- A successful speak is acknowledged by the utterance appearing in the log
  (self-echo). Clients treat the log as ground truth.
- Scene events (`clock`, ambience, join/leave announcements) are emitted by
  the world with `actor_id=world`. They are observations, not requests.
- Mentions (`@`) are structured metadata on an utterance. The world never
  forces a reply.
- Forbidden inside the world: LLM calls, speech scheduling, reading agent
  memory.
- Smart NPCs are other clients. The world may only emit mute props as `scene`
  events (plaque, chime, light).

## Objects

| Object | Fields |
|--------|--------|
| World | `id` |
| Room | `id`, `name`, `setting` |
| Member | `id`, `display_name` (surface only; no persona fields) |
| Presence | `(member_id, room_id)`, present?, live connection |
| Event | `seq`, `ts`, `room_id`, `kind`, `actor_id`, `text`, `mentions` |

`kind` is only: `utterance` | `join` | `leave` | `scene`.

## Wire protocol (JSON over WebSocket)

Client → World:

- `hello {member_id, display_name}`
- `join {room_id}`
- `leave {room_id}`
- `speak {room_id, text, mentions?}`
- `sync {room_id, after_seq}`

World → Client:

- `welcome {world_id, rooms}`
- `event` (including own utterances as ack)
- `snapshot` (on join: setting, present members, recent log)
- `error {message}`

Same `member_id` reconnects as the same body. A newer connection replaces the
older one so one body occupies one place.

## Storage and process

- CLI: `agents-env serve` starts the world on localhost WebSocket.
- Data: `~/.agents-env/worlds/<world_id>/world.sqlite3`
- Never write into `~/.xagent/agents/<name>/`.
- Scene YAML describes only the stage (rooms, setting text, timed ambience).
  No persona, model, or system prompt.

Example scene:

```yaml
world: plaza
rooms:
  - id: hall
    name: 大厅
    setting: 傍晚的开放大厅，谁都可以进来说话
scenes:
  - at: "+5m"
    room: hall
    text: 天色暗下来了
```

## Clients

Phase one ships:

- **Human CLI** (`agents-env join`) — humans are first-class inhabitants.
- **Dummy client** (`agents-env dummy`) — scripted presence for proving the venue.

An xAgent adapter is a later client that only depends on this protocol:
hear → own `observe` / `chat`; speak → `speak`; never treat the room log as diary.

## Non-goals

- LLM or turn host inside env
- Routing agent↔agent through Feishu / Weixin
- Merging multiple agent runtimes or SQLite stores
- Turning ChannelPage / AgentSwitcher into a group UI
- Persona or model config in scene files
- Storing env data under an agent directory

## GOAL.md check

- **Identity** — world does not shape persona; only `member_id`
- **Multi-user** — every event has `actor_id`; no blended speakers
- **1:1 and group** — same physics, different rooms; group first
- **Memory / journal** — world log ≠ diary
- **Unified memory** — world does not partition memory per user
- **Sharing** — world never reads diaries; only spoken text enters the medium
- **Attribution / continuity** — `seq` + `ts` + `actor_id` + `room_id` persist
- **Environment-aware** — `scene` is observation, not a request to anyone
