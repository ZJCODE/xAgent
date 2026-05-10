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

Follow the prompts to choose your provider, model, API key, search provider, and optional identity. A clear identity helps the agent respond in the role and style you expect.

Search is optional. OpenAI providers can use OpenAI built-in web search without an extra key, DuckDuckGo, Brave Search, or no search. Other providers can use DuckDuckGo, Brave Search, or no search. Brave Search requires a Brave Search API key in `search.api_key` or `BRAVE_SEARCH_API_KEY`.

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
xagent server --open
```

The web page is best for longer conversations, streaming replies, and image input.

## Best Practices

- Run `xagent init` before your first chat.
- Keep your API key in the generated local configuration.
- Use the CLI for quick tasks and the web page when you want more room to work.
- Give the agent a concise identity so it knows how it should help.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
