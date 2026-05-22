# xAgent

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent lets you run a personal AI agent from the terminal or from a built-in web page.

## Install

```bash
pip install myxagent
```

Run the first-time setup:

```bash
xagent init
```

Follow the prompts to choose your provider, model, API key, optional tools, and identity. xAgent selects one model API protocol from the provider: official OpenAI uses OpenAI Responses; DeepSeek and Qwen use OpenAI-compatible Chat Completions; MiniMax and Anthropic use Anthropic Messages. For a custom provider, `xagent init` asks which `model_api` to use before asking for the base URL. A clear identity helps the agent respond in the role and style you expect.

OpenAI runtimes default to OpenAI built-in web search and recommend OpenAI image generation during init, but image generation can be disabled. Other providers can choose DuckDuckGo, OpenAI search, Brave Search, or no search during init. OpenAI built-in search reuses the main API key when the main provider is OpenAI; non-OpenAI providers must set an OpenAI key in `search.api_key`. Brave Search requires a Brave Search API key in `search.api_key` or `BRAVE_SEARCH_API_KEY`.

Image input is supported by OpenAI and Qwen by default, with the known provider list kept in `VISION_CAPABLE_PROVIDERS`. Other built-in providers reject image input clearly instead of sending unsupported image payloads to the model; the Web UI hides image upload controls when the active provider lacks vision support. Any provider can explicitly override vision support with `provider.supports_vision: true` or `false` when its selected model differs from the provider default. Image generation is a separate optional tool: OpenAI and MiniMax providers recommend their native image generation provider during init, but can choose `none`; other providers default to `none` and do not load cross-provider image generation. Web/API/Feishu inbound images and generated files are saved under `workspace/temp/images` and rendered through the workspace blob endpoint. Feishu workspace blob images/files are uploaded back as native Feishu attachments.

Langfuse observability is included for teams that need LLM tracing, latency, usage, and error monitoring. It is disabled by default; `xagent init` can write an `observability` block only when you choose to enable it.

## Use From The CLI

Start an interactive chat:

```bash
xagent chat
```

Send one message and exit:

```bash
xagent chat "Help me plan today"
```

The CLI is best for quick questions, terminal work, and short back-and-forth sessions.

## Use From The Web Page

Start xAgent and open the web page in your browser:

```bash
xagent web
```

The web page is best for longer conversations, segmented replies, and image input.
Use the Transport selector when you need to compare final-only HTTP with WebSocket event delivery. WebSocket is an API transport, not a separate channel.

Run it as a managed background service instead:

```bash
xagent service start api
xagent service status
xagent service logs api
xagent service stop api
```

Use `api` for HTTP JSON, WebSocket, and the built-in web page. Use `feishu` for the Feishu bot, and `all` when you want every enabled channel. Without a channel, `service start` chooses one enabled channel, preferring `api`; other service actions default to `all`.

## Use From Feishu

Configure the Feishu channel after the base init:

```bash
xagent init feishu
xagent service start feishu
```

`xagent service start all` starts every enabled channel in `config.yaml`.

## Chat And Observe

Use `chat` when someone is directly addressing the agent and expects a reply.

Use `observe` for context the agent notices or overhears: ambient speech, room state, notifications, reminders, or sensor updates. An observation is saved to the message stream for future context and memory, but it does not generate an immediate reply.

From the CLI:

```bash
xagent observe "Bob mentioned the demo may move to 3pm" --source feishu --event-type group_message
```

Long-term memory is built from the agent's experience stream, not only direct chats. Meaningful observations can be consolidated alongside conversations; preserve attribution in the observation text or metadata so overheard speech is not confused with a direct request from the current user.

Memory writes are buffered for efficiency, then flushed by batch size, by a stale-message fallback, runtime heartbeat in long-lived API/Feishu processes, and normal CLI/server shutdown. Recent memory context is managed automatically and defaults to the last 3 days. Long-term memory is time-based and stored as daily, weekly, monthly, and yearly markdown files.

The runtime also creates a `workspace/` directory beside `memory/` and `messages/`. This is the agent's external working area for notes, project records, temporary files, scripts, images, and other artifacts. In standard xAgent runtimes, the built-in `run_command` tool defaults to this directory when no working directory is supplied. The web console includes a Workspace page for browsing, editing, searching, uploading, previewing, and deleting workspace files, plus a Maintenance action to clear workspace contents.

Agent Skills live in the sibling `skills/` directory. Each skill is a folder with a required `SKILL.md` file containing YAML frontmatter (`name` and `description`) followed by markdown instructions. Optional `references/`, `scripts/`, and `assets/` files can be bundled with the skill. xAgent uses progressive loading: enabled skill names and descriptions are always exposed to the model as Available Skills; when a description matches the task, the model calls the built-in `read_skill` loader to read `SKILL.md`; referenced files are read only when needed. Skill scripts are not auto-registered as tools; if a skill asks the agent to run a script, it goes through the existing `run_command` policy. The web console includes a Skills page for viewing, creating, searching, enabling, disabling, and deleting skills.

## API Transports

`POST /chat` remains the final-only HTTP interface and returns `{"reply": ...}`.

`/ws/chat` is the realtime event protocol. It returns JSON frames such as `message_start`, `message_delta`, `message_done`, `tool_call`, `tool_result`, `error`, and `done`; `stream` controls whether text deltas are emitted.

`/ws/observe` accepts the same observe JSON over WebSocket and returns `result`, `error`, and `done` frames.

For external integrations, configuration details, and full HTTP/WebSocket payload examples, see [TECHNICAL.md](TECHNICAL.md).

## Best Practices

- Run `xagent init` before your first chat.
- Keep your API key in the generated local configuration.
- Use the CLI for quick tasks and the web page when you want more room to work.
- Give the agent a concise identity so it knows how it should help.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
