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

Follow the prompts to choose your provider, model, API key, search provider, and optional identity. xAgent selects the SDK from the provider: OpenAI, DeepSeek, and Qwen use the OpenAI SDK; MiniMax and Anthropic use the Anthropic SDK. For a custom provider, `xagent init` asks which SDK to use before asking for the base URL. A clear identity helps the agent respond in the role and style you expect.

Search is optional. Any provider can use OpenAI built-in web search, DuckDuckGo, Brave Search, or no search. OpenAI built-in search reuses the main API key when the main provider is OpenAI; non-OpenAI providers must set an OpenAI key in `search.api_key`. Brave Search requires a Brave Search API key in `search.api_key` or `BRAVE_SEARCH_API_KEY`.

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
xagent run --channel api --open
```

The web page is best for longer conversations, streaming replies, and image input.
Use the Transport selector to compare regular HTTP/SSE with WebSocket chat delivery. WebSocket is an API transport, not a separate channel.

Run it as a managed background service instead:

```bash
xagent start --channel api
xagent status
xagent logs --channel api
xagent stop --channel api
```

Use `--channel api` for HTTP JSON, SSE, WebSocket, and the optional built-in web page. Use `--channel feishu` for the Feishu bot, and comma-separated channels such as `--channel api,feishu` when you want both.

## Use From Feishu

Configure the Feishu channel after the base init:

```bash
xagent init feishu
xagent start --channel feishu
```

`xagent start --channel all` starts every enabled channel in `config.yaml`.

## Chat And Observe

Use `chat` when someone is directly addressing the agent and expects a reply.

Use `observe` for context the agent notices or overhears: ambient speech, room state, notifications, reminders, or sensor updates. An observation is saved to the message stream for future context and memory, but it does not generate an immediate reply.

From the CLI:

```bash
xagent observe "Bob mentioned the demo may move to 3pm" --source feishu --event-type group_message
```

Long-term memory is built from the agent's experience stream, not only direct chats. Meaningful observations can be consolidated alongside conversations; preserve attribution in the observation text or metadata so overheard speech is not confused with a direct request from the current user.

Memory writes are buffered for efficiency, then flushed by batch size, by a stale-message fallback, runtime heartbeat in long-lived API/Feishu processes, and normal CLI/server shutdown. Recent memory context defaults to 7 days and can be changed in `memory.recent_days`. When memory entries contain quote-backed stable information about a person, xAgent can also append that evidence to `memory/people/` profiles.

## API Transports

`POST /chat` remains the default HTTP interface. Set `stream=true` to receive server-sent events.

`/ws/chat` accepts the same chat JSON over WebSocket and returns JSON frames: `delta`, `message`, `error`, and `done`.

`/ws/observe` accepts the same observe JSON over WebSocket and returns `result`, `error`, and `done` frames.

For external integrations, configuration details, and full HTTP/WebSocket payload examples, see [TECHNICAL.md](TECHNICAL.md).

## Best Practices

- Run `xagent init` before your first chat.
- Keep your API key in the generated local configuration.
- Use the CLI for quick tasks and the web page when you want more room to work.
- Give the agent a concise identity so it knows how it should help.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
