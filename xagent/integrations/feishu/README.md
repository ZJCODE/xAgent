# Feishu (Lark) Integration

Connect xAgent to a Feishu bot over the official **WebSocket long-connection**
event channel. No public webhook, no reverse proxy, no internal-network
tunneling required — works from a laptop, a corporate intranet, or a server
that only has outbound HTTPS.

## Architecture

```
Feishu open platform
        │   (WebSocket long connection)
        ▼
FeishuChannel  ──► xagent.integrations.feishu.FeishuAdapter
                       │
                       ▼
                  xagent.core.Agent  (in-process, 127.0.0.1 only)
```

The adapter is intentionally thin. `lark_oapi.channel.FeishuChannel` from the
official SDK already provides:

- WebSocket transport with auto-reconnect
- Inbound message normalization (`content_text`, `mentioned_bot`, `chat_type`, …)
- Outbound text / markdown / card sending
- Streaming card replies (LLM-style token output)
- Mention policy and retries

`FeishuAdapter` only adds xAgent-specific routing.

## Install

```bash
pip install myxagent
```

Configure your Feishu bot

1. Create an agent: https://open.feishu.cn/page/launcher
2. Open your agent: https://open.feishu.cn/app
3. Add extra permissions:
    * im:message.group_msg (for group chats)
    * im:message.group_at_msg.include_bot:readonly (for group @mentions from users and bots)
    * contact:user.base:readonly (for user display names)
    * admin:app.info:readonly (for other bot or agent display names)
4. Copy your App ID and App Secret.


## Configure xAgent

```bash
# First time only (creates ~/.xagent/config.yaml + identity.md)
xagent init

# Add channels.feishu to ~/.xagent/config.yaml
xagent init feishu
```

This updates `~/.xagent/config.yaml`:

```yaml
channels:
  feishu:
    enabled: true
    app_id: cli_xxx
    app_secret: your_secret  # or ${LARK_APP_SECRET}
    log_level: info
    stream: false
    enable_memory: true
    group_history_count: 10
    show_sender_ids: true
```

`${ENV_VAR}` placeholders are expanded at load time — keep secrets out of git.

## Run

```bash
# foreground: stay attached and stream logs to this terminal
xagent run --channel feishu

# background: managed process with PID and log files
xagent start --channel feishu

# stop the managed Feishu process for this runtime dir
xagent stop --channel feishu

# inspect PID, log path, and running state
xagent status --channel feishu

# custom runtime dir:
xagent run --channel feishu --dir ~/.xagent
```

`xagent start --channel feishu` starts a detached process, writes its PID to
`~/.xagent/run/feishu.pid`, and appends logs to `~/.xagent/logs/feishu.log`.
Use `xagent run --channel feishu` when you want the bot attached to your
terminal so you can watch logs live.

## Routing rules (hardcoded — no knobs)

The adapter behaves like a real human teammate:

| Message | Routed to | Notes |
|---|---|---|
| Direct chat (`p2p`) | `agent.chat` | Always reply, sent as a fresh message (no quoting). |
| Group / topic, bot @mentioned | `agent.chat` | Pulls recent Feishu history first, then replies. Anchored to the source message via `reply_to`; never as a Feishu topic/thread reply. |
| Group / topic, not @mentioned | ignored | The bot does not listen or speak unless explicitly addressed. |
| Non-text content | ignored (Phase 1) | Image/file routing is on the roadmap. |

> **Permission check.** The bot can reply to group @mentions with
> `im:message.group_at_msg`; use
> `im:message.group_at_msg.include_bot:readonly` when other bots in the group
> may @ the current bot. To read recent group history after the @mention,
> the app also needs Feishu history/group message read permissions such as
> `im:message:readonly` plus `im:message.group_msg`, `admin:app.info:readonly`. If those permissions are
> missing, xAgent simply replies using the current @message.

Before a message reaches xAgent, the adapter resolves Feishu sender IDs with
the official `client.contact.v3.user.get(request)` API and passes the display
name into `agent.chat`. This keeps internal IDs such as `ou_xxx` inside the
Feishu layer instead of exposing them to prompts or memory keys. If the contact
lookup is unavailable, the adapter falls back to a display name already present
on the SDK event, then to a generic `Feishu User` label.

For messages sent by other Feishu apps or bot agents, the adapter resolves the
sender's `app_id` with `client.application.v6.application.get(request)`. Querying
other apps requires Feishu's application information permission; without it,
those senders fall back the same way as unresolved users.

For group/topic traffic, the adapter wraps recent messages plus the current
mention in a room-context block before calling `agent.chat`:

```text
[room context]
room_name: Project Room
room_id: oc_dd80df0e88ca4f803995f3b75f2c8833

Telos(ou_xxx) 2026-05-12 15:05: @Mono hey
you 2026-05-12 15:05: hey Telos
[/room context]
```

The block includes `room_id` and adds `room_name` when the Feishu group name is
available. Direct chats do not use room context. By default, speaker labels and
mention replacements include IDs from the receive event, for example
`Telos(ou_xxx)` or `@Tom(ou_xxx)`. If Feishu denies the
contact/app lookup but the message API still returned a sender ID, xAgent keeps
that signal as `Feishu User(ou_xxx)` or `Feishu Bot(cli_xxx)` instead of
collapsing multiple senders into the same anonymous label. Feishu mention
placeholders such as `@_user_1` are replaced from message mention metadata when
names are available.

The Feishu adapter always runs normal non-private turns. It does not expose
or forward xAgent's `private` flag, because bot chat memory should remain
predictable across direct and group conversations.

## Streaming replies

Set `channels.feishu.stream: true` in `~/.xagent/config.yaml`. The adapter uses
`FeishuChannel.stream(...)` with markdown — Feishu renders the answer as a
streaming card that updates token-by-token. Disabled automatically when the
agent is configured with `output_schema` (structured output requires
non-stream JSON).

## Python API

```python
import asyncio
from xagent.interfaces.base import BaseAgentRunner
from xagent.integrations.feishu import FeishuAdapter, FeishuAdapterConfig

runner = BaseAgentRunner(config_dir="~/.xagent")
cfg = FeishuAdapterConfig.from_dict(runner.config["channels"]["feishu"])
adapter = FeishuAdapter(agent=runner.agent, config=cfg)
asyncio.run(adapter.run())
```

## Operational notes

- xAgent runs **in-process** with the adapter. Nothing listens on a public
  port. Even when you keep the HTTP/Web channel (`xagent start --channel web`) running, it
  stays bound to `127.0.0.1` — the adapter never goes through HTTP.
- `run_command` is a built-in xAgent tool with shell-execution capability.
  Audit your `identity.md` and consider running the adapter in a container
  or under a restricted user when exposing the bot to a real Feishu tenant.
- For multi-bot or multi-tenant routing, instantiate `FeishuAdapter` multiple
  times in your own entrypoint (one channel per app credential).

## Roadmap

- Phase 2: forward image / file resources from Feishu into `agent.chat`.
- Phase 3: card-based interactive prompts and richer mention metadata.
- Channel-abstraction layer to support Slack / Discord / WeCom with the same
  routing core.
