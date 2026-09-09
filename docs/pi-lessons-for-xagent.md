# PI 框架对 xAgent 的建议

对照对象：

- PI：[`badlogic/pi-mono`](https://github.com/badlogic/pi-mono)（阅读时的主包是 `@earendil-works/pi-ai`、`pi-agent-core`、`pi-coding-agent`）
- xAgent：本仓库当前主线，核心在 `xagent/core/`

本文只谈**从 PI 的代码形状里，xAgent 该吸收什么、不该吸收什么**。不建议换语言，不建议把 xAgent 做成 coding agent。

## 先定性：两个项目不是同一类东西

PI 自己的定位是 *minimal terminal coding harness*。默认四件工具（`read` / `write` / `edit` / `bash`），会话是一次任务的 JSONL 树，核心刻意不做子 agent、plan mode、权限弹窗、MCP。扩展点极多，是为了「不要 fork 内核就能改工作流」。

xAgent 的定位在 `GOAL.md`：独立数字个体。默认能力是身份、第一人称日记、多用户、1:1 与群、环境观察、跨渠道连续存在。会话不是一次 coding task，而是一条 agent 级生命流。

所以：

- **不要学**：四工具默认集、`/tree` `/fork` 任务分叉、把日记换成 session JSONL、把渠道改成 TUI 插件、整仓迁到 TypeScript。
- **要学**：PI 把「能跑的 loop」和「产品假设」切开的方式。xAgent 现在把两者焊在同一个 `Agent` 里。

PI 的分层是真的：

```text
pi-ai            统一 LLM：catalog / auth / stream / usage / 跨 provider 转写
pi-agent-core    无产品假设的 loop：事件、工具、steer/follow-up、abort、convertToLlm
pi-coding-agent  产品：TUI、session 树、extensions、skills、compaction UI
```

xAgent 当前是一个 Python 包里的 `Agent` 同时拥有：provider 协议、turn lock、prompt 拼装、日记/关系/笔记注入、工具执行、观察事件、群参与决策。`Agent.__init__` 已经在绑定 `search_memory` / note 工具 / `see_image`；`chat_events` 既是 loop 也是产品编排。

这不是「写得乱」，是**缺少一层 harness**。后面每条建议都围绕这件事。

## 1. 把 loop 收成 harness，产品挂在 hook 上

PI 的 `agentLoop`（`packages/agent/src/agent-loop.ts`）只做这几件事：

1. 发 `agent_start` / `turn_start`
2. `transformContext` → `convertToLlm` → 调 LLM
3. 执行工具（可并行），把 `toolResult` 写回 transcript
4. 回合结束后看 steer / follow-up 队列
5. abort 时带 `stopReason: "aborted"` 退出，工具侧也能看见 `AbortSignal`

`Agent` 类再包一层：状态、队列、`beforeToolCall` / `afterToolCall` / `shouldStopAfterTurn`。coding-agent 的日记式功能、compaction、skills **不在这个文件里**。

xAgent 的 `_drive_claimed_turn`（`xagent/core/agent.py`）在同一段里做：存用户消息、拼日记/关系/笔记、循环模型、存 preface、跑工具、存最终回复、调度日记写入。潜意识循环（`xagent/core/runtime/subconscious.py` 的 `_generate_subconscious_thought`）又把这套 loop **复制了一遍**，而且明确丢掉工具调用。

建议：

- 抽出一个只认识「消息 + 工具 + 事件」的 `AgentLoop`。它不应该知道日记、飞书、潜意识。
- 产品通过 hook 进入：`before_turn`（注入 identity / diary / relationship）、`after_turn`（写日记）、`before_tool` / `after_tool`。
- 潜意识、定时任务、群参与决策都变成对同一个 loop 的不同 `inbox_kind` + 不同 prompt kind，而不是第二套 `for range(max_agent_loops)`。

`PromptRegistry` 已经是对的方向（instructions / turn / decision 三套 kind）。缺的是 **turn 生命周期 hook**，不只是拼 system 段。

PI 的 harness hook 名字可以直接借，不必借它的 TypeScript 运行时：

| hook | PI 用来做什么 | xAgent 应对应什么 |
|---|---|---|
| `transform_context` | 压缩、注入、过滤 UI-only 消息 | 热窗口 + working summary + 渠道说明 |
| `before_tool` / `after_tool` | 拦截、改结果、`terminate` | 现有 `ToolGuard`，但要能 ASK 真提问，而不是改成 DENY |
| `before_run` | 注入额外 user 消息 | 观察事件、定时任务包装、steer |
| `shouldStopAfterTurn` | 工具声明「到此结束，不要再调模型」 | 图像/附件工具出站后提前收口（你们已经在 `handle_tool_calls` 里 hard-return） |
| `prepareNextTurn` | 回合之间做 compaction | 现在只在 turn 开头 `ensure_fresh`，长工具链中间不会压 |

## 2. 分清三种 transcript，不要都挤成 OpenAI dict

PI 最重要的边界不是「用 TypeScript」，而是：

```text
AgentMessage[]  --transformContext-->  AgentMessage[]  --convertToLlm-->  Message[]  --> LLM
     ↑                                      ↑
  可含 custom / bashExecution /          只含 user / assistant / toolResult
  compactionSummary / 通知
```

LLM 永远只看见三种 role。UI、session 树、扩展自定义条目活在 `AgentMessage` 上，靠 `convertToLlm` 丢掉或转写。

xAgent 现在有三套互相泄漏的格式：

1. **生命流**：`schemas.Message` + SQLite（user / assistant / environment observation）。这是对的，符合 GOAL.md。
2. **prompt 段**：`PromptRegistry` 产出 `{role, name, content}`。
3. **协议 dict**：`input_messages` 里直接是 Chat Completions / Responses / Anthropic 形状；`ToolExecutor` 再往里面 `append` assistant `tool_calls` 和 `role: tool`。

工具回合 **不进 SQLite**。用户能在 UI 看到的历史，和模型下一轮真正看到的 `iteration_messages`，不是同一条带。working context 只好另存 `.working_context.json`。`sanitize_input_messages` 只丢掉**开头**的孤儿 `tool` / `function_call_output`，窗口中间切断的 tool 对它不管。

建议：

- SQLite 继续只存生命流（谁说了什么、观察、渠道）。不要把 function_call JSON 写进日记。
- 每个 waking turn 另有一份 **loop transcript**（内存即可，必要时落盘）：assistant 文本、tool call、tool result、preface、abort 半截。这才是 `convertToLlm` 的输入。
- `convertToLlm` 按当前 `model_api` 转协议。今天这份工作散落在 `ModelClient._build_*` 和 `ToolExecutor._to_chat_tool_call` / `_to_responses_tool_result`，约 1600 行协议分支。
- observation、关系卡、笔记索引走 custom message 或 named prompt section，**转换时再决定是否给模型**。潜意识不该和聊天共用同一串 OpenAI dict 再靠 `task_mode == "subconscious_json"` 抠掉段落。

这和 GOAL.md 的「日记是记忆载体、原始对话是归因」是对齐的：生命流归日记/SQLite，loop transcript 归 harness，二者不要互相冒充。

## 3. 把已经声明的 STEER 做完，并分开 follow-up

`InboxKind.STEER` 已经在 `xagent/core/inbox.py` 里，而且 `wakes=True`。全仓库没有第二处使用它。`AgentInbox` 实际只是一把 turn lock + abort Event。

PI 把队列分成两种，语义非常清楚：

- **steer**：人在工具还在跑时插话。当前这批工具会跑完，然后把 steer 消息注入，再进入下一轮 LLM。不会把半截 tool 结果丢掉。
- **follow-up**：agent 本会停下来时，再追加一句。用于「做完这个之后再总结」。

xAgent 现在的 `abort()` 文档写得很诚实：停在「下一次模型调用或工具批次边界」，**不回滚已跑工具，也不杀正在跑的 shell**。Web 点停止时，长 `run_command` 会继续跑完。群里用户插话，只能等整轮结束，因为 `acquire_turn` 是互斥的。

建议：

- `abort` 继续存在，但是走 `AbortSignal` 传到 `model_turn_events` 和 `run_command`。
- 另做 `steer(item)`：不取消当前工具，只在 `turn_end` 后注入。这才是多渠道产品该有的「我还想补一句」。
- 定时任务、潜意识外发走 follow-up 或独立 `inbox_kind`，不要和真人插话抢同一把锁却没有队列。
- 观察（`InboxKind.OBSERVATION`）保持不唤醒。这点 xAgent 已经比 PI 更贴近 GOAL.md，不要改掉。

## 4. Abort 必须穿过模型和工具，而不是只看循环边界

PI 的 `executeToolCalls` 把同一个 `AbortSignal` 传进 tool `execute(..., signal)`。abort 后工具结果是 `isError` + `"Operation aborted"`，assistant 半截消息留在 transcript，可以 `continue()`。

xAgent：

- `abort()` 只 `set` 一个 Event。
- 检查点在：下一轮 `model_turn_events` 之前、拿到 tool_calls 之后、工具批次之后。
- `model_turn_events` 本身没有 cancellation。
- `_run_shell_command` 只有 timeout，没有外部 cancel。

建议最小改动：

1. `AgentInbox` 持有 `asyncio.Event` 的同时，给本轮一个 `asyncio.CancelledError` / `AbortController` 等价物。
2. `ModelClient` 把 httpx/SDK 的 timeout+close 接到它。
3. `run_command` 对 `asyncio.create_subprocess_exec` 在 abort 时 `terminate()`。
4. 被打断的 assistant 文本仍写入 SQLite（标注 `turn_phase: aborted`），下一轮看得到。不要像现在一样直接 `return` 丢半截。

## 5. Compaction 按 token 切，且不要切断 tool 对

PI compaction 的规则（`packages/agent/src/harness/compaction/compaction.ts` + coding-agent 文档）值得直接搬语义：

- 触发：`contextTokens > contextWindow - reserveTokens`（默认 reserve 16k），用的是 **provider 返回的 usage**，不是消息条数。
- 保留：从新到旧累加到 `keepRecentTokens`（默认 20k）。
- 切口只允许落在 user / assistant；**绝不落在 tool result**（tool result 必须跟着它的 tool call）。
- 单轮大到超过保留预算时承认 split turn，前半段单独摘要，而不是静默丢掉。
- 全文仍在 session 文件里；摘要只影响下次送给 LLM 的窗口。
- 工具链中间也会检查，不只在用户下一条之前。
- 摘要请求用新的 `sessionId` 且 `cacheRetention: "none"`，避免污染 prompt cache。

xAgent 现在：

- 热窗口是 **12 条消息**（`DEFAULT_RECENT_MESSAGES`）。
- working context 按 storage cursor 滚动摘要，阈值是 `hot_window + slack` 条。
- 摘要上限 1500 字符 / 2048 tokens，和真实 context window 无关。
- `ModelClient` 为了 Langfuse 开了 `include_usage`，但 loop / compactor **不用这些数字做预算**。
- 长工具链在 `max_agent_loops=50` 内反复 `sanitize_input_messages`，上下文只增不按 token 压。

建议：

- 日记继续按「天」注入（那是记忆，不是窗口管理）。
- working context 改成 token 预算：用上次 assistant `usage.input+cacheRead`（或估算）相对模型 `contextWindow`。
- 切口规则抄 PI：保留最近完整 turn，切开时带上未完成的 tool 对。
- 在工具批次结束后、下一轮 LLM 前调用 `shouldCompact`，不要只在 `chat_events` 开头 `view_for_turn`。
- 12 这条数可以留作下限，但不能当唯一预算。多图、长 shell 输出、笔记索引会把 12 条撑爆窗口，条数却还很「短」。

不要把 PI 的 session 树搬过来。xAgent 需要的是**同一条生命流上的窗口压缩**，不是 `/fork` 出另一段任务史。GOAL.md 要的是连续时间线。

## 6. 模型层做成 catalog，而不是 1600 行协议 if

`pi-ai` 把「哪个模型」做成数据：`contextWindow`、`maxTokens`、`input: ['text','image']`、`reasoning`、`cost`、`compat`（developer role、reasoning_effort 字段名、thinking 格式）。stream 统一成 `text_delta` / `thinking_delta` / `toolcall_*` / `done` / `error`。换模型是换一份 metadata，不是改 loop。

它还处理了 xAgent 已经疼过的事：

- 同一段对话换 provider：把别家的 thinking block 转成 `<thinking>` 文本。
- OAuth token 刷新进 `getApiKey`，每轮解析。
- `sessionId` → prompt cache / session affinity。
- `strict` tool schema、截断 JSON 的 tool 参数校验。
- 输出 `stopReason: "length" | "toolUse" | "aborted" | "error"`。

xAgent 的 `ModelClient`（1633 行）按 `openai_responses` / `openai_chat_completions` / `anthropic_messages` 手写三套 build/parse。`providers.py` 用硬编码表决定默认 API 和「这个 DeepSeek 模型能不能看图」。没有 contextWindow，没有 per-model cost，没有把 usage 喂回 compaction。thinking 被塞进 `ChatToolCall.reasoning_content`，UI 事件只有 `delta` / `text` / `tool_calls` / `error`。

建议按优先级：

1. 给每个配置里的模型补一份 catalog 行：`context_window`、`supports_vision`、`reasoning`、`api`。从现有 `VISION_MODEL_OVERRIDES` 长出来。
2. `ModelStreamEvent` 增加 `thinking_delta`、`usage`、`stop_reason`。loop 用 `stop_reason == "length"` 拒绝执行本批工具（PI 在 `failToolCallsFromTruncatedMessage` 里写得很明确：截断参数可能 JSON 仍能 parse，执行会是错的）。
3. 每次请求带上稳定 `session_id`（按 agent + channel + room，不要按「这一轮」）。OpenAI / Anthropic 的 cache 才有意义；你们现在每轮从 SQLite 重拼 messages，cache 命中天生差。
4. 不要再为每个新厂商复制 400 行 `_build_*_params`。新厂商如果是 OpenAI-compatible，应该只填 `compat` 而不是新分支。

## 7. 工具：schema、进度、终止、ASK 要成真

PI 的 `AgentTool.execute(toolCallId, params, signal, onUpdate)` 能流式汇报；整个批次若都 `terminate: true`，就不再自动跟一轮 LLM。截断的 tool call 全部失败回模型。`beforeToolCall` 可以 block。

xAgent 已有不错的雏形：`ToolGuard.pre_execute` / `post_execute`、并发 semaphore、`WorkspaceShellGuard`。缺口：

- `ToolDecision.ASK` 被 executor 翻译成 `"approval required: ..."` 然后当错误返回模型。没有 UI、没有飞书确认、没有真正暂停。
- 工具没有 `signal`，没有 `onUpdate`。Web/渠道只能等 `tool_result` 整包。
- 图像附件工具靠在 executor 里 hard-return 结束 turn。这就是 PI 的 `terminate`，应该变成工具返回值，而不是 `if is_generated_image_result` 特判。
- 工具 schema 来自 `function_tool` 对 typing 的猜测，没有校验调用参数；坏 JSON 只是字符串错误。
- `Agent.__init__` 把记忆/笔记/看图工具焊死。搜索、生图、fetch 更像包，不像个体本能。

建议：

- 核心工具保持小：`run_command`、记忆读写、笔记、技能读取。搜索/生图/fetch 用和渠道一样的注册表挂上去。
- `execute_single` 增加 `signal` 和可选 progress callback，事件流已有 `tool_call` / `tool_result`，加一个 `tool_update` 即可。
- ASK 要么做成真的 steer 点（发到当前渠道问一句，等下一条用户消息），要么先删掉这个 enum，避免假装有审批。
- 工具参数用显式 JSON schema + 执行前 validate；失败当 `isError` tool result，让模型改参数，不要当 turn-level error。

## 8. Skills：对齐 Agent Skills 标准，但不要变成 coding 技能市场

两边都是「目录里的 `SKILL.md` + catalog 注入 + 按需读取」。PI 额外做了：

- `disable-model-invocation`：只许人用 `/skill:name` 唤起，模型不能自己读。
- 多来源（user / project / package）和 ignore 文件。
- 技能正文是能力说明书，模型用 `read` 去打开，不把全文塞进 system prompt。

xAgent 的 `catalog_text(max_chars=8000)` 已经在控制注入体积。建议补：

- 尊重 Agent Skills 的 frontmatter（`name` / `description` / `disable-model-invocation`）。
- catalog 只放 name+description；正文继续 `read_skill`。
- 不要为了学 PI 去做 npm 技能包市场。xAgent 的技能是这个个体会的事，不是仓库插件生态。

## 9. 扩展面：学 hook，不学 Doom 插件

`PromptRegistry.section()` 已经返回 dispose，说明你们想过可插拔 prompt。PI 的 `ExtensionAPI` 则覆盖工具、命令、快捷键、provider、自定义消息渲染、`sendMessage(deliverAs: steer|followUp)`。那是 coding CLI 的产品面。

xAgent 需要的扩展面小得多，而且必须服从 GOAL.md：

**值得做成稳定 hook 的**

- prompt section（已有）
- tools
- channels（飞书/微信/web/voice 已经是 adapter，但没有统一 `submit(InboxItem)` 以外的生命周期）
- memory writers（日记 / 关系卡 / 笔记蒸馏）——它们是日记的投影，必须能关掉
- model providers

**不要做成扩展的**

- 身份（`identity.md`）
- 日记作为唯一记忆载体
- 多用户归因
- 群参与决策的语义（实现可以 hook，原则不行）

一个 Python 模块 `register(agent)` 就够。不必 jiti、不必 npm `pi-package`。渠道已经证明 adapter 模式能用；缺的是 loop 对它们暴露和 PI 同级的事件，而不是让飞书去 fork `Agent.chat_events`。

## 10. 明确不要从 PI 搬来的东西

这些在 PI 里是优点，搬到 xAgent 会伤产品：

| PI 特性 | 为什么不要 |
|---|---|
| 任务 session 树 + `/tree` `/fork` | 个体记忆必须是一条时间线。分叉是 coding 的「我试试另一条改法」，不是人生。 |
| 默认四工具、无记忆 | xAgent 没有日记就不是 xAgent。 |
| 无权限系统、建议用容器 | 你们已经有 workspace cwd 守卫；多用户渠道上还需要归因，不是把整机交给模型。 |
| 无内置子 agent | 同意，不要学别人去加。潜意识已经是第二条嘴，先统一再谈子 agent。 |
| 把一切做成可热加载 TS 扩展 | 身份和日记若可被扩展改写，GOAL.md 的主体性就没了。 |
| RPC/JSONL CLI 模式当主 API | 你们已有 Web/渠道事件。优先把现有 `chat_events` 做成稳定契约。 |
| prompt cache 优化优先于日记 | cache 是性能；日记是产品。先正确再 cache。 |

## 建议落地顺序（只改 Python 内核）

按侵入性从低到高。每一项都可以单独 PR，不必「先重写 Agent」。

1. **`stop_reason` + 截断 tool 拒绝执行。** 改 `ModelClient` 和 `ToolExecutor`，行为修复，不动产品。
2. **abort 传到 subprocess 和 HTTP 请求。** 补齐 `abort()` 自己写在 docstring 里却没做的事。
3. **实现 `InboxKind.STEER` 队列。** 工具跑完再注入；观察仍然不唤醒。这是多渠道产品立刻能感到的差别。
4. **usage 驱动 working context。** catalog 里写 `context_window`，compaction 用 token 而不是 12 条。切点避开孤立 tool result。
5. **loop transcript 与生命流分离。** `convertToLlm` 成为唯一协议出口。潜意识改走同一 loop + `KIND` 不同。
6. **把 `shouldStopAfterTurn` / `before_tool` 做成 hook。** 删掉 executor 里对生图/附件的类型特判。
7. **模型 catalog + session_id。** 新 provider 只加数据。ASK 要么做真审批，要么删。

不要并行做「插件市场」或「Node 内核」。那些不增强 GOAL.md。

## GOAL.md 检查

- **Identity：** 把 identity 留在核心 prompt section，不做成可替换扩展。拆 loop 是为了别的功能别改 identity。
- **Multi-user：** steer/follow-up 必须带着 `user_id` / `channel` / `room_name`。队列不能把群消息变成无主插话。
- **1:1 and group：** 群插话是 STEER 的主场景；不要用 abort 冒充插话。
- **Memory/journal：** 日记仍是唯一长期载体。loop transcript 不是记忆。working context 仍是可丢弃摘要。
- **Unified memory：** compaction 按时间切窗口，不按 user 分库。
- **Agent-governed sharing：** hook 可以注入关系卡，但不能改成「按用户检索记忆」。
- **Diary-anchored carrier：** 不要为了学 PI session JSONL 再引入第二套真相源。
- **Attribution and continuity：** abort 半截、steer、观察，都要能在 SQLite 里追到是谁、在哪个渠道、哪种 inbox_kind。

## 一句话

PI 值得学的不是「最小 coding agent」，而是：**一个不懂日记的 loop，外加一层很硬的 convertToLlm 边界，外加 steer/abort/compaction 这些窗口力学。** xAgent 已经有身份、日记、多用户、观察；缺的是把这些从 `Agent.chat_events` 里拆出去，让它们挂在一个更小、可中断、按 token 记账的 harness 上。
