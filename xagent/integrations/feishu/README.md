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
- Mention policy, dedup, retries

`FeishuAdapter` only adds xAgent-specific routing.

## Install

```bash
pip install 'myxagent[feishu]'
# or, in an existing env:
pip install lark-oapi
```

## Configure your Feishu bot once

In the [Feishu Open Platform](https://open.feishu.cn/) developer console:

1. Create an app → enable **Bot** capability.
2. **Event Subscription** → choose **WebSocket** (not webhook).
3. Subscribe to event `im.message.receive_v1`.
4. Grant permissions:
   - `im:message`
   - `im:message.p2p_msg`
   - `im:message.group_at_msg`  (and `im:message.group_msg` if you plan to
     listen to non-mentioned group messages)
   - `im:message:send_as_bot`
5. Re-publish / re-install the app.
6. Copy `App ID` and `App Secret`.

## Configure xAgent

```bash
# First time only (creates ~/.xagent/config.yaml + identity.md)
xagent init

# Create feishu.yaml under ~/.xagent/
xagent feishu init
```

This writes `~/.xagent/feishu.yaml`:

```yaml
app_id: cli_xxx
app_secret: your_secret  # or ${LARK_APP_SECRET}

# Optional:
# log_level: info
# stream: false
# enable_memory: true
```

`${ENV_VAR}` placeholders are expanded at load time — keep secrets out of git.

## Run

```bash
xagent feishu run
# or with a custom runtime dir / config path:
xagent feishu run --dir ~/.xagent --config ~/.xagent/feishu.yaml --verbose
```

That's it. The bot is now live on Feishu.

## Routing rules (hardcoded — no knobs)

The adapter behaves like a real human teammate:

| Message | Routed to | Notes |
|---|---|---|
| Direct chat (`p2p`) | `agent.chat` | Always reply, sent as a fresh message (no quoting). |
| Group / topic, bot @mentioned | `agent.chat` | Always reply. Anchored to the source message via `reply_to`; never as a Feishu topic/thread reply. |
| Group / topic, not @mentioned | `agent.observe` | The agent decides whether to speak. Replies are sent as fresh group messages. |
| Non-text content | ignored (Phase 1) | Image/file routing is on the roadmap. |

> **Permission check.** For the bot to see un-addressed group messages
> (the `observe` path), the Feishu app needs `im:message.group_msg`
> in addition to `im:message.group_at_msg`. Without it the SDK will never
> receive them, regardless of any client-side setting.

User identity is the Feishu **`open_id`** (`msg.sender_id`). xAgent's memory
layer is keyed by this stable id, so long-term memory survives across
sessions automatically.

The Feishu adapter always runs normal non-private turns. It does not expose
or forward xAgent's `private` flag, because bot chat memory should remain
predictable across direct and group conversations.

## Streaming replies

Set `stream: true` in `feishu.yaml`. The adapter uses
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
cfg = FeishuAdapterConfig.from_file("~/.xagent/feishu.yaml")
adapter = FeishuAdapter(agent=runner.agent, config=cfg)
asyncio.run(adapter.run())
```

## Operational notes

- xAgent runs **in-process** with the adapter. Nothing listens on a public
  port. Even when you keep the HTTP server (`xagent server`) running, it
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
