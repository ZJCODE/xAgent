# Weixin channel

The Weixin adapter connects the single Agent Runtime to Tencent's iLink Bot
API. It is a transport boundary, not a second Agent process.

## Install and configure

```bash
pip install "myxagent[weixin]"
xagent channel setup weixin
xagent channel start weixin
```

Setup displays a QR code and stores the resulting account credentials in the
Agent's mode-`0600` SQLite database.

## Behavior

- Direct messages only; group messages are ignored.
- Owner-only by default, with optional explicit `allow_users`.
- Inbound media is downloaded before the event is submitted.
- Outbound proactive or scheduled messages need a cached iLink
  `context_token`.
- Cursor, credential, context-token, conversation, and delivery state all live
  in the Runtime SQLite database.
- A connection, polling, media, or send failure affects only Weixin.
- Stopping Weixin causes new outbound deliveries for it to become `blocked`.
  Starting it again never sends them automatically.

Control it independently:

```bash
xagent channel list
xagent channel stop weixin
xagent channel restart weixin
xagent delivery list --status blocked
xagent delivery retry DELIVERY_ID
```

Relevant configuration:

```yaml
schema_version: 2
channels:
  weixin:
    enabled: false
    account_id: account@im.bot
    owner_user_id: owner@im.wechat
    owner_only: true
    allow_users: []
    media_enabled: true
```
