# xAgent Technical Integration Guide

本文档面向需要把 xAgent 接入外部系统的开发者，例如飞书机器人、企业内部 IM、网页控制台、传感器事件服务或其他后端应用。内容严格按当前代码整理，覆盖运行配置、CLI、HTTP API、WebSocket API、工具、调度器、消息与记忆行为，以及对外接入建议。

## 1. 运行模型

xAgent 由四个主要部分组成：

- CLI 入口：`xagent.interfaces.cli`，提供 `xagent init`、`xagent chat`、`xagent voice`、`xagent web`、`xagent observe`、`xagent service ...`、`xagent doctor`、`xagent inspect ...` 等命令。
- Agent 运行时：`xagent.core.agent.Agent`，负责对话、观察事件、工具调用、消息流和长期记忆。
- HTTP 服务：`xagent.interfaces.server.AgentHTTPServer`，基于 FastAPI，对外提供 final-only HTTP JSON 和 WebSocket 事件接口，并托管内置 Web UI。
- 后台运行时：文件系统调度器（`tasks/`）和 runtime heartbeat（长期记忆维护），随 API server 和 Feishu daemon 自动启动。

默认运行目录是 `~/.xagent`。也可以通过 CLI 的 `--dir` 指定其他目录。

```bash
xagent init --dir ~/.xagent
xagent web --dir ~/.xagent --host 127.0.0.1 --port 8010
```

服务默认绑定：

- Host：`127.0.0.1`
- Port：`8010`
- API channel：提供 HTTP JSON、WebSocket，以及内置 Web UI
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
  workspace/
  skills/
    .xagent-skills.json
    code-review/
      SKILL.md
      references/
      scripts/
  tasks/
    20260601-143000-a1b2c3d4.json
    failed/
      20260601-143000-a1b2c3d4.json.failed
  run/
    api.pid
    feishu.pid
  logs/
    api.log
    feishu.log
```

### 2.1 config.yaml

`config.yaml` 支持这些顶层键：

- `provider`：必填，模型提供方配置。
- `search`：可选，联网搜索工具配置。
- `image_generation`：可选，图像生成工具配置。
- `output_schema`：可选，结构化输出配置。
- `channels`：可选，API / Feishu / Voice 入口配置。
- `runtime`：可选，后台 runtime（heartbeat、默认 channel）配置。
- `observability`：可选，Langfuse 观测与 tracing 配置。

出现其他顶层键会在启动时失败，例如 `agent`、`system_prompt`、`server`、`workspace`、`skills` 都不是当前支持的配置项。Skills 是目录资源，存放在运行目录的 `skills/` 下，不写入 `config.yaml`。

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
  api:
    host: 127.0.0.1
    port: 8010
```

`provider.model` 是必填项。`provider.name` 决定唯一的模型 API protocol：

| provider.name | model_api（运行时分支） | 默认 base_url |
| --- | --- | --- |
| `openai` | `openai_responses` | `https://api.openai.com/v1` |
| `deepseek` | `openai_chat_completions` | `https://api.deepseek.com` |
| `qwen` | `openai_chat_completions` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `minimax` | `anthropic_messages` | `https://api.minimaxi.com/anthropic` |
| `anthropic` | `anthropic_messages` | `https://api.anthropic.com` |
| `custom` | 必须显式配置 `model_api` | — |

`provider.base_url` 和 `provider.api_key` 会传给该协议对应的客户端；Anthropic Messages 路径会在工具调用后回传完整 assistant content blocks，以兼容 thinking/tool_use 要求。内部只保存 `model_api` 这一个运行时分支。Custom provider 需要显式配置 `provider.model_api`，可选值为 `openai_responses`、`openai_chat_completions`、`anthropic_messages`。

`provider` 支持的全部键：`name`、`model`（必填）、`base_url`、`api_key`、`model_api`（仅 custom）、`sdk`（旧式别名，等价 `model_api`，仅 custom，不能与 `model_api` 同时出现）、`max_tokens`（正整数，默认 `4096`）、`supports_vision`（布尔，覆盖默认视觉能力）。出现其他键会在启动时失败。

常见提供方：

```yaml
# OpenAI
provider:
  name: openai
  base_url: https://api.openai.com/v1
  api_key: your_api_key_here
  model: gpt-5.4-mini

# DeepSeek（OpenAI Chat Completions 兼容）
provider:
  name: deepseek
  base_url: https://api.deepseek.com
  api_key: your_api_key_here
  model: deepseek-chat

# MiniMax（Anthropic Messages 兼容）
provider:
  name: minimax
  base_url: https://api.minimaxi.com/anthropic
  api_key: your_api_key_here
  model: MiniMax-M2

# Qwen（OpenAI Chat Completions 兼容）
provider:
  name: qwen
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: your_api_key_here
  model: qwen-flash

# Anthropic
provider:
  name: anthropic
  base_url: https://api.anthropic.com
  api_key: your_api_key_here
  model: claude-sonnet-4-20250514

# Custom（OpenAI 兼容）
provider:
  name: custom
  model_api: openai_chat_completions
  base_url: https://api.example.com/v1
  api_key: your_api_key_here
  model: your_model_here

# Custom（Anthropic 兼容）
provider:
  name: custom
  model_api: anthropic_messages
  base_url: https://api.example.com/anthropic
  api_key: your_api_key_here
  model: your_model_here
```

### 2.2 search

`xagent init` 会对所有模型 provider 显式询问 `search.provider`，不会根据主 provider 自动启用搜索。`search.provider` 支持：

- `openai`：使用 OpenAI Responses API 的内置 `web_search`。任意模型 provider 都可选择；当主 `provider.name` 不是 OpenAI 时，必须额外配置 `search.api_key` 作为 OpenAI API key。可选 `search.model` 指定用于搜索的 OpenAI 模型。OpenAI search 工具参数支持 `query`、`max_results`、`search_context_size`、`country`、`city`、`region`、`timezone`、`allowed_domains`、`blocked_domains`、`external_web_access`、`return_token_budget` 和 `force_search`。
- `qwen`：使用 DashScope OpenAI-compatible Responses API 的内置 `web_search`，默认搜索模型 `qwen3-max-2026-01-23`。当主 `provider.name` 是 `qwen` 时复用主 Qwen API key；其它 provider 使用 Qwen search 时需要配置 `search.api_key`。Qwen search 工具参数支持 `query`、`max_results`、`enable_thinking`、`web_extractor`、`code_interpreter`。
- `minimax`：调用 MiniMax search API。当主 `provider.name` 是 `minimax` 时复用主 MiniMax API key；其它 provider 使用 MiniMax search 时需要配置 `search.api_key`。MiniMax search 工具参数只暴露 `query` 和 `max_results`。
- `none`：关闭联网搜索工具。

搜索结果默认返回 `5` 条，最多 `20` 条。

非 OpenAI provider 使用 OpenAI search 示例：

```yaml
provider:
  name: deepseek
  base_url: https://api.deepseek.com
  api_key: YOUR_DEEPSEEK_API_KEY
  model: deepseek-chat

search:
  provider: openai
  api_key: YOUR_OPENAI_API_KEY
  model: gpt-5.4-mini
```

Qwen provider 使用原生 search 示例：

```yaml
provider:
  name: qwen
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: YOUR_DASHSCOPE_API_KEY
  model: qwen-flash

search:
  provider: qwen
  enable_thinking: true
  web_extractor: true
  code_interpreter: true
```

MiniMax search 示例：

```yaml
provider:
  name: minimax
  base_url: https://api.minimaxi.com/anthropic
  api_key: YOUR_MINIMAX_API_KEY
  model: MiniMax-M2

search:
  provider: minimax
```

当搜索开启时，Agent 会加载 `web_search` 工具；无论搜索是否开启，`run_command` 工具都会作为内置工具加载。

### 2.3 image input and generation

图片输入默认只对 `openai` 和 `qwen` provider 开启。这个列表集中在 `xagent/core/providers.py` 的 `VISION_CAPABLE_PROVIDERS`，后续新增内置视觉 provider 时只需要扩展这个集合。其他内置 provider 收到 `image_source` 或消息中的图片 URL 时，会在 Agent 层返回不支持图片输入的提示，不会把 `image_url` / `input_image` payload 发送给模型。`provider.supports_vision` 可以对任意 provider 显式覆盖默认能力，用于所选模型与 provider 默认能力不一致的场景。

为了控制多轮视觉成本并保持上下文边界清晰，xAgent 只会把当前 user turn 显式携带的图片发送给模型。图片仍会保存在消息元数据与 workspace 中用于历史预览，但后续纯文本追问不会自动复用旧图片，除非用户再次上传或在当前消息中显式引用图片。

通用文件接收走 `WorkspaceAttachment` 元数据：文件先保存到 `workspace/`，再以 `attachments` 传入聊天边界。每个 attachment 至少包含 `path` 或 `blob_url`，可附带 `mime_type`、`file_name`、`size_bytes`、`source_channel` 和平台资源 ID。非图片附件不会作为模型二进制输入发送，只会在用户消息中追加 `Attached files:` 清单，并保存在消息 metadata 中供 Web UI、Messages 页面和渠道适配器展示或转发。图片附件是同一套 attachment 的特例；当 provider 支持 vision 时，当前 turn 的图片 attachment 还会进入 `image_source` 流程。

自定义 provider 可以显式声明是否支持图片 URL 理解：

```yaml
provider:
  name: custom
  model_api: openai_chat_completions
  base_url: https://api.example.com/v1
  api_key: YOUR_API_KEY
  model: custom-vision-model
  supports_vision: true
```

`provider.supports_vision` 默认不写入；不写时使用内置 provider 默认能力。`xagent init` 选择 custom provider 时会询问该 provider 是否支持 image URL input；已知 provider 也可以手动写入该字段来覆盖默认值。

`image_generation.provider` 支持 `none`、`openai`、`minimax`、`qwen`，并加载 `generate_image` 工具：

- `openai`：调用 OpenAI Image API。默认模型 `gpt-image-2`，默认 `size: auto`、`quality: auto`、`output_format: png`、`background: auto`。主 provider 是 OpenAI 时复用主 API key。
- `minimax`：调用 MiniMax 图像生成 API。默认模型 `image-01`。主 provider 是 MiniMax 时复用主 API key。支持文生图与通过 `reference_image_url` / `reference_image_urls` 传入主体参考图。
- `qwen`：调用 DashScope 图像生成 API。默认模型 `qwen-image-2.0-pro`。主 provider 是 Qwen 时复用主 API key。
- `none`：关闭图像生成工具。

图像生成配置只允许 provider-native：OpenAI 主 provider 可选 OpenAI 图像生成，MiniMax 主 provider 可选 MiniMax，Qwen 主 provider 可选 Qwen；跨 provider 时必须额外配置 `image_generation.api_key`，否则启动失败。`image_generation` 支持的键包括 `provider`、`api_key`、`base_url`、`endpoint`、`model`、`size`、`quality`、`output_format`、`background`、`output_compression`、`moderation`、`negative_prompt`、`prompt_extend`、`watermark`、`aspect_ratio`、`width`、`height`、`n`、`seed`、`prompt_optimizer`、`aigc_watermark`、`reference_image_url`、`reference_image_urls`、`subject_reference`、`style`，不同 provider 接受的子集不同，传入不支持的值会返回明确错误。

```yaml
# OpenAI 主 provider
image_generation:
  provider: openai

# MiniMax 主 provider
image_generation:
  provider: minimax

# Qwen 主 provider
image_generation:
  provider: qwen
```

生成文件会写入 `workspace/temp/images/`，工具返回结构化 `attachments` 元数据（`path`、`blob_url`、`mime_type`、`file_name`、`size_bytes`），而不是把图片塞进 Markdown 正文；Web chat 和 Messages 页面用附件预览图片或下载文件，CLI 打印本地文件路径，Feishu 走原生图片/文件发送能力。

### 2.4 runtime

`runtime` 控制长期运行进程的后台行为：

```yaml
runtime:
  default_channel: api      # 可选：api | feishu | all
  heartbeat_enabled: true   # 可选，布尔，默认 true
  heartbeat_interval_seconds: 300  # 可选，正数，默认 300
```

- `heartbeat_enabled` / `heartbeat_interval_seconds`：控制 runtime heartbeat。默认开启，默认间隔 `300` 秒。heartbeat 只做长期记忆维护：定期 flush 待写入记忆，并在每周一为上一周生成 weekly summary（详见 2.5）。
- `default_channel`：被校验为 `api`、`feishu` 或 `all` 之一，作为保留配置项。`xagent service start`（未显式传 channel）当前直接从已启用 channel 中按 `api` 优先选择，不读取该字段。

### 2.5 memory

长期记忆默认自动管理，不需要在 `config.yaml` 中配置。每轮对话会自动注入最近 `2` 天的长期记忆上下文；完整原始记录以 `messages/` 为准，长期记忆是基于消息流异步整理出的派生数据。

后台写入按内部批量策略执行：累计待写入消息达到 `20` 条且距上次写入超过 `600` 秒（10 分钟）时立即整理写入；否则在待写入超过 `900` 秒（15 分钟）后兜底 flush。退出 CLI 单次/交互 chat、API server 或 Feishu channel 时不会强制 flush 记忆，未写入的内容仍保留在 `messages/`，后续由批量写入、idle fallback、长驻 runtime heartbeat 或显式 `write_memory` 工具整理进长期记忆。

API server lifespan 和 Feishu daemon 会启动 runtime heartbeat；CLI 单次/交互 chat 不启动。heartbeat 每 `300` 秒执行一次：flush 待写入记忆，并在每周一（`weekday()==0`）为上一周生成尚未存在的 weekly summary。summary 生成是后台维护，不是模型可见工具。

长期记忆是纯时间维度存储，只包含 `daily/`、`weekly/`、`monthly/` 和 `yearly/`。旧版本可能遗留的 `memory/people/` 文件不会被自动删除，但当前记忆 API 和搜索不会再创建、列出或检索它们。

### 2.6 workspace

`workspace/` 是和 `memory/`、`messages/` 同级的外置工作区。它不是自动注入 prompt 的长期记忆，而是 Agent 可自主管理的文件系统空间，可用于项目记录、临时状态、markdown 笔记、脚本、图片和其他产物。

标准 runner 会把 `run_command` 的默认工作目录绑定到 `workspace/`。如果工具调用显式传入 `working_directory`，则按该参数执行。当前版本采用策略边界而不是硬沙箱：Agent 可以在 `workspace/` 内自主创建、覆盖和删除文件；对 `workspace/` 外的写入、删除、安装、网络或 git mutation 操作仍应先获得用户明确确认。

### 2.7 skills

`skills/` 是和 `memory/`、`messages/`、`workspace/` 同级的 Agent Skills 目录。第一版只扫描当前 `--dir` 下的 `skills/`，不扫描项目级 `.agents/skills` 或用户全局目录。

每个 skill 是一个目录，至少包含 `SKILL.md`：

```text
skills/
  code-review/
    SKILL.md
    references/
      checklist.md
    scripts/
      validate.py
    assets/
```

`SKILL.md` 必须以 YAML frontmatter 开头：

```markdown
---
name: code-review
description: Reviews code changes for correctness. Use when reviewing diffs or PRs.
---

# Code Review

## Instructions

...
```

`name` 只能包含小写字母、数字和单个连字符，最长 64 个字符，并且必须和父目录名一致。`description` 必填，最长 1024 个字符，应同时说明 skill 做什么以及何时使用。可选字段包括 `license`、`compatibility`、`metadata` 和实验性的 `allowed-tools`。

xAgent 使用三层渐进式加载：

| Level | 加载时机 | 内容 | 入口 |
| --- | --- | --- | --- |
| Level 1: Metadata | 每轮 system prompt | 启用 skill 的 `name`、`description` 和 `SKILL.md` 路径 | `Available Skills` 系统层 |
| Level 2: Instructions | skill 描述匹配当前任务时 | `SKILL.md` 正文，也就是主要流程和最佳实践 | `read_skill(skill_name)` |
| Level 3: Resources/code | 只有被引用且任务需要时 | references、templates、schemas、examples、scripts 等 bundle 文件 | `read_skill(skill_name, file_path=...)` 或 `run_command` |

`description` 是 discovery metadata：它告诉模型 skill 做什么、什么时候使用；它不是完整操作说明。模型不需要调用工具来列出 skills，因为 `Available Skills` 已经在 system prompt 中暴露。模型侧只注册一个 Skills 加载工具：`read_skill`。默认调用会读取 `SKILL.md`，并返回当前 skill 包内的轻量文件清单；之后只能按需读取同一 skill 目录内的引用文件。`SKILL.md` 正文和 references 不会默认塞进 system prompt。启用/禁用状态写入 `skills/.xagent-skills.json`，不会修改 skill 自身文件，也不会写入 `config.yaml`。

Skill 中的 `scripts/` 是普通文件资源，不会自动注册为 function tool。若 skill 指示运行脚本，Agent 必须通过 `run_command` 执行，并继承 workspace 外操作的现有安全策略。

Web UI 的 Skills 页面支持查看文件树、搜索、创建 `SKILL.md`、启用/禁用和删除 skill。第一版新增只提供表单创建单个 `SKILL.md`；zip/folder 导入和完整多文件编辑可后续扩展。

### 2.8 output_schema

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

启用结构化输出后，Agent 会关闭 token 级文本增量。官方 OpenAI Responses 路径会使用 `text.format` 的 JSON schema；OpenAI-compatible Chat Completions 路径会使用 JSON object 模式；Anthropic Messages 路径会通过 schema prompt 约束输出并做 Pydantic 校验。HTTP `/chat` 始终只返回最终 `reply`；WebSocket `/ws/chat` 仍返回事件边界，但结构化结果会作为完整 `message_done` 输出。

### 2.9 identity.md

`identity.md` 是必填文件，内容不能为空。它定义 Agent 的身份、语气和行为边界，并作为 `instructions` 注入；它不能覆盖核心交互规则、隐私规则、安全规则或工具策略。服务启动时会读取该文件；也可以通过 HTTP 接口热更新：

```http
PUT /api/agent/identity
Content-Type: application/json

{"identity":"# Identity\n\nYou are a helpful assistant."}
```

### 2.10 channels

`channels` 支持 `api`、`feishu`、`voice` 三个键。

`channels.api` 控制 API channel 的监听地址和端口。API channel 始终暴露 HTTP JSON、WebSocket 和内置静态页面；`--open` 只控制启动后是否打开浏览器，不改变服务能力。

```yaml
channels:
  api:
    host: 127.0.0.1
    port: 8010
```

`websocket` 是 API channel 内部的 transport，不是 channel。需要 WebSocket 时运行 `xagent web` 或 `xagent service start api`，然后连接 `/ws/chat`、`/ws/observe` 或 `/ws/tasks`。

未显式传 channel 时，`xagent service start` 会从已启用 channel 中选择单个入口：优先 `api`，如果只启用了 `feishu` 则选择 `feishu`。`stop`、`restart`、`status` 和 `logs` 默认使用 `all`。`logs --follow` 必须显式指定单个 channel。

#### channels.feishu

`channels.feishu` 由 `xagent init feishu` 写入。默认走一键注册：基于飞书设备授权流程（`lark_oapi.register_app`，需 `lark-oapi>=1.5.5`），打印授权链接/二维码，由飞书管理员授权后自动写入 `app_id`/`app_secret`。需要使用已有应用凭据时用 `xagent init feishu --manual`（或直接传 `--app-id`/`--app-secret`）。`${ENV_VAR}` 形式会在 Feishu adapter 加载配置时展开，也可通过 `LARK_APP_ID` 和 `LARK_APP_SECRET` 提供凭据。

支持的全部键与默认值：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `app_id` | 必填 | 飞书 app id（`cli_xxx`） |
| `app_secret` | 必填 | 飞书 app secret |
| `domain` | `None` | `feishu`（默认）、`lark` 或完整自定义域名 |
| `log_level` | `info` | `debug` / `info` / `warn` / `error` |
| `stream` | `false` | 使用飞书 streaming card 增量更新当前回复段 |
| `enable_memory` | `true` | 透传到 agent 的长期记忆开关 |
| `history_count` | `None` | 每轮透传给 `agent.chat` 的历史条数 |
| `max_iter` | `None` | 每轮工具循环上限 |
| `max_concurrent_tools` | `None` | 每轮工具并发上限 |
| `group_history_count` | `10` | 群/话题群每轮拉取的最近消息条数（`0` 关闭） |
| `history_fetch_timeout` | `5.0` | 等待飞书历史的最长秒数 |
| `show_sender_ids` | `false` | 在房间上下文里包含飞书 sender ID |
| `group_reply_without_mention` | `false` | 未 @ 的群/话题群消息也进入 chat |
| `advanced` | `{}` | 透传给 `FeishuChannel` 的高级 kwargs（policy、safety 等） |

```yaml
channels:
  feishu:
    app_id: cli_xxx
    app_secret: ${LARK_APP_SECRET}
    enable_memory: true
    group_history_count: 10
    group_reply_without_mention: false
```

默认情况下，群聊和话题群只有 @bot 的消息会进入 `Agent.chat`。`group_reply_without_mention: true` 会让未 @ 的群聊/话题群消息也进入 chat。开启后需要确认飞书应用已具备普通群消息事件/权限，并关注 `group_history_count`、限流以及多 bot 群里的回复回路风险；如果 `advanced` 里配置了显式 SDK policy，则 policy 优先。

回复语义：普通群和 p2p 回复都以普通消息发送，不用 `reply_to` 引用原消息（飞书的 `reply_to` 会把原作者拉进通知，相当于 @ 对方）。仅话题群保留 `reply_to=root`，这是飞书结构性要求，否则回复会落入用户看不到的隐藏子线程。所有定时/调度交付一律不引用、不 @ 创建提醒的人。

#### channels.voice

`channels.voice` 配置本机前台语音模式。当前版本支持 Soniox、Qwen 与混用两者的 `custom`，不参与 `xagent service start all`，也没有 `/ws/voice`。`enable_interruptions` 默认为 `false`，并由 `xagent init` 写入配置模板。

普通用户只需要通过 `xagent init` 选择语音 provider 并写入 API key：

```yaml
channels:
  voice:
    provider: qwen
    api_key: your_qwen_api_key_here
    enable_interruptions: false
    audio:
      input: auto
      output: auto
    wake:
      enabled: false
      wake_phrases: ["xAgent"]
      exit_phrases: ["exit", "stop", "goodbye", "that's all", "never mind"]
      match_mode: prefix
      idle_timeout_seconds: 60
    stt:
      model: qwen3-asr-flash-realtime
    tts:
      model: qwen3-tts-flash-realtime
      voice: Cherry
```

或 Soniox：

```yaml
channels:
  voice:
    provider: soniox
    api_key: your_soniox_api_key_here
    enable_interruptions: false
    audio:
      input: auto
      output: auto
    stt:
      model: stt-rt-v4
    tts:
      model: tts-rt-v1
      voice: Owen
```

如果需要混用 STT 和 TTS provider，可以在 `xagent init` 的 Voice Provider 里选择 `custom`：

```yaml
channels:
  voice:
    provider: custom
    audio:
      input: auto
      output: auto
    stt:
      provider: qwen
      api_key: qwen-stt-key
      model: qwen3-asr-flash-realtime
    tts:
      provider: soniox
      api_key: soniox-tts-key
      model: tts-rt-v1
      voice: Owen
```

`channels.voice` 顶层键：`provider`（`soniox` / `qwen` / `custom`）、`api_key`、`websocket_base_url`、`enable_interruptions`（默认 `false`）、`audio`、`wake`、`stt`、`tts`。当 `provider` 不是 `custom` 时，`stt.provider` 与 `tts.provider` 必须与顶层 `provider` 一致。

高级配置可以手动覆盖 STT/TTS 默认值。Soniox 高级示例：

```yaml
channels:
  voice:
    provider: soniox
    api_key: soniox-key
    enable_interruptions: false
    stt:
      model: stt-rt-v4
      audio_format: pcm_s16le
      sample_rate: 16000
      num_channels: 1
      enable_endpoint_detection: true
      max_endpoint_delay_ms: 700
      language_hints: ["zh", "en"]
      enable_language_identification: true
      enable_speaker_diarization: false
    tts:
      model: tts-rt-v1
      voice: Owen
      audio_format: pcm_s16le
      sample_rate: 24000
      language_policy: from_stt_dominant
      fallback_language: zh
```

STT 约束：`sample_rate` 必须是 `16000`，`num_channels` 必须是 `1`，`audio_format` 为 `pcm` 或 `pcm_s16le`（Soniox 归一化为 `pcm_s16le`，Qwen 归一化为 `pcm`）；Soniox 下 `enable_endpoint_detection` 必须为 `true`。TTS `sample_rate` 取值为 `8000/16000/24000/44100/48000` 之一。

本机音频设备默认使用 `auto`，会按系统默认输入/输出、同名 duplex 设备、采样率与声道兼容性自动选择。需要固定设备时，可以用设备名或 `#index`：

```yaml
channels:
  voice:
    audio:
      input: "MacBook Pro麦克风"
      output: "MacBook Pro扬声器"
```

先运行 `xagent voice --list-devices` 查看可用设备；临时覆盖可以用 `xagent voice --input-device "MacBook Pro麦克风" --output-device "MacBook Pro扬声器"`。设备 index 会随插拔变化，长期配置更推荐使用设备名。

共享空间里建议开启 `wake`。开启后 runtime 仍会持续把麦克风音频交给 realtime STT，但只有完整转写命中 `wake.wake_phrases` 后才会进入 agent 对话；未唤醒时的背景语音不会调用 agent，也不会作为消息保存。`match_mode` 同时作用于 `wake_phrases` 和 `exit_phrases`：默认 `prefix` 只接受转写句首的短语，误触发更少；需要更宽松时可以改成 `contains`。如果唤醒句后还有内容，例如“xAgent，几点了”，runtime 会剥离唤醒短语并把“几点了”作为用户输入；如果只说唤醒短语本身，runtime 会进入已唤醒状态并等待下一句。

已唤醒状态会在两种情况下恢复到待唤醒：用户说出 `exit_phrases` 中的短语，或超过 `idle_timeout_seconds` 没有新的用户输入。推荐保留空闲超时，避免家庭环境中一次唤醒后长期监听所有对话。语音调度任务仍可按 `delivery_channel: voice` 主动播报，不受 wake gate 影响。

Qwen 默认使用 `qwen3-asr-flash-realtime` 和 `qwen3-tts-flash-realtime`，通过 DashScope Realtime WebSocket 发送 `session.update`、音频/文本 buffer 事件，并接收最终转写与音频 delta。`session_options` 会合并到 Qwen 的 `session.update.session` payload；已显式建模的字段仍以同名配置项为准。

```yaml
channels:
  voice:
    provider: qwen
    api_key: qwen-key
    websocket_base_url: wss://dashscope.aliyuncs.com/api-ws/v1/realtime
    enable_interruptions: false
    stt:
      model: qwen3-asr-flash-realtime
      audio_format: pcm
      sample_rate: 16000
      num_channels: 1
      language: zh
      turn_detection: server_vad
      vad_threshold: 0.2
      silence_duration_ms: 400
      session_options:
        custom_stt_option: true
    tts:
      model: qwen3-tts-flash-realtime
      voice: Cherry
      audio_format: pcm
      sample_rate: 24000
      language_policy: from_stt_dominant
      fallback_language: zh
      max_buffer_chars: 80
      mode: server_commit
      language_type: Auto
      instructions: 语速自然，语气友好。
      optimize_instructions: true
      session_options:
        custom_tts_option: value
```

运行：

```bash
xagent init
xagent voice --dir ~/.xagent
```

语音链路是本机麦克风 PCM → realtime STT → provider-side endpoint detection → wake gate（可选）→ `Agent.chat_events(stream=True)` → realtime TTS → 本机扬声器。默认情况下（`enable_interruptions: false`），播放期间会暂停麦克风流，避免把扬声器声音重新送入 STT。配置 `enable_interruptions: true` 后，播放期间会继续监听麦克风；如果 STT 在当前回复仍在播放时产出新的用户句子，runtime 会取消当前 TTS 和本地播放，并立即处理新的用户输入。开启 wake 时，中断只在已唤醒会话内生效；待唤醒状态下的背景语音仍会被忽略。

### 2.11 observability

`observability` 用于启用 Langfuse 对 OpenAI SDK 路径模型调用的观测。默认 `xagent init` 不写这个顶层键，等价于关闭观测；只有在初始化时明确选择启用，或手动添加 `enabled: true` 时才会加载 Langfuse wrapper。

```yaml
observability:
  enabled: true
  provider: langfuse
  public_key: pk-lf-...
  secret_key: sk-lf-...
  base_url: https://cloud.langfuse.com
  sample_rate: 1.0
  debug: false
  tracing_enabled: true
```

- `provider`：当前仅支持 `langfuse`。
- `public_key` / `secret_key`：Langfuse 项目凭据，启用时必填。
- `base_url`：Langfuse 服务地址。EU 默认是 `https://cloud.langfuse.com`；也可使用 US、Japan、HIPAA 或自托管地址。
- `sample_rate`：可选，范围 `0.0` 到 `1.0`。
- `debug` / `tracing_enabled`：可选布尔，分别映射 Langfuse SDK 的调试和 tracing 开关。

启用后，xAgent 会在创建 OpenAI SDK `AsyncOpenAI` client 时使用 `langfuse.openai.AsyncOpenAI`。官方 OpenAI 主对话使用 Responses API，DeepSeek/Qwen/custom OpenAI-compatible provider 使用 Chat Completions；两者都按 Langfuse SDK 当前能力做 best-effort 捕获。Anthropic SDK 路径当前使用 Anthropic SDK 原生客户端，不经过 Langfuse OpenAI wrapper。xAgent 不会在退出时阻塞等待观测事件提交；未提交事件按 SDK 自身后台策略处理。

### 2.12 文件系统调度器

调度器使用运行目录下的 `tasks/` 作为唯一状态队列。计划任务使用 `YYYYMMDD-HHMMSS-xxxxxxxx.json`，文件名编码当前生效的下一次触发时间，内容是结构化任务 envelope，主要字段包括 `kind=task`、稳定 `id`、`title`、`task.type`（`message` 或 `agent`）、`task.content`、`delivery.channel`、`delivery.target`、`execution`（`history_count` / `max_iter` / `max_concurrent_tools` / `enable_memory`）。周期规则统一写入 `recurrence` 规则列表，例如 `[{"kind":"daily","time":"10:00:00"}]` 或 `[{"kind":"weekly","time":"14:28:00","weekdays":["wed","fri"]}]`。

调度器始终根据这些规则计算下一个未来触发时刻，并继续保留同一个 `task_id`。如果 runtime 停机错过多个周期，恢复后最多补执行一次，然后直接跳到下一个未来时间。失败的任务移动到 `tasks/failed/`。API/Web、Feishu 和 Voice runtime 启动时会自动运行一个轻量消费者，只领取自己能处理的任务（按 `delivery.channel` 过滤：API/Web 处理 `api`/`web`，Feishu 处理 `feishu`，Voice 处理 `voice`）；领取前会原子重命名为运行中状态。

这意味着用户在对话中说“1 分钟后提醒我走两步”时，模型应调用 `manage_scheduled_tasks(action="create", task_type="message", delay_seconds=60, ...)`；说“每天 10 点提醒我写日报”时，应调用 `manage_scheduled_tasks(action="create", task_type="message", recurrence=[{"kind":"daily","time":"10:00:00"}], ...)`；说“每周三、周五 14:28 提醒我走路”时，应调用 `manage_scheduled_tasks(action="create", task_type="message", recurrence=[{"kind":"weekly","time":"14:28:00","weekdays":["wed","fri"]}], ...)`；说“半小时后帮我查一下当前系统的温度然后发我”时，应调用 `manage_scheduled_tasks(action="create", task_type="agent", ...)`。查看和删除任务则分别使用 `action="list"` 与 `action="delete"`（按 `task_id`）。Web UI 的 Tasks tab 通过 `/api/tasks` 查看和管理这些任务；在线 Web 客户端通过 `/ws/tasks` 接收已到期结果。

## 3. CLI 命令

```text
xagent init [--dir DIR] [--force] [--schema]
xagent init feishu [--dir DIR] [--app-id ...] [--app-secret ...] [--manual]
                    [--stream/--no-stream] [--memory/--no-memory]
                    [--group-history-count N] [--show-sender-ids/--no-show-sender-ids]
                    [--group-reply-without-mention/--no-...] [--force]
xagent chat [MESSAGE] [--dir DIR] [--user-id ID] [--verbose]
            [--events] [--stream/--no-stream] [--memory/--no-memory]
xagent voice [--dir DIR] [--user-id ID] [--verbose] [--list-devices]
             [--input-device DEV] [--output-device DEV] [--memory/--no-memory]
xagent web [--dir DIR] [--host H] [--port P] [--open/--no-open]
           [--max-concurrent-chats N] [--queue-timeout S] [--chat-timeout S]
xagent observe TEXT [--dir DIR] [--source S] [--event-type T] [--metadata JSON]
xagent service start [api|feishu|all] [--dir DIR] [api runtime args]
xagent service stop [api|feishu|all] [--dir DIR]
xagent service restart [api|feishu|all] [--dir DIR] [api runtime args]
xagent service status [api|feishu|all] [--dir DIR] [--json]
xagent service logs [api|feishu|all] [--dir DIR] [--lines N] [--follow]
xagent doctor [--dir DIR] [--channel ...] [--online]
xagent inspect config [show|validate|path] [--dir DIR]
xagent inspect identity [show|path] [--dir DIR]
xagent inspect memory [stats|list|clear] [--dir DIR] [--scope ...] [--yes]
xagent inspect memory show PATH [--dir DIR]
xagent inspect memory search QUERY [--dir DIR] [--scope ...]
xagent inspect messages [stats|list|clear] [--dir DIR] [--count N] [--offset N] [--yes]
xagent version
```

- `init`：交互式创建 `config.yaml` 和 `identity.md`。`--force` 覆盖 init 管理的文件，并询问是否清空 `memory/`、`messages/`、`workspace/`、`tasks/`、`skills/`；`--schema` 写入一个示例 `output_schema`。
- `chat`：传入 `MESSAGE` 时单次对话，否则进入交互模式。`--events` 使用分段事件输出，`--stream` 在事件模式下打印增量。
- `voice`：本机前台语音对话；`--list-devices` 列出音频设备后退出。
- `web`：前台运行 API channel 并默认打开 Web UI，适合本地使用和调试。
- `observe`：摄入一条观察上下文，不生成回复。
- `service`：托管后台运行 channel，PID 写入 `<dir>/run/{channel}.pid`，日志写入 `<dir>/logs/{channel}.log`。
- `doctor`：检查本地就绪状态；`--online` 额外做网络/模型检查。
- `inspect`：只读/管理本地配置、identity、长期记忆和消息流。

```bash
xagent service start api
xagent service start all
xagent service status all --json
xagent service logs feishu --follow
xagent service stop all
```

`web` / `service start api|restart` 的 API runtime 参数：

- `--host`：监听地址，默认 `127.0.0.1`。
- `--port`：监听端口，默认 `8010`。
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

用于简单请求-响应对话。HTTP `/chat` 不承担实时、分段或流式职责；它永远只返回最终回答。需要分段事件时使用 `/ws/chat`，或在进程内直接使用 `Agent.chat_events()`。

请求体：

```json
{
  "user_id": "alice",
  "user_message": "帮我总结一下今天的会议",
  "image_source": null,
  "images": null,
  "attachments": [
    {
      "path": "temp/attachments/web/report.pdf",
      "blob_url": "/api/workspace/blob?path=temp%2Fattachments%2Fweb%2Freport.pdf",
      "mime_type": "application/pdf",
      "file_name": "report.pdf",
      "size_bytes": 12345
    }
  ],
  "history_count": 20,
  "max_iter": 50,
  "max_concurrent_tools": 4,
  "enable_memory": true
}
```

字段说明：

- `user_id`：必填，当前说话人的稳定 ID。接入飞书时建议使用 `open_id`、`union_id` 或内部用户 ID。
- `user_message`：必填，用户消息文本。
- `image_source`：可选，图片来源，支持字符串或字符串数组。可传图片 URL、`data:image/...;base64,...`，或服务端可访问的本地文件路径。文本里的图片 URL 和 Markdown 图片也会被自动识别。
- `images`：可选，结构化图片输入数组（与 `image_source` 等价的另一种传入方式）。
- `attachments`：可选，workspace-backed 文件附件数组。每项至少传 `path` 或 `blob_url`；建议同时传 `mime_type`、`file_name` 和 `size_bytes`。服务端会去重并限制单条消息附件总量不超过 `200MB`，单个附件不超过 `50MB`。非图片附件只作为文件引用进入上下文，图片附件在当前 turn 中按 provider vision 能力进入图片输入流程。
- `history_count`：可选，默认 `20`；实际注入模型前最多使用最近 `30` 条消息。
- `max_iter`：可选，默认 `50`，工具调用循环上限。
- `max_concurrent_tools`：可选，默认 `4`，单轮最多并发执行的工具数。
- `enable_memory`：可选，默认 `true`。`false` 时不读取记忆，也不写入记忆，也不暴露记忆读写工具；消息仍写入主消息库。

请求体含未知字段（包括 `stream`）会因 `extra=forbid` 校验失败返回 `422`。这是有意设计：HTTP 只表达 final-only JSON 协议，避免和 WebSocket 事件协议混用。

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
  "workspace_dir": "/Users/you/.xagent/workspace",
  "memory_dir": "/Users/you/.xagent/memory",
  "message_storage": {
    "stream": "local",
    "backend": "local",
    "path": "/Users/you/.xagent/messages/messages.sqlite3"
  },
  "tools": ["run_command", "web_search", "write_memory", "search_memory"],
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

返回当前时间维度记忆的 markdown 文件树。只包含 `daily/`、`weekly/`、`monthly/` 和 `yearly/`：

```json
{
  "tree": [
    {"name": "daily", "path": "daily", "type": "dir", "children": []}
  ]
}
```

#### GET /api/memory/read?path=...

读取指定记忆 markdown 文件。`path` 必须是 `memory/` 内的相对路径，只允许读取 `.md` 文件。

```http
GET /api/memory/read?path=daily/2026/2026-05/2026-05-11.md
```

#### GET /api/memory/search?query=...&limit=...

按文件名和文件内容搜索当前时间维度记忆。`limit` 范围 `1` 到 `200`，默认 `50`。

#### POST /api/memory/clear

删除并重建整个 `memory/` 目录。该操作不可恢复，外部控制台接入时应额外加权限控制。

```json
{"status":"ok"}
```

#### GET /api/workspace/tree

返回 `workspace/` 下的完整文件树，包含目录、文本文件、图片和其他二进制文件的元数据。

#### GET /api/workspace/read?path=...

读取 `workspace/` 内的相对路径。UTF-8 文本文件会返回 `content`；二进制文件返回元数据和 `blob_url`。路径穿越和指向 workspace 外部的 symlink 会被拒绝。

#### GET /api/workspace/blob?path=...

以文件响应形式返回 workspace 文件，用于图片预览或二进制下载。

#### GET /api/workspace/search?query=...&limit=...

按文件名、相对路径和 UTF-8 文本内容搜索 workspace。大文件和二进制文件只参与文件名/路径匹配。

#### POST /api/workspace/clear

清空 `workspace/` 内的所有内容，但保留 workspace 根目录。会删除 workspace 内的 symlink 本身，不跟随指向 workspace 外部的 symlink 目标。

```json
{"status":"ok","message":"Workspace cleared","deleted":3}
```

#### PUT /api/workspace/write

写入 UTF-8 文本文件，可自动创建父目录。

```json
{"path":"notes/today.md","content":"# Today\n\n...","create_parents":true}
```

#### DELETE /api/workspace/delete?path=...&recursive=true

删除 workspace 内的文件或目录。不能删除 workspace 根目录；非空目录需要 `recursive=true`。

#### POST /api/workspace/upload

使用 multipart form 上传文件。字段：`file` 为上传文件，`path` 可选；`path` 可以是目标文件路径，也可以用尾部 `/` 表示目标目录。

上传图片会校验真实内容为 PNG、JPEG 或 WebP，单图最大 `10MB`；非图片附件最大 `50MB`。响应包含 `path`、`mime_type`、`size` 和 `blob_url`，可以直接作为 `/chat` 或 `/ws/chat` 的 `attachments` 条目继续发送。

```json
{
  "status": "ok",
  "name": "report.pdf",
  "path": "temp/attachments/web/report.pdf",
  "type": "file",
  "size": 12345,
  "mime_type": "application/pdf",
  "binary": true,
  "blob_url": "/api/workspace/blob?path=temp%2Fattachments%2Fweb%2Freport.pdf"
}
```

#### Skills 接口

- `GET /api/skills/info`：返回 skills 根目录、启用/禁用/无效计数、已发现 skill 元数据和整体验证结果。
- `GET /api/skills/tree`：返回 `skills/` 下的安全文件树，附带每个已发现 skill 的 metadata；`.xagent-skills.json` 不出现在树中。
- `GET /api/skills/read?path=...`：读取 `skills/` 内相对路径。路径穿越和外部 symlink 会被拒绝。
- `GET /api/skills/search?query=...&limit=...`：搜索 skills 文件名、路径和文本内容。
- `POST /api/skills/create`：创建新的 skill 目录和 `SKILL.md`。`name`、`description` 必填，`body` 可选。
- `PUT /api/skills/write`：写入 `skills/` 内 UTF-8 文本文件；保留的 `.xagent-skills.json` 不能直接写入。
- `DELETE /api/skills/delete?path=...&recursive=true`：删除 skills 内的文件或目录。
- `PUT /api/skills/state`：启用或禁用一个有效 skill，状态写入 `.xagent-skills.json`，例如 `{"name":"code-review","enabled":false}`。
- `GET /api/skills/validate[?name=...]`：验证一个或所有 skill 的 `SKILL.md` frontmatter。

#### GET /api/messages?count=...&offset=...

分页读取短期消息流。`count` 范围 `1` 到 `500`，默认 `50`；`offset` 跳过最近多少条，默认 `0`。返回中的 `messages` 按时间正序排列；内部查询时按新到旧分页，再反转给前端。

```json
{
  "messages": [
    {
      "role": "user",
      "type": "message",
      "content": "你好",
      "sender_id": "alice",
      "timestamp": 1760000000.123,
      "metadata": {},
      "attachments": [],
      "attachment_count": 0
    }
  ],
  "total": 1,
  "count": 50,
  "offset": 0,
  "has_more": false
}
```

#### GET /api/messages/search?query=...&limit=...

按角色、内容、发送者、工具调用和 metadata 搜索消息流，返回命中的字段（`matched_in`）和摘要片段（`snippet`）。

#### GET /api/messages/stats

返回消息数量、存储后端和最早/最新时间戳。

#### GET /api/tasks

列出 `tasks/` 中当前 active 的任务。每条记录至少包含 `task_id`、`title`、`task_type`、`content`、`next_run_at`、结构化 `recurrence` 规则列表、`status`、`channel`。

#### DELETE /api/tasks/delete?task_id=...

删除一个 active 任务。`task_id` 必须来自 `/api/tasks` 返回结果。

## 5. WebSocket API

WebSocket 适合自建前端、实时机器人网关或需要多轮复用连接的服务。连接建立后，可以在同一连接上连续发送多次请求。每次请求的响应都以 `{"type":"done"}` 结束。

### 5.1 /ws/chat

连接：

```text
ws://127.0.0.1:8010/ws/chat
```

WebSocket 是唯一远程分段事件协议入口。请求体与 `/chat` 基本相同，但额外支持 `stream`。事件化与文本流式是两件事：无论 `stream` 是否开启，`/ws/chat` 都会返回分段事件；`stream=false`（默认）时不发送 `message_delta`，只发送完整 `message_done`。

`attachments`、`images` 和 `image_source` 的含义与 `/chat` 相同。Web UI 会先调用 `/api/workspace/upload`，再把返回的 workspace `blob_url` 作为 attachment 发送到这里。

请求示例：

```json
{
  "user_id": "alice",
  "user_message": "我们当前在什么目录下？",
  "stream": false,
  "enable_memory": true
}
```

分段响应示例：

```json
{"type":"message_start","message_id":"...","phase":"preface"}
```

```json
{"type":"message_done","message_id":"...","phase":"preface","content":"我去看看"}
```

```json
{"type":"tool_call","call_id":"call_...","name":"run_command"}
```

```json
{"type":"tool_result","call_id":"call_...","name":"run_command"}
```

```json
{"type":"message_start","message_id":"...","phase":"final"}
```

```json
{"type":"message_done","message_id":"...","phase":"final","content":"我们在 /Users/... 目录下。"}
```

```json
{"type":"done"}
```

`stream=true` 时，每个文本段会在 `message_start` 与 `message_done` 之间额外发送 `message_delta`：

```json
{"type":"message_delta","message_id":"...","phase":"final","delta":"我们在 "}
```

`tool_call`/`tool_result` 默认只暴露工具名和调用 ID，不暴露完整参数或结果内容。

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

### 5.3 /ws/tasks

Web UI 使用这个长连接接收已到期提醒。

```text
ws://127.0.0.1:8010/ws/tasks?user_id=web_user
```

到期事件示例：

```json
{"type":"scheduled_message","content":"走两步","task":{"kind":"task","state":"running","payload":{"task":{"type":"message","content":"走两步"}}}}
```

`task_type="agent"` 的到期任务还会推送 `tool_call` / `tool_result` / `message_start` / `message_done` 等事件，与 `/ws/chat` 一致。

## 6. 工具

Agent 侧最多注册以下 function tools（按 `TOOL_POLICY_ORDER` 排序，实际是否加载取决于配置和 `enable_memory`）：

| 工具 | 加载条件 | 说明 |
| --- | --- | --- |
| `run_command` | 始终加载 | 在 workspace 内执行 shell；不传 `working_directory` 时默认在 workspace 目录运行。默认超时 `30` 秒，上限 `300` 秒，stdout/stderr 各限 `50KB`。 |
| `manage_scheduled_tasks` | 始终加载 | 创建/列出/删除调度任务，`action` 取 `create`/`list`/`delete`，`task_type` 取 `message`/`agent`，支持 `delay_seconds` 或结构化 `recurrence`。 |
| `write_memory` | `enable_memory=true` | 写入长期记忆（追加到当日 daily 文件）。 |
| `search_memory` | `enable_memory=true` | 按关键词、日期或日期范围检索长期记忆，参数含 `query`、`date`、`scope`（`daily`/`weekly`/`monthly`/`yearly`/`all`）、`context_lines`（0–10）。 |
| `web_search` | `search.provider != none` | 联网搜索；参数随 provider 不同。 |
| `generate_image` | `image_generation.provider != none` | 生成图片，文件写入 `workspace/temp/images/`，返回 attachment 元数据。 |
| `attach_artifact` | 始终加载 | 把已存在的 workspace 文件登记为结构化附件发送给用户；参数为 workspace 相对路径 / blob URL / workspace 内绝对路径，可选 caption。 |
| `read_skill` | 存在已启用 skill 时 | 加载 `SKILL.md` 或 skill 包内引用文件。 |

模型被明确要求不要用 Markdown 图片语法 `![alt](url)` 输出图片；用户可见的图片或文件一律通过 `generate_image` 或 `attach_artifact` 以结构化附件交付。

## 7. 消息、记忆与隐私行为

### 7.1 短期消息流

默认消息存储在 SQLite：

```text
~/.xagent/messages/messages.sqlite3
```

存储的是一个全局有序消息流，包括：

- 用户消息：`role=user`，`sender_id` 是请求里的 `user_id`。
- Agent 回复：`role=assistant`，`sender_id=agent`。
- 观察事件：`role=environment`，`type=context_event`。

工具调用消息只在模型循环中临时使用，不作为普通消息长期写入。每轮注入模型的历史最多 `30` 条。

### 7.2 长期记忆

长期记忆是 markdown 文件：

```text
memory/
  daily/<year>/<year-month>/<date>.md          # daily/2026/2026-05/2026-05-11.md
  weekly/<year>/<week_start>_to_<week_end>.md   # weekly/2026/2026-05-05_to_2026-05-11.md
  monthly/<year>/<year-month>.md                # monthly/2026/2026-05.md
  yearly/<year>.md                              # yearly/2026.md
```

每轮对话会自动注入最近 `2` 天的 daily 记忆作为上下文。`enable_memory` 影响记忆行为如下：

| 参数 | 读取记忆 | 写入记忆 | 写入主消息库 |
| --- | --- | --- | --- |
| `enable_memory=true` | 是 | 是 | 是 |
| `enable_memory=false` | 否 | 否 | 是 |

后台写入按 `20` 条消息 + `600` 秒最小间隔、或 `900` 秒兜底策略整理。退出时不会强制 flush；短对话至少保存在 `messages/` 中，后续由批量写入、idle fallback、长驻 runtime heartbeat 或显式 `write_memory` 工具整理进长期记忆。长驻 API/Feishu 运行时通过 heartbeat（默认每 `300` 秒）执行相同维护，并在每周一生成上一周 weekly summary。

`/observe` 总是写入主消息流；是否值得进入长期日记由事件内容、`event_type` 和 `metadata.memory_policy` 等元数据决定。常见值为 `memory_policy: "never"`、`"auto"` 或 `"always"`。

## 8. 飞书机器人接入建议

建议不要让飞书直接访问 xAgent，而是增加一个“飞书适配服务”，或直接使用内置 `channels.feishu` 适配器（`xagent service start feishu`）。如果自建适配层：

```text
Feishu Event/Webhook
  -> Adapter Service
     -> 校验飞书签名、tenant、事件类型、权限
     -> 映射 user_id / chat_id / message_id
     -> 调用 xAgent HTTP 或 WebSocket
     -> 调用飞书发送消息 API 回复
```

### 8.1 私聊或 @ 机器人

使用 `/chat`：

```json
{
  "user_id": "feishu_open_id_xxx",
  "user_message": "帮我写一版周报",
  "enable_memory": true
}
```

飞书字段映射建议：

- `user_id`：优先使用稳定用户 ID，如 `union_id` 或内部账号 ID。
- `user_message`：飞书消息纯文本内容；富文本需先转成可读文本。
- `image_source`：消息里有图片时，优先传 `/api/workspace/blob?path=...`、公网 URL 或 data URI。xAgent 会把 data URI 和本地文件归一化保存到 workspace，并在模型调用边界按需转成 provider 可接受的图片输入。
- `attachments`：平台消息里有文件时，先保存到 `workspace/`，再传 workspace attachment metadata，方便 Web UI、Feishu 和其他 IM 共享同一套 blob URL 展示、下载和转发逻辑。

### 8.2 群聊中未 @ 机器人

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

如果响应里 `replied` 为 `false`，适配服务不需要发送飞书消息；为 `true` 则把 `reply` 发回群聊。

### 8.3 分段回复与内置 Feishu adapter

外部实时入口应使用 `/ws/chat` 或进程内 `Agent.chat_events()`。`stream` 只控制是否把文本拆成 `message_delta`；分段边界、工具调用和最终 `done` 始终存在。

内置 Feishu adapter 也走 `Agent.chat_events()`：每个 `message_done` 默认发送一条 markdown 消息；`channels.feishu.stream: true` 时使用 Feishu streaming card 增量更新当前段。

如果飞书消息包含图片或文件，内置 Feishu adapter 会通过官方 message resource API 下载资源。图片保存到 `workspace/temp/images/feishu/`，非图片保存到 `workspace/temp/attachments/feishu/`，二者都会以 workspace attachment metadata 传入 Agent。大图会先按通道预算压缩：保留宽高比例、应用 EXIF 方向、去除元数据、最长边收敛到 `2048px`、目标大小 `8MB` 以内（JPEG 质量 `85`，必要时降到 `55`），并以 JPEG 派生图进入模型或 Feishu 原生上传；原始 workspace 文件不会因出站发送被覆盖。模型调用边界只会为当前 user turn 从 workspace 读取图片并转成 provider 可接受的图片输入；非图片文件保持为引用和清单。当当前 provider 不支持 vision 时，图片不会调用模型，会直接回复无法理解图片内容，并把已保存图片作为飞书原生附件返回。Agent 返回的结构化 workspace attachments 会被解析为本地 workspace 文件，并通过 `FeishuChannel.send({"image": ...})` 或 `FeishuChannel.send({"file": ...})` 发送。

### 8.4 多用户与记忆边界

xAgent 会把 `user_id` 作为当前说话人的身份注入上下文。接入外部系统时必须保持 ID 稳定，否则长期记忆和多用户归因会变差。

建议：

- 私聊：`user_id = union_id` 或内部用户 ID。
- 群聊：@ 机器人时走 `/chat`，`user_id` 使用当前发言人的稳定 ID；未 @ 的环境消息走 `/observe`，真实发言人放入 `metadata.speaker_id`，并在 `context` 中保留可读归因。
- 跨平台：同一个真实用户最好映射到同一个内部 ID。
- 敏感会话：使用 `enable_memory: false` 可避免读取和写入长期记忆；当前不提供临时私密消息流。

## 9. 最小客户端示例

### 9.1 Python HTTP

```python
import httpx


def chat(message: str, user_id: str = "alice") -> str:
    response = httpx.post(
        "http://127.0.0.1:8010/chat",
        json={
            "user_id": user_id,
            "user_message": message,
        },
        timeout=620,
    )
    response.raise_for_status()
    return response.json()["reply"]
```

### 9.2 JavaScript WebSocket

```javascript
const ws = new WebSocket("ws://127.0.0.1:8010/ws/chat");

ws.addEventListener("open", () => {
  ws.send(JSON.stringify({
    user_id: "alice",
    user_message: "写一句欢迎语",
    stream: true
  }));
});

let currentText = "";
ws.addEventListener("message", (event) => {
  const frame = JSON.parse(event.data);
  if (frame.type === "message_start") {
    currentText = "";
  } else if (frame.type === "message_delta") {
    currentText += frame.delta;
  } else if (frame.type === "message_done") {
    currentText = frame.content || currentText;
    console.log(frame.phase, currentText);
  } else if (frame.type === "error") {
    console.error(frame.error, frame.details || "");
  } else if (frame.type === "done") {
    console.log("turn complete");
  }
});
```

## 10. 对外部署检查清单

- 不要把未加鉴权的 xAgent 端口直接暴露到公网。
- 外部适配服务负责平台签名校验，例如飞书的 challenge、timestamp、nonce、signature。
- 为每个平台用户生成稳定 `user_id`。
- 群聊、传感器、通知类事件优先用 `/observe`；直接提问才用 `/chat`。
- 图片应优先传 workspace blob URL、公网可访问 URL、临时下载 URL 或 data URI；本地文件路径只对 xAgent 服务进程所在机器有效。
- 根据业务设置 `--max-concurrent-chats`、`--queue-timeout`、`--chat-timeout`。
- 对 `POST /api/memory/clear`、`POST /clear_messages`、`POST /api/workspace/clear`、`PUT /api/agent/identity` 做额外权限控制。
- 对飞书等平台回复做长度截断、频率控制和失败重试。
