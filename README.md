# xAgent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a local-first AI agent platform that gives each agent its own identity, memory, workspace, and ongoing life.

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
