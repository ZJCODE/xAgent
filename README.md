# xAgent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a local-first AI agent platform that gives each agent its own identity, memory, workspace, and ongoing life.

Interact with your agents through the terminal, Web UI, voice, Feishu, or Weixin, and manage them from a unified interactive launcher.

## Features

- **Local-first** — Agent data is stored on your own machine.
- **Independent agents** — Each agent has its own identity, memory, diary, workspace, skills, tasks, logs, and channel state.
- **Multiple interfaces** — Use xAgent from the terminal, Web UI, voice, Feishu, or Weixin.
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

## Getting Started

After installation, launch xAgent with:

```bash
xagent
```

The interactive launcher will guide you through creating and managing your agents.

Each Agent runs one persistent core. The public API starts by default; Feishu,
Weixin and Voice connect only when requested:

```bash
xagent run --agent atlas       # foreground
xagent start --agent atlas     # background
xagent channel feishu connect --agent atlas
xagent channel feishu disconnect --agent atlas
xagent status --agent atlas
xagent logs --agent atlas --follow
xagent restart --agent atlas
xagent stop --agent atlas
```

In the terminal launcher, choose **Runtime → Start**, then open **Channel** to
connect Feishu, Weixin, or Voice. In the Web client, manage connections from
**Channels**. Restart the Agent after changing its configuration or identity.

To add a hardware or software device, open **Agent → Devices** and follow the
pairing steps. No configuration edits or restart are needed.

Learn more: [Managing channels and restarts](docs/runtime-core.md) ·
[Connecting devices](docs/device-execution.md).

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
