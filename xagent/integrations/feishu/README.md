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
- Outbound image / file upload and sending
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
    * im:resource:readonly (for downloading images and files sent to the bot)
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
    app_id: cli_xxx
    app_secret: your_secret  # or ${LARK_APP_SECRET}
    stream: false
    enable_memory: true
    group_history_count: 10
```

`${ENV_VAR}` placeholders are expanded at load time — keep secrets out of git.

## Run

```bash
# background: managed process with PID and log files
xagent service start feishu

# stop the managed Feishu process for this runtime dir
xagent service stop feishu

# inspect PID, log path, and running state
xagent service status feishu

# follow logs
xagent service logs feishu -f

# custom runtime dir:
xagent service start feishu --dir ~/.xagent
```

`xagent service start feishu` starts a detached process, writes its PID to
`~/.xagent/run/feishu.pid`, and appends logs to `~/.xagent/logs/feishu.log`.
Use `xagent service logs feishu -f` when you want to watch logs live.

## Routing rules (hardcoded — no knobs)

The adapter behaves like a real human teammate:

| Message | Routed to | Notes |
|---|---|---|
| Direct chat (`p2p`) | `agent.chat` | Always reply, sent as a fresh message (no quoting). |
| Group / topic, bot @mentioned | `agent.chat` | Pulls recent Feishu history first, then replies. Anchored to the source message via `reply_to`; never as a Feishu topic/thread reply. |
| Group / topic, not @mentioned | ignored | The bot does not listen or speak unless explicitly addressed. |
| Image content | `agent.chat` | Feishu image resources are downloaded into workspace attachments. Providers with vision also receive current-turn image input; providers without vision still get workspace file references for file-level tools. |
| File content | `agent.chat` | Feishu file resources are downloaded into `workspace/temp/attachments/feishu` and passed as workspace attachments. The model sees a file manifest and workspace blob link, not raw file bytes. |

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

The Feishu adapter always uses the standard chat flow so memory behavior
remains predictable across direct and group conversations.

## Images and files

Incoming Feishu image and file messages are downloaded through the official
message resource API. Images are saved under `workspace/temp/images/feishu`; other
files are saved under `workspace/temp/attachments/feishu`. Large images are
compressed at the Feishu boundary before they are saved or sent to the model:
EXIF orientation is applied, metadata is stripped, aspect ratio is preserved, the
longest edge is capped at 2048px by default, and the target payload is kept under
8MB when possible. Both images and files become workspace-backed attachments with
a stable `/api/workspace/blob?path=...` URL.

Images and files are recorded as workspace attachment metadata, so the Web UI
Messages page can render image previews or download entries without depending on
Markdown image rendering. At the model boundary the adapter passes
provider-ready image data as `image_source` only for current-turn images;
non-image files remain references, not raw bytes.
OpenAI and Qwen support vision by default; custom providers can opt in with
`provider.supports_vision: true`. Providers without vision support do not
receive image bytes as model image input, but image messages still route to chat
with workspace-backed attachment references. This lets the agent use file-level
tools for operations such as rotate, compress, convert, or reattach without
claiming to understand the image contents. Plain file attachments route the same
way because they do not require vision support.

When xAgent returns structured workspace attachments, the adapter resolves each
file under `workspace/`, writes a cached compressed derivative under
`workspace/temp/images/feishu/outbound` when an image is too large for reliable
transport, uploads that derivative through `FeishuChannel.send`, and sends it
back as a native Feishu image. Other workspace files are sent back as Feishu
files. Any surrounding text is sent first, then attachments are sent as separate
messages with deterministic UUID suffixes.

## Segmented replies

Feishu replies are always driven by `Agent.chat_events()`. Every completed
assistant segment (`message_done`) is sent as its own markdown message, so a
preface such as “我去看看” can appear before tool execution and the final answer
can arrive as a later message.

Set `channels.feishu.stream: true` to use `FeishuChannel.stream(...)` for
the current segment. This only controls whether text deltas update the active
Feishu card; segmented message boundaries are always enabled.

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
  port. Even when you keep the API channel (`xagent service start api`) running, it
  stays bound to `127.0.0.1` — the adapter never goes through HTTP.
- `run_command` is a built-in xAgent tool with shell-execution capability.
  Audit your `identity.md` and consider running the adapter in a container
  or under a restricted user when exposing the bot to a real Feishu tenant.
- For multi-bot or multi-tenant routing, instantiate `FeishuAdapter` multiple
  times in your own entrypoint (one channel per app credential).

## Roadmap

- Phase 3: card-based interactive prompts and richer mention metadata.
- Channel-abstraction layer to support Slack / Discord / WeCom with the same
  routing core.
