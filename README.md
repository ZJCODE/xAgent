# xAgent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a local-first AI agent platform that gives each agent its own identity, memory, workspace, and ongoing life.

An Agent remains the same individual across conversations with one or many people, observations, and private reflection. Its diary is one continuing experience stream, with clear attribution to people and settings; what it remembers and what it chooses to share with a particular audience are separate decisions.

Interact with your agents through the terminal, Web UI, voice, or Weixin, and manage them from a unified interactive launcher.

## Features

- **Local-first** — Agent data is stored on your own machine.
- **Independent agents** — Each agent has its own identity, memory, diary, workspace, skills, tasks, logs, and channel state.
- **Multiple interfaces** — Use xAgent from the terminal, Web UI, voice, or Weixin.
- **Unified management** — Create, configure, and manage agents from one interactive launcher.
- **External execution** — Connect hardware or software devices to the Agent API and call their declared capabilities through one generic tool.

## Requirements

- Python 3.10 or later

## Installation

### Install with the official script

```bash
curl -fsSL https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh | bash
```

### Install with pip

```bash
pip install myxagent
```

Voice uses the browser’s microphone and speaker or a paired WebSocket voice device. Configure shared voice settings in Agent, then open Voice; no separate Host voice startup is needed. The Agent server does not capture local audio.

Browser voice includes Opus support by default. A startup codec self-test selects Opus for both directions, with a fixed PCM profile available when native browser codecs are unsupported. Current browsers with AudioWorklet support and audio protocol 2 are required.

## Getting Started

After installation, launch xAgent with:

```bash
xagent
```

The interactive launcher will guide you through creating and managing your agents.

## Updating

Update xAgent without changing how it was installed:

```bash
xagent update
```

## Local Data

All xAgent data is stored locally in:

```text
~/.xagent/
```

Agent-specific data is stored under:

```text
~/.xagent/agents/
```

## Autonomous idle work

An agent can use its ordinary tool loop during idle wakes to advance existing
user-delegated work. Configure this in the agent's `config.yaml`:

```yaml
agent:
  subconscious_mode: act
  subconscious_interval_seconds: 1800
```

Modes are `act` (tool-enabled autonomous work), `reflect` (the existing private
reflection protocol), and `off`. An interval of `0` also disables wakes. New
agent configurations use `act`; existing configurations without a mode keep
`reflect`. Restart the agent after changing its mode.

Both modes share the same reflection task, recent conversation and working
summary, recent diary and longer-range summaries, notebook recall, relationship
context, and named available recipients. Both return the same final protocol:

```json
{"note": null, "message": null}
```

The agent chooses independently whether to save a private note or send an
outward message. A new insight is valid even without tool calls; silence is also
valid. Rewording an unchanged task, elapsed time and repeated self-criticism are
not new information. This uses one reflection decision with no separate novelty
model or note database; the existing exact-repeat check is a small safeguard.
Meaningful actions and remaining work can be recorded in `note` using the same
diary path, so later reflections read progress from the diary.

`act` adds the normal tool catalog, skill/workspace instructions and execution
loop. Its reflection purpose and note/message choices are the same as `reflect`;
it has no separate outcome/summary/next-step protocol, automatic diary summary,
continuation chain or idle-backoff policy. Both use the configured reflection
interval. Tool-enabled turns share the normal turn lock: foreground and scheduled
requests interrupt them and wait for tool cleanup before taking over. A turn has
at most 12 model iterations (or the agent's lower limit) and 180 seconds (or the
agent's lower timeout), plus cleanup. Repeated identical tool calls and results
are bounded.

Wake inputs, process text and control JSON are transient, like tool-process data.
They remain in turn receipts, not user/assistant chat history. Only actual outward
notifications enter the conversation. Legacy internal rows are excluded before
history pagination and from search, working summaries, diary consolidation and
people extraction. Old working summaries rebuild with coverage reset and the
original file retained as `.legacy`; existing diary prose is not automatically
rewritten.

There is no `previous_wake` model input or separate work-memory snapshot.
`.runtime/autonomous.json` retains only scheduling, notification and operational
status. Execution/interruption receipts remain for diagnostics; check current
state before repeating a potentially interrupted operation. Weixin notifications
use its existing durable queue with stable identities. Detailed runtime status
includes an `autonomous` snapshot.

## Uninstallation

If xAgent was installed with the official installation script:

```bash
uv tool uninstall myxagent
```

If xAgent was installed with pip:

```bash
pip uninstall myxagent
```

Uninstalling the CLI does not delete data stored under:

```text
~/.xagent/
```

Remove that directory manually only when you no longer need your local agent data.

## License

xAgent is released under the [MIT License](LICENSE).
