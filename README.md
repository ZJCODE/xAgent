# xAgent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a local-first AI agent with its own identity, memory, and life. Use it from the terminal, Web UI, voice, Feishu, or Weixin, and manage everything from one interactive launcher.

## Quick Start

```bash
# Install
curl -fsSL https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh | bash

# Launch
xagent
```

Or install with pip:

```bash
pip install --upgrade myxagent
```

The launcher will guide you through the rest.

## Local Data

All agent data is stored locally under:

```text
~/.xagent/agents/
```

Each agent has its own identity, memory, diary, workspace, skills, tasks, logs, and channel state.

## License

MIT. See [LICENSE](LICENSE).