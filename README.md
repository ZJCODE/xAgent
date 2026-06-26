# xAgent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a local-first personal AI agent. Use it from the terminal, Web UI, voice, Feishu, or Weixin, and manage everything from one interactive launcher.

## Install

```bash
pip install myxagent
```

## Start

Run:

```bash
xagent
```

The launcher will guide you through the rest

For most users, `xagent` is the only command you need to remember.

## What xAgent Gives You

- A terminal chat for quick conversations.
- A Web UI for longer conversations, files, memory, tasks, skills, and workspace browsing.
- A voice channel for microphone and speaker interaction.
- Background channels for Web/API, voice, Feishu, and Weixin.
- Multiple named agents, each with its own identity, memory, workspace, tasks, skills, logs, and channel state.

## Local Data

xAgent stores your agents locally under:

```text
~/.xagent/agents/
```

Each agent has its own config, identity, memory, messages, workspace, skills, tasks, logs, and channel process files.

## Contributing

xAgent is open source and in active development. Issues, bug reports, and pull requests are welcome.

- Repository: https://github.com/ZJCODE/xagent
- Issues: https://github.com/ZJCODE/xagent/issues

For local development:

```bash
python -m pip install -e .
python -m pytest
```

## License

xAgent is released under the MIT License. See [LICENSE](LICENSE).
