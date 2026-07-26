# Feishu channel

The Feishu adapter only receives and sends transport messages. It submits every
accepted message to the single Agent Runtime and never constructs an Agent,
writes memory, or runs its own scheduler.

## Install and configure

```bash
pip install "myxagent[feishu]"
xagent channel setup feishu
```

The setup flow asks for the Feishu app ID and secret. The app needs the event
and message permissions required by the messages you intend to receive. Use a
long connection in the Feishu developer console.

Start and inspect the channel:

```bash
xagent channel start feishu
xagent channel list
```

Stop or restart it without stopping the Agent:

```bash
xagent channel stop feishu
xagent channel restart feishu
```

## Runtime behavior

- Direct and group messages retain their Feishu conversation and sender IDs.
- A stable channel account is linked to a `person_id` in SQLite. Cross-channel
  identity is never guessed; use `xagent person link` when needed.
- Group participation is decided by the Agent, but current speaker and audience
  remain explicit in every event.
- Media download errors degrade only Feishu. Other channels and the cognitive
  loop continue running.
- If the adapter exits, its supervisor marks it `degraded` and reconnects with
  bounded exponential backoff.
- Deliveries created while Feishu is explicitly disabled stay `blocked`; merely
  starting the channel does not send them.

Relevant configuration:

```yaml
schema_version: 2
channels:
  feishu:
    enabled: false
    app_id: cli_xxx
    app_secret: secret
    stream: false
    group_fetch_limit: 10
    group_reply_only_when_mentioned: false
```

Keep `config.yaml` private because it contains credentials.
