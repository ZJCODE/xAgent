# xAgent

xAgent runs each Agent as one long-lived individual: one Runtime, one strictly
ordered event stream, one memory timeline, and multiple hot-swappable channels.

## Design

- API, Feishu, Weixin, and voice are transport adapters only.
- Web, CLI, and the scheduler are event sources, not channels. A task belongs
  to the Agent and has an optional explicit delivery destination.
- The Web client uses the Runtime's authenticated loopback API. The separately
  managed Public API channel is for external clients and can be disabled
  without breaking Web chat.
- Every input is persisted in SQLite before one FIFO cognitive loop handles it.
- A channel failure is isolated; the Runtime and other channels continue.
- Stopping a channel persists that choice. Its outbound deliveries become
  `blocked` and are sent only after an explicit retry.
- SQLite stores events, deliveries, tasks, people, account links, channel
  transport state, and journal cursors. Markdown diary files are the only
  long-term memory facts.
- Shell is a built-in capability by default. Code limits it to bounded,
  read-only commands inside the Agent workspace; it can still be disabled in
  Settings.

## Requirements

- Python 3.11 or newer
- A supported model provider and API key

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh | bash
```

Or:

```bash
pip install myxagent
```

Install channel features only when needed:

```bash
pip install "myxagent[feishu]"
pip install "myxagent[weixin]"
pip install "myxagent[voice]"
pip install "myxagent[image]"
pip install "myxagent[search]"
pip install "myxagent[all]"
```

## Start on a desktop

```bash
xagent setup
xagent web
```

`xagent web` opens the loopback-only desktop management center at
`http://127.0.0.1:1415`. It manages Agents, Runtime lifecycle, chat, the durable
message timeline, Markdown memory, Agent tasks with optional delivery, channels, delivery
issues, and the complete schema-v2 configuration. Secrets are masked in the
browser. It runs in the foreground; Ctrl+C closes only the Web surface and
leaves every Agent Runtime unchanged.

Use `xagent web --no-open` to start without opening a browser, `--port PORT` to
choose another local port, or `--agent NAME` to select the initial Agent.

## Start on a headless host

```bash
xagent setup
xagent launcher
```

Running `xagent` with no command is the same as `xagent launcher`. This
keyboard-driven terminal surface is intended for SSH and systems without a
desktop. It is only a navigation layer: lifecycle and channel actions delegate
to the same public commands used for scripting.

Direct commands remain available:

```bash
xagent start
xagent status
xagent chat
```

Channel control is independent of Runtime control:

```bash
xagent channel list
xagent channel setup feishu
xagent channel start feishu
xagent channel stop feishu
xagent channel restart feishu
```

`xagent stop` stops the whole Runtime without changing the persisted channel
choices. `xagent channel start NAME` starts the Runtime first if necessary.
The Runtime remains alive when every channel is disabled.

Inspect and retry blocked outbound work explicitly:

```bash
xagent delivery list --status blocked
xagent delivery retry DELIVERY_ID
```

Link accounts only when a human confirms they belong to the same person:

```bash
xagent person list
xagent person link PERSON_ID feishu ACCOUNT_ID
```

## Data and version boundary

Managed Agent data lives under `~/.xagent/agents/`. Each Agent directory
contains:

```text
config.yaml          schema_version: 2
identity.md
state.sqlite3
memory/
workspace/
skills/
run/runtime.json     loopback control endpoint and token, mode 0600
```

Version 1.0 accepts only the current configuration and database structure. It
does not import, transform, infer, or repair data from another structure. An
unknown schema stops startup without modifying the files.

## Development

```bash
uv run --frozen pytest
cd frontend && pnpm build
```

## License

[MIT](LICENSE)
