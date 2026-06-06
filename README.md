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

Follow the prompts to choose your provider, model, API key, optional tools, local voice, and identity. xAgent selects one model API protocol from the provider: official OpenAI uses OpenAI Responses; DeepSeek and Qwen use OpenAI-compatible Chat Completions; MiniMax and Anthropic use Anthropic Messages. Search is always an explicit init choice and supports `none`, OpenAI, Qwen, and MiniMax; matching search providers reuse the main API key. For a custom provider, `xagent init` asks which `model_api` to use before asking for the base URL. A clear identity helps the agent respond in the role and style you expect.

OpenAI runtimes default to OpenAI built-in web search and recommend OpenAI image generation during init, while Qwen runtimes default to DashScope/Qwen built-in web search and Qwen image generation. Other providers can choose OpenAI search, Qwen search, or no search during init. OpenAI and Qwen native search reuse the main API key when the main provider matches; cross-provider OpenAI or Qwen search must set the matching key in `search.api_key`.

Image input is supported by OpenAI and Qwen by default, with the known provider list kept in `VISION_CAPABLE_PROVIDERS`. Other built-in providers reject image input clearly instead of sending unsupported image payloads to the model; the Web UI keeps generic file upload available but only sends image bytes into the model when the active provider supports vision. Any provider can explicitly override vision support with `provider.supports_vision: true` or `false` when its selected model differs from the provider default. Image generation is a separate optional tool: OpenAI and MiniMax providers recommend their native image generation provider during init, but can choose `none`; other providers default to `none` and do not load cross-provider image generation. Web/API/Feishu inbound files are saved as workspace attachments under `workspace/temp/attachments/...`, images remain previewable through `workspace/temp/images/...`, and both use `/api/workspace/blob?path=...` links. Generated images and artifact files are returned as structured attachments: Web previews images or shows downloadable file chips, CLI prints local file paths, and Feishu sends native image/file attachments. Feishu compresses large images at the channel boundary before model input or native upload.

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

## Use From Voice

Enable local voice during first-time setup, then start a foreground voice session:

```bash
xagent init
xagent voice
xagent voice --user-id local_voice --no-memory
xagent voice --list-devices
xagent voice --input-device "MacBook Pro麦克风" --output-device "MacBook Pro扬声器"
```

Voice mode supports Soniox and Qwen in this version. `xagent init` writes the selected voice provider plus separate `stt.api_key` and `tts.api_key` entries into `config.yaml`; selecting `custom` lets STT and TTS use different Soniox/Qwen providers and API keys. No separate voice extra or environment variable setup is required. `auto` keeps the built-in best-device selection. To pin local devices, set `channels.voice.audio.input` and `channels.voice.audio.output` to a device name, `#index`, or `auto`; CLI `--input-device` and `--output-device` override the config for one run. For shared rooms, enable `channels.voice.wake.enabled` and set `wake.wake_phrases` so background speech is ignored until a configured wake phrase is heard. It streams microphone audio to the selected realtime STT service, uses provider-side endpoint detection to decide when a user turn ends, calls the existing xAgent text runtime, and streams the assistant reply through the selected realtime TTS service. It is not part of `xagent service start all` yet.

## Use From The Web Page

Start xAgent and open the web page in your browser:

```bash
xagent web
```

The web page is best for longer conversations, segmented replies, and file or image attachments.
Use the Transport selector when you need to compare final-only HTTP with WebSocket event delivery. WebSocket is an API transport, not a separate channel.

Run it as a managed background service instead:

```bash
xagent service start api
xagent service status
xagent service logs api
xagent service stop api
```

Use `api` for HTTP JSON, WebSocket, and the built-in web page. Use `feishu` for the Feishu bot, and `all` when you want every enabled managed channel. The local `voice` command is foreground-only and is not managed by `service` in this version. Without a channel, `service start` chooses one enabled channel, preferring `api`; other service actions default to `all`.

## Scheduled Tasks

In long-running channels, xAgent can schedule future work directly from conversation. For example, if you say `一分钟后提醒我走两步` in the Web UI or Feishu, the agent writes a `message` task under `~/.xagent/tasks/` and the active channel runtime delivers the reminder automatically. If you say `半小时后帮我查一下当前系统的温度然后发我`, the agent writes an `agent` task that runs when due, calls tools as needed, and sends the final result back to the same channel.

The Web UI includes a Tasks tab for viewing and deleting scheduled tasks. API/Web scheduled task results are also pushed to connected Web clients through `/ws/tasks`.

## Use From Feishu

Configure the Feishu channel after the base init:

```bash
# One-click: creates the Feishu app and writes the config (admin authorization required)
xagent init feishu
# Or paste an existing App ID/Secret instead:
xagent init feishu --manual
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

Memory is an asynchronous derivative of the message stream. Complete raw conversations and observations live in `messages/`; long-term memory is consolidated later by batch size, by a stale-message fallback, by runtime heartbeat in long-lived API/Feishu processes, or by explicit memory tools. Exiting a foreground command does not block on memory generation. Recent memory context is managed automatically and defaults to the last 3 days. Long-term memory is time-based and stored as daily, weekly, monthly, and yearly markdown files.

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
