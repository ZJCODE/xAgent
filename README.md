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

Feishu/Lark support needs an extra SDK that is large on slow links:

```bash
pip install 'myxagent[feishu]'
```

### Raspberry Pi

Raspberry Pi OS configures pip with [piwheels](https://www.piwheels.org/) as an extra index (`/etc/pip.conf`). That is why a normal `pip install myxagent` downloads every wheel from `www.piwheels.org`, prints a flood of old `pytz` / `regex` filename warnings, and crawls large files such as `lark-oapi` at tens of KB/s with dropped connections.

`pip install -i https://pypi.org/simple` is not enough: pip still reads `extra-index-url` from `/etc/pip.conf` and keeps using piwheels. Ignore that config, then use PyPI or a nearby mirror:

```bash
PIP_CONFIG_FILE=/dev/null pip install --isolated myxagent -i https://pypi.org/simple
# China:
PIP_CONFIG_FILE=/dev/null pip install --isolated myxagent -i https://pypi.tuna.tsinghua.edu.cn/simple
```

Or skip pip entirely and use the official installer above (uv talks only to PyPI). To also skip the Feishu SDK:

```bash
XAGENT_EXTRAS= curl -fsSL https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh | bash
```

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
