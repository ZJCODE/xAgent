# Weixin iLink Integration

Connect xAgent to WeChat direct messages through Tencent's iLink Bot API.
The integration talks to the iLink HTTP/JSON protocol directly and does not
import third-party Weixin bot packages.

## Scope

- Direct messages only. Group messages are ignored.
- QR login during setup.
- Owner-only by default: only the WeChat user who authorizes the QR login can
  trigger the agent, plus any configured `allow_users`.
- Full media support: inbound image/file/video/voice download from the iLink
  CDN, outbound image/video/file upload, and audio as file fallback unless a
  native voice format is proven safe.
- Scheduled delivery is supported only for users with a cached `context_token`.

## Setup

```bash
xagent init
xagent channel weixin setup
xagent channel weixin start
xagent channel weixin logs -f
```

Setup scans a WeChat QR code, stores iLink credentials under the runtime's
`weixin/accounts/` directory, and writes non-secret channel settings to
`config.yaml`:

```yaml
channels:
  weixin:
    account_id: e06c1ceea05e@im.bot
    owner_user_id: o9cq800kum_4g8Py8Qw5G0a@im.wechat
    base_url: https://ilinkai.weixin.qq.com
    cdn_base_url: https://novac2c.cdn.weixin.qq.com/c2c
    owner_only: true
    media_enabled: true
```

Credential, cursor, and context-token files are stored with restrictive file
permissions when the platform allows it. Do not commit the runtime directory.

## Runtime Notes

The iLink receive side is long polling via `/ilink/bot/getupdates`. The adapter
persists the opaque `get_updates_buf` cursor after each successful response.
Replies require the latest inbound `context_token` for the user, so proactive
or scheduled sends can only target users who have already messaged the bot.

If iLink returns `ret` or `errcode` `-14`, the channel stops and logs an
instruction to rerun setup. It does not auto-login in the background.

Typing indicators use `getconfig` plus `sendtyping`; message streaming is sent
as ordered final messages because normal WeChat bot chats do not reliably render
`GENERATING` updates as editable bubbles.
