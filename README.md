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

In `reflect`, the agent still chooses whether to write a private diary note.
The existing recent-diary context provides the comparison material; the agent
returns `note: null` when there is no meaningful addition. Rewording an unchanged
task, elapsed time and repeated self-criticism are not new information. A new
insight, blocker, approach or completion state on the same topic can be recorded.
Notes and outward messages remain independent choices. This decision uses the
same reflection call, with no separate novelty model or additional note database;
the existing exact-repeat check remains a small runtime safeguard.

Autonomous turns share the normal tool catalog, workspace rules and turn lock.
Wake inputs, process text and control JSON are transient inputs/outputs,
like tool-process data. They are retained in turn receipts, not written as
user/assistant conversation messages. Only actual outward notifications enter
chat history. Legacy internal rows are hidden before history pagination and
excluded from search, working summaries, diary consolidation and people extraction.
Old working summaries are rebuilt with their coverage cursor reset; their original
file is retained with a `.legacy` suffix on replacement. Existing diary prose is
not automatically rewritten. Foreground and
scheduled requests interrupt them and wait for tool cleanup before taking over.
Each wake uses at most 12 model iterations (or the agent's lower configured
limit) and 180 seconds (or the agent's lower turn timeout), plus cleanup time.
Three identical tool batches and results stop the wake instead of looping.

A wake may finish quietly. Routine wake transcripts bypass automatic diary
writing; only a new explicit note is appended. Unchanged idle/blocked outcomes
back off up to two hours; user requests reset the backoff. Progress with a next
step can continue after 60 seconds, for at most three wakes before returning to
the normal interval. Wake results and recent tool events persist under
`.runtime/autonomous.json` in the agent workspace root; normal turn receipts
retain execution/interruption state. An interrupted operation is checked against
current state on the next wake rather than replayed automatically.

Notifications use explicit recipients and the existing delivery sink. Weixin
notifications enter its durable queue with a stable identity so recovery does
not rerun completed tools. Detailed runtime status includes an `autonomous`
snapshot with mode, activity, backoff and pending notification state.

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
