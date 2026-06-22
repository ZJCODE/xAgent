# xAgent Architecture

xAgent uses a layered architecture with explicit dependency direction:

`domain` -> no internal dependencies except shared pure helpers.

`ports` -> depends only on `domain` and Python protocols.

`application` -> orchestrates agent turns, memory, tasks, tools, and delivery through ports.

`infrastructure` -> implements storage, LLM clients, media handling, voice providers, search, image generation, and observability.

`channels` -> adapts user-facing transports such as terminal, web, Feishu, Weixin, and voice.

`api` and `cli` -> control-plane entrypoints. They parse requests or commands and call bootstrapped application services.

`bootstrap` -> the only composition root. It reads configuration and wires concrete infrastructure into application services.

## Dependency Rules

- `domain` must not import `application`, `infrastructure`, `channels`, `api`, or `cli`.
- `application` must not import concrete provider SDK adapters directly.
- `channels`, `api`, and `cli` must obtain agent services through `bootstrap`.
- Built-in tools live under `tools/builtins`; tool execution depends on `tools.protocol`, `tools.registry`, and `tools.results`.
- Static web assets are packaged from `api/static`.
- Built-in skills are packaged from `tools/builtins/catalog`.

## Current Migration Note

The first architecture migration moved existing behavior into the new package layout. Some large modules still need internal extraction, but their public package ownership now matches the target architecture.
