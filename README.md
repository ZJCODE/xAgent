# xAgent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a local-first personal AI agent. Use it from the terminal, Web UI, voice, Feishu, or Weixin, and manage everything from one interactive launcher.

## Install

```bash
pip install myxagent
```

Python 3.10 or newer is required.

## Start

Run:

```bash
xagent
```

The launcher will guide you through the rest:

- **Setup**: configure your model, API key, identity, search, image generation, and voice.
- **Agents**: create or switch between different local agents.
- **Channel**: open Chat, Web, Voice, Feishu, or Weixin.
- **Inspect**: view config, memory, messages, and local state.
- **Help**: see the most common commands for the current agent.

For most users, `xagent` is the only command you need to remember.

## What xAgent Gives You

- A terminal chat for quick conversations.
- A Web UI for longer conversations, files, memory, tasks, skills, and workspace browsing.
- A voice channel for microphone and speaker interaction.
- Background channels for Web/API, voice, Feishu, and Weixin.
- Multiple named agents, each with its own identity, memory, workspace, tasks, skills, logs, and channel state.

## Useful Shortcuts

You can do everything from the launcher, but these commands are convenient once you know what you want:

```bash
xagent setup
xagent chat
xagent web
xagent voice start
xagent status
```

Channel logs and lifecycle commands follow the same pattern:

```bash
xagent api start
xagent api logs -f
xagent api stop

xagent voice status
xagent voice logs -f
xagent voice stop
```

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
