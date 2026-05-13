# xAgent Technical Integration Guide

本文档面向需要把 xAgent 接入外部系统的开发者，例如飞书机器人、企业内部 IM、网页控制台、传感器事件服务或其他后端应用。内容按当前代码整理，覆盖运行配置、HTTP API、WebSocket API、消息与记忆行为，以及对外接入建议。

## 1. 运行模型

xAgent 由三个主要部分组成：

- CLI 入口：`xagent.interfaces.cli`，提供 `xagent init`、`xagent chat`、`xagent run/start/stop/status/logs` 等命令。
- Agent 运行时：`xagent.core.agent.Agent`，负责对话、观察事件、工具调用、消息流和长期记忆。
- HTTP 服务：`xagent.interfaces.server.AgentHTTPServer`，基于 FastAPI，对外提供 HTTP、SSE 和 WebSocket 接口。

默认运行目录是 `~/.xagent`。也可以通过 CLI 的 `--dir` 指定其他目录。

```bash
xagent init --dir ~/.xagent
xagent run --channel web --dir ~/.xagent --host 127.0.0.1 --port 8010
```

服务默认绑定：

- Host：`127.0.0.1`
- Port：`8010`
- Web UI：`web` channel 默认开启；用 `--channel http` 只启动 API，不挂载内置网页
- API 文档：FastAPI 默认提供 `/docs` 和 `/openapi.json`

> 当前服务端没有内置 API 鉴权。对外网、飞书、企业 IM 或公网回调接入时，应放在自己的网关或适配服务后面，先完成平台签名校验、鉴权、限流和审计，再转发到 xAgent。

## 2. 运行目录与配置

运行目录结构：

```text
~/.xagent/
  config.yaml
  identity.md
  memory/
    daily/
    weekly/
    monthly/
    yearly/
  messages/
    messages.sqlite3
  run/
    web.pid
    feishu.pid
  logs/
    web.log
    feishu.log
```

### 2.1 config.yaml

`config.yaml` 支持这些顶层键：

- `provider`：必填，模型提供方配置。
- `search`：可选，联网搜索工具配置。
- `output_schema`：可选，结构化输出配置。
- `channels`：可选，HTTP/Web/Feishu 等运行入口配置。
- `runtime`：可选，CLI 运行时默认值。

出现其他顶层键会在启动时失败，例如 `agent`、`system_prompt`、`server`、`workspace` 都不是当前支持的配置项。

最小示例：

```yaml
provider:
  name: openai
  base_url: https://api.openai.com/v1
  api_key: your_api_key_here
  model: gpt-5.4-mini
search:
  provider: openai
channels:
  http:
    enabled: true
    host: 127.0.0.1
    port: 8010
    web: true
runtime:
  default_channel: web
```

`provider.model` 是必填项。`provider.name` 建议填写，用于区分 OpenAI、DeepSeek、Qwen 或自定义 OpenAI-compatible 服务。`provider.base_url` 和 `provider.api_key` 会传给 OpenAI SDK 的 `AsyncOpenAI` 客户端；如果二者都不提供，则使用 SDK 默认客户端行为。

常见提供方：

```yaml
# OpenAI
provider:
  name: openai
  base_url: https://api.openai.com/v1
  api_key: your_api_key_here
  model: gpt-5.4-mini

# DeepSeek
provider:
  name: deepseek
  base_url: https://api.deepseek.com
  api_key: your_api_key_here
  model: deepseek-v4-pro

# Qwen OpenAI-compatible
provider:
  name: qwen
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: your_api_key_here
  model: qwen3.6-flash
```

### 2.2 search

`search.provider` 支持：

- `openai`：使用 OpenAI Responses API 的内置 web search，仅当 `provider.name: openai` 或 `provider.base_url: https://api.openai.com/v1` 时允许。
- `duckduckgo`：不需要 API key。
- `brave`：需要 `search.api_key`，也可通过环境变量 `BRAVE_SEARCH_API_KEY` 或 `BRAVE_API_KEY` 提供。
- `none`：关闭联网搜索工具。

Brave 示例：

```yaml
search:
  provider: brave
  api_key: YOUR_BRAVE_SEARCH_API_KEY
  safesearch: moderate
```

当搜索开启时，Agent 会加载 `web_search` 工具；无论搜索是否开启，`run_command` 工具都会作为内置工具加载。

### 2.3 output_schema

`output_schema` 用于把模型回复约束为 Pydantic 结构。支持字段类型：`str`、`int`、`float`、`bool`、`list`、`dict`；`list` 可以通过 `items` 指定元素类型。

```yaml
output_schema:
  class_name: WeatherReport
  fields:
    location:
      type: str
      description: Location name
    temperature_celsius:
      type: int
      description: Temperature in degrees Celsius
    condition:
      type: str
      description: Short weather condition summary
```

启用结构化输出后，Agent 会使用 JSON object 模式，并且内部会关闭流式文本增量。即使请求里传 `stream: true`，SSE/WebSocket 也会返回一个完整 `message`，而不是多个 `delta`。

### 2.4 identity.md

`identity.md` 是必填文件，内容不能为空。它定义 Agent 的身份、语气和行为边界。服务启动时会读取该文件；也可以通过 HTTP 接口热更新：

```http
PUT /api/agent/identity
Content-Type: application/json

{"identity":"# Identity\n\nYou are a helpful assistant."}
```

### 2.5 channels

`channels.http` 控制 HTTP API 和内置网页。`web: true` 时，对用户暴露为 `web` channel；`web: false` 时，对用户暴露为 `http` channel。`web` 包含 HTTP API、SSE 和 WebSocket，只是额外挂载静态页面。

```yaml
channels:
  http:
    enabled: true
    host: 127.0.0.1
    port: 8010
    web: true
```

`channels.feishu` 由 `xagent init feishu` 写入。`${ENV_VAR}` 形式会在 Feishu adapter 加载配置时展开，也可通过 `LARK_APP_ID` 和 `LARK_APP_SECRET` 提供凭据。

```yaml
channels:
  feishu:
    enabled: true
    app_id: cli_xxx
    app_secret: ${LARK_APP_SECRET}
    log_level: info
    stream: false
    enable_memory: true
    group_history_count: 10
    show_sender_ids: true
```

## 3. 启动参数

```bash
xagent run --channel web \
  --dir ~/.xagent \
  --host 127.0.0.1 \
  --port 8010 \
  --max-concurrent-chats 4 \
  --queue-timeout 30 \
  --chat-timeout 600
```

`run` 表示前台运行，适合开发和调试。`start` 表示托管后台运行，会把 PID 写入 `<dir>/run/{channel}.pid`，日志写入 `<dir>/logs/{channel}.log`。

```bash
xagent start --channel web
xagent start --channel http,feishu
xagent start --channel all
xagent status --channel all
xagent logs --channel feishu --follow
xagent stop --channel all
```

参数说明：

- `--dir`：运行目录，默认 `~/.xagent`。
- `--host`：监听地址，默认 `127.0.0.1`。
- `--port`：监听端口，默认 `8010`。
- `--open`：启动后打开 Web UI。
- `--channel web`：启动 HTTP API + 内置 Web UI。
- `--channel http`：只启动 HTTP API、SSE 和 WebSocket，不挂载内置 Web UI。
- `--channel feishu`：启动飞书 WebSocket 长连接适配器。
- `--channel all`：启动 `config.yaml` 中已启用的所有 channel。
- `--max-concurrent-chats`：最大并发对话/观察请求数，默认 `4`。
- `--queue-timeout`：等待可用并发槽的秒数，默认 `30`；超时返回 429。
- `--chat-timeout`：单次对话/观察总超时时间，默认 `600` 秒；超时返回 504 或流式错误帧。

## 4. HTTP API

默认 Base URL：

```text
http://127.0.0.1:8010
```

所有 JSON API 都使用 `Content-Type: application/json`。

### 4.1 健康检查

```http
GET /i/health
```

返回：

```json
"ok"
```

```http
GET /health
```

返回：

```json
{"status":"healthy","service":"xAgent HTTP Server"}
```

### 4.2 POST /chat

用于直接对话。适合私聊、飞书机器人被 @、用户明确向 Agent 提问等场景。

请求体：

```json
{
  "user_id": "alice",
  "user_message": "帮我总结一下今天的会议",
  "image_source": null,
  "stream": false,
  "history_count": 100,
  "max_iter": 10,
  "max_concurrent_tools": 10,
  "enable_memory": true,
  "private": false
}
```

字段说明：

- `user_id`：必填，当前说话人的稳定 ID。接入飞书时建议使用 `open_id`、`union_id` 或内部用户 ID。
- `user_message`：必填，用户消息文本。
- `image_source`：可选，图片来源，支持字符串或字符串数组。可传图片 URL、`data:image/...;base64,...`，或服务端可访问的本地文件路径。文本里的图片 URL 和 Markdown 图片也会被自动识别。
- `stream`：可选，默认 `false`。`true` 时走 SSE。
- `history_count`：可选，默认 `100`，但实际注入模型前最多使用最近 `40` 条消息。
- `max_iter`：可选，默认 `10`，工具调用循环上限。
- `max_concurrent_tools`：可选，默认 `10`，单轮最多并发执行的工具数。
- `enable_memory`：可选，默认 `true`。`false` 时不读取记忆，也不暴露记忆读写工具。
- `private`：可选，默认 `false`。`true` 时本轮使用临时私密消息流，不写入主消息库；仍可在 `enable_memory: true` 时读取记忆，但不会写入长期记忆。

非流式响应：

```json
{
  "reply": "会议主要讨论了三个事项..."
}
```

如果配置了 `output_schema`，`reply` 可能是对象：

```json
{
  "reply": {
    "location": "Shanghai",
    "temperature_celsius": 23,
    "condition": "Cloudy"
  }
}
```

SSE 请求：

```bash
curl -N http://127.0.0.1:8010/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "alice",
    "user_message": "写一段欢迎语",
    "stream": true
  }'
```

SSE 数据格式：

```text
data: {"delta":"你"}

data: {"delta":"好"}

data: [DONE]
```

也可能返回完整消息或错误：

```text
data: {"message":"完整回复"}

data: [DONE]
```

```text
data: {"error":"Agent chat timed out.","status_code":504}

data: [DONE]
```

常见错误：

- `422`：请求体字段不符合 Pydantic 校验。
- `429`：并发槽等待超时。
- `500`：服务端处理异常。
- `504`：Agent 处理超时。

### 4.3 POST /observe

用于记录“观察到的上下文”。`/observe` 是纯摄入接口：事件会进入消息流并可用于后续上下文和记忆，但不会触发即时回复。适合群聊中未 @ 机器人的消息、环境传感器、通知、语音转写、房间状态等。

请求体：

```json
{
  "context": "Bob 说项目演示可能提前到下午三点。",
  "source": "feishu",
  "event_type": "group_message",
  "metadata": {
    "chat_id": "oc_xxx",
    "message_id": "om_xxx",
    "speaker_id": "bob",
    "addressed_to_agent": false
  }
}
```

字段说明：

- `context`：必填，观察内容。
- `source`：可选，事件来源，默认 `environment`。
- `event_type`：可选，事件类型，默认 `observation`。
- `metadata`：可选，外部系统元数据。需要表达说话人、动作发起者、房间、消息 ID 等归因信息时，用明确字段，例如 `speaker_id`、`actor_id`、`chat_id`、`message_id`。

`/observe` 没有当前说话人，也没有回复投递目标。直接提问、私聊消息、群聊 @ 机器人消息应使用 `/chat`，由外部适配层负责回复目标和投递方式。

响应：

```json
{
  "kind": "observe",
  "replied": false,
  "reply": null,
  "event_id": 1760000000.123,
  "event_type": "group_message",
  "source": "feishu"
}
```

### 4.4 POST /clear_messages

清空短期消息流，不删除长期记忆 markdown。

```http
POST /clear_messages
```

返回：

```json
{
  "status": "success",
  "message": "Message stream cleared"
}
```

### 4.5 监控与管理接口

这些接口主要服务内置监控页面，也可供外部控制台使用。

#### GET /api/agent/info

返回当前 Agent 元数据：

```json
{
  "model": "gpt-5.4-mini",
  "workspace": "/Users/you/.xagent",
  "memory_dir": "/Users/you/.xagent/memory",
  "message_storage": {
    "stream": "local",
    "backend": "local",
    "path": "/Users/you/.xagent/messages/messages.sqlite3"
  },
  "tools": ["run_command", "web_search", "write_daily_memory", "search_memory", "generate_memory_summary"],
  "identity": "# Identity\n\nYou are a helpful assistant.",
  "identity_file": "identity.md",
  "identity_path": "/Users/you/.xagent/identity.md",
  "identity_editable": true,
  "system_prompt": "# Identity\n\nYou are a helpful assistant."
}
```

#### GET /api/agent/identity

读取 `identity.md`：

```json
{
  "identity": "# Identity\n\nYou are a helpful assistant.\n",
  "path": "/Users/you/.xagent/identity.md",
  "filename": "identity.md",
  "modified": 1760000000.123
}
```

#### PUT /api/agent/identity

写入 `identity.md` 并更新运行中的 Agent。空内容会返回 `400`。

```json
{"identity":"# Identity\n\nYou are a practical assistant."}
```

#### GET /api/memory/tree

返回 `memory/` 下的 markdown 文件树：

```json
{
  "tree": [
    {
      "name": "daily",
      "path": "daily",
      "type": "dir",
      "children": []
    }
  ]
}
```

#### GET /api/memory/read?path=...

读取指定记忆 markdown 文件。`path` 必须是 `memory/` 内的相对路径，只允许读取 `.md` 文件。

```http
GET /api/memory/read?path=daily/2026/2026-05/2026-05-11.md
```

返回：

```json
{
  "path": "daily/2026/2026-05/2026-05-11.md",
  "content": "...",
  "modified": 1760000000.123
}
```

#### GET /api/memory/search

按文件名和文件内容搜索记忆。

```http
GET /api/memory/search?query=会议&limit=50
```

`limit` 范围是 `1` 到 `200`，默认 `50`。

#### POST /api/memory/clear

删除并重建整个 `memory/` 目录。该操作不可恢复，外部控制台接入时应额外加权限控制。

```json
{"status":"ok"}
```

#### GET /api/messages

分页读取短期消息流。

```http
GET /api/messages?count=50&offset=0
```

参数：

- `count`：`1` 到 `500`，默认 `50`。
- `offset`：跳过最近多少条，默认 `0`。

返回中的 `messages` 是按时间正序排列；内部查询时会按新到旧分页，再反转给前端。

```json
{
  "messages": [
    {
      "role": "user",
      "type": "message",
      "content": "你好",
      "sender_id": "alice",
      "timestamp": 1760000000.123,
      "metadata": {}
    }
  ],
  "total": 1,
  "count": 50,
  "offset": 0,
  "has_more": false
}
```

#### GET /api/messages/stats

返回消息数量、存储后端和最早/最新时间戳。

## 5. WebSocket API

WebSocket 适合自建前端、实时机器人网关或需要多轮复用连接的服务。连接建立后，可以在同一连接上连续发送多次请求。每次请求的响应都以 `{"type":"done"}` 结束。

### 5.1 /ws/chat

连接：

```text
ws://127.0.0.1:8010/ws/chat
```

发送的 JSON 与 `/chat` 请求体相同。

非流式示例：

```json
{
  "user_id": "alice",
  "user_message": "你好",
  "stream": false
}
```

接收：

```json
{"type":"message","message":"你好，我在。"}
```

```json
{"type":"done"}
```

流式示例：

```json
{
  "user_id": "alice",
  "user_message": "写一段短欢迎语",
  "stream": true
}
```

接收：

```json
{"type":"delta","delta":"欢迎"}
```

```json
{"type":"delta","delta":"加入"}
```

```json
{"type":"done"}
```

错误帧：

```json
{
  "type": "error",
  "error": "Invalid chat payload.",
  "status_code": 422,
  "details": []
}
```

超时帧：

```json
{
  "type": "error",
  "error": "Agent chat timed out.",
  "status_code": 504
}
```

### 5.2 /ws/observe

连接：

```text
ws://127.0.0.1:8010/ws/observe
```

发送的 JSON 与 `/observe` 请求体相同。

```json
{
  "context": "会议室灯打开了。",
  "source": "sensor",
  "event_type": "light",
  "metadata": {"room":"meeting-room-a"}
}
```

接收：

```json
{
  "type": "result",
  "result": {
    "kind": "observe",
    "replied": false,
    "reply": null,
    "event_id": 1760000000.123,
    "event_type": "light",
    "source": "sensor"
  }
}
```

```json
{"type":"done"}
```

## 6. 消息、记忆与隐私行为

### 6.1 短期消息流

默认消息存储在 SQLite：

```text
~/.xagent/messages/messages.sqlite3
```

存储的是一个全局有序消息流，包括：

- 用户消息：`role=user`，`sender_id` 是请求里的 `user_id`。
- Agent 回复：`role=assistant`，`sender_id=agent`。
- 观察事件：`role=environment`，`type=context_event`。

工具调用消息只在模型循环中临时使用，不作为普通消息长期写入。

### 6.2 长期记忆

长期记忆是 markdown 文件：

```text
memory/
  daily/<year>/<year-month>/<date>.md
  weekly/<year>/<week_start>_to_<week_end>.md
  monthly/<year>/<year-month>.md
  yearly/<year>.md
```

Agent 会基于对话和观察事件异步整理日记式记忆。`enable_memory` 和 `private` 会影响 `/chat` 的记忆行为：

| 参数 | 读取记忆 | 写入记忆 | 写入主消息库 |
| --- | --- | --- | --- |
| `enable_memory=true, private=false` | 是 | 是 | 是 |
| `enable_memory=false, private=false` | 否 | 否 | 是 |
| `enable_memory=true, private=true` | 是 | 否 | 否，写入临时私密消息流 |
| `enable_memory=false, private=true` | 否 | 否 | 否，写入临时私密消息流 |

私密模式会在切回普通模式时丢弃临时消息流。

`/observe` 总是写入主消息流；是否值得进入长期日记由事件内容、`event_type` 和 `metadata.memory_policy` 等元数据决定。常见值为 `memory_policy: "never"`、`"auto"` 或 `"always"`。

## 7. 飞书机器人接入建议

建议不要让飞书直接访问 xAgent，而是增加一个“飞书适配服务”：

```text
Feishu Event/Webhook
  -> Adapter Service
     -> 校验飞书签名、tenant、事件类型、权限
     -> 映射 user_id / chat_id / message_id
     -> 调用 xAgent HTTP 或 WebSocket
     -> 调用飞书发送消息 API 回复
```

### 7.1 私聊或 @ 机器人

使用 `/chat`：

```json
{
  "user_id": "feishu_open_id_xxx",
  "user_message": "帮我写一版周报",
  "stream": false,
  "enable_memory": true,
  "private": false
}
```

飞书字段映射建议：

- `user_id`：优先使用稳定用户 ID，如 `union_id` 或内部账号 ID。
- `user_message`：飞书消息纯文本内容；如果是富文本，需要适配服务先转换为可读文本。
- `image_source`：如果消息里有图片，适配服务应先拿到临时下载 URL，或下载后转成 data URI 再传给 xAgent。

### 7.2 群聊中未 @ 机器人

使用 `/observe` 记录为环境上下文，不即时插话：

```json
{
  "context": "群聊中 Bob 说：下午的演示提前到三点。",
  "source": "feishu",
  "event_type": "group_message",
  "metadata": {
    "chat_id": "oc_xxx",
    "message_id": "om_xxx",
    "speaker_id": "feishu_open_id_bob",
    "addressed_to_agent": false
  }
}
```

如果响应里 `replied` 为 `false`，适配服务不需要发送飞书消息。如果为 `true`，把 `reply` 发回群聊。

### 7.3 流式回复

飞书普通消息发送通常更适合非流式 `/chat`。如果你的飞书端实现了“更新消息卡片”或“打字机效果”，可以使用：

- HTTP SSE：`POST /chat` 且 `stream: true`
- WebSocket：`/ws/chat` 且 `stream: true`

外部适配服务应把多个 `delta` 累积为完整文本，并按飞书 API 限速更新消息，最后在 `done` 后完成收尾。

### 7.4 多用户与记忆边界

xAgent 会把 `user_id` 作为当前说话人的身份注入上下文。接入外部系统时必须保持 ID 稳定，否则长期记忆和多用户归因会变差。

建议：

- 私聊：`user_id = union_id` 或内部用户 ID。
- 群聊：@ 机器人时走 `/chat`，`user_id` 使用当前发言人的稳定 ID；未 @ 的环境消息走 `/observe`，真实发言人放入 `metadata.speaker_id`，并在 `context` 中保留可读归因。
- 跨平台：同一个真实用户最好映射到同一个内部 ID。
- 敏感会话：使用 `private: true`；如果也不希望读取历史记忆，再加 `enable_memory: false`。

## 8. 最小客户端示例

### 8.1 Python HTTP

```python
import httpx


def chat(message: str, user_id: str = "alice") -> str:
    response = httpx.post(
        "http://127.0.0.1:8010/chat",
        json={
            "user_id": user_id,
            "user_message": message,
            "stream": False,
        },
        timeout=620,
    )
    response.raise_for_status()
    return response.json()["reply"]
```

### 8.2 Python SSE

```python
import json
import httpx


with httpx.stream(
    "POST",
    "http://127.0.0.1:8010/chat",
    json={
        "user_id": "alice",
        "user_message": "写一句欢迎语",
        "stream": True,
    },
    timeout=620,
) as response:
    response.raise_for_status()
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ")
        if payload == "[DONE]":
            break
        event = json.loads(payload)
        if "delta" in event:
            print(event["delta"], end="", flush=True)
        elif "message" in event:
            print(event["message"])
        elif "error" in event:
            raise RuntimeError(event["error"])
```

### 8.3 JavaScript WebSocket

```javascript
const ws = new WebSocket("ws://127.0.0.1:8010/ws/chat");

ws.addEventListener("open", () => {
  ws.send(JSON.stringify({
    user_id: "alice",
    user_message: "写一句欢迎语",
    stream: true
  }));
});

let text = "";
ws.addEventListener("message", (event) => {
  const frame = JSON.parse(event.data);
  if (frame.type === "delta") {
    text += frame.delta;
  } else if (frame.type === "message") {
    text = typeof frame.message === "string"
      ? frame.message
      : JSON.stringify(frame.message);
  } else if (frame.type === "error") {
    console.error(frame.error, frame.details || "");
  } else if (frame.type === "done") {
    console.log(text);
  }
});
```

## 9. 对外部署检查清单

- 不要把未加鉴权的 xAgent 端口直接暴露到公网。
- 外部适配服务负责平台签名校验，例如飞书的 challenge、timestamp、nonce、signature。
- 为每个平台用户生成稳定 `user_id`。
- 群聊、传感器、通知类事件优先用 `/observe`；直接提问才用 `/chat`。
- 图片应传公网可访问 URL、临时下载 URL 或 data URI；本地文件路径只对 xAgent 服务进程所在机器有效。
- 根据业务设置 `--max-concurrent-chats`、`--queue-timeout`、`--chat-timeout`。
- 对 `POST /api/memory/clear`、`POST /clear_messages`、`PUT /api/agent/identity` 做额外权限控制。
- 对飞书等平台回复做长度截断、频率控制和失败重试。
