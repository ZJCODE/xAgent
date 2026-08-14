# xAgent Node.js 重写探索

日期：2026-08-14  
对照：DeepSeek Harness (`dsh`) v0.1 developer preview，仓库 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)  
本分支只做判断和设计，不改运行时代码。

## 结论

**能用 Node.js 重写，但不值得现在整仓重写。更不值得把 xAgent 迁到 DeepSeek Harness 上。**

- 语言层面：xAgent 没有必须绑死 Python 的算法。消息、日记、调度、HTTP、渠道适配都可以用 TypeScript 重做。
- 产品层面：xAgent 是「持续存在的数字个体」；`dsh` 是「插件化的 coding-agent 运行时」。两者看起来都叫 harness，目标不同。
- 成本层面：当前 Python 运行时约 3.9 万行业务代码（`xagent/`），测试约 2.2 万行，前端已经是 TypeScript。整仓 Node 重写等于再做一遍飞书 / 微信 / 语音 / 潜意识 / 日记，回归风险集中在这些渠道，而不是 agent loop。
- 时机层面：`dsh` 仍是 developer preview，官方明确会有兼容性破坏。把它当底座等于把产品绑在未稳定的第三方内核上。

推荐：继续用 Python 做产品内核。前端已经是 Node 栈，不必为「统一语言」再搬一次后端。若要吸收 `dsh` 的优点，学它的插件缝（LLM / tools / session events），而不是换运行时。

---

## 两个项目不是同一类东西

| | xAgent | DeepSeek Harness |
|---|---|---|
| 产品定义 | 独立数字个体：身份、第一人称日记、多用户、跨渠道连续存在 | Agent runtime：模型、工具、sandbox、loop、UI 全是插件 |
| 默认交互 | 终端 / Web / 语音 / 飞书 / 微信，长期陪伴 | Web UI / headless 编程任务 |
| 记忆 | 统一日记流 + 关系备忘，agent 自己决定分享边界 | Session log 是模型可见历史的源；偏任务会话 |
| 主动性 | 潜意识循环、联系人、定时任务、心跳 | jobs / goals / 子 agent，服务一次任务 |
| 扩展方式 | Python 模块 + 渠道 adapter | Cordis 插件树：profile / bundle / patch |
| 分发 | `pip` / `uv` / `install.sh`，数据在 `~/.xagent/` | `npx @deepseek-ai/dsh`，profile 在 harness home |
| 成熟度 | 已有用户数据、渠道和 launcher | v0.1-rc，breaking changes 是官方承诺 |

`dsh` 的核心句子是 *everything is a plugin*。xAgent 的核心句子在 `GOAL.md`：agent 是主体，不是单用户工具壳。把后者塞进前者，会先丢掉身份 / 日记 / 多用户 / 飞书群参与这些产品约束。

---

## 当前代码规模（本仓库）

粗算（含空行和注释）：

| 区域 | 约行数 | 说明 |
|---|---|---|
| `xagent/core` | 9.5k | agent loop、model client、working context、journal、subconscious、scheduler |
| `xagent/interfaces` | 16k | launcher / CLI / FastAPI / voice；launcher 和 setup 就约 4.2k |
| `xagent/integrations` | 7.4k | 飞书 adapter 单文件 3.2k；微信约 0.9k |
| `xagent/tools` | 3.1k | shell / search / fetch / memory / skills / image / scheduler |
| `xagent/components` | 2.1k | markdown 日记、关系、SQLite 消息、skills |
| 测试 | ~22k / 47 个文件 | 渠道、记忆、语音、调度都有覆盖 |
| `frontend/` | ~8.9k TS | 已经是 Node；重写后端带不走这部分收益 |

重写真正贵的不是 `Agent.chat()`，而是：

1. 飞书：群/单聊路由、@、历史、名片、媒体、参与决策。
2. 微信 iLink：凭证、媒体、二维码会话、过期重登。
3. 本地语音：`sounddevice` 半双工、Soniox STT、TTS 播放、麦克风冷却。
4. 日记与 working context：第一人称写作、滚动摘要、关系文件。
5. 潜意识与文件调度：联系人、主动外发、任务落盘。

这些都是产品差异，不是「换成 TypeScript 更好写」的部分。

---

## 技术可行性：按子系统

结论：**全部可移植，但难度不均匀。**

| 子系统 | Node 替代 | 难度 | 备注 |
|---|---|---|---|
| LLM 调用（OpenAI Responses / Chat / Anthropic） | 官方 JS SDK | 低 | `dsh` 已经有 adapter 缝，但 xAgent 自己写也够 |
| Agent loop / tool 并行 | `async` + 自研或 Cordis | 中 | 逻辑可搬；事件模型和 prompt 分层要重做 |
| SQLite 消息 | `better-sqlite3` | 低 | 数据格式要兼容 `~/.xagent/agents/*/messages/` |
| Markdown 日记 / 关系 | `fs` + 文件锁 | 低 | 产品语义在 prompt，不在语言 |
| FastAPI HTTP / WS | Fastify / Hono / ws | 低 | 前端 API 契约可保持 |
| Web UI | 已有 Vite + React | 无 | 不用动 |
| CLI launcher（rich / readchar） | Ink / 简单 TUI | 中 | 交互向导行数多，属于重写税 |
| Skills（本地 md + catalog） | 文件扫描 | 低 | 和 `dsh` skills 形似但存储布局不同 |
| Shell / workspace | `child_process` | 低 | `dsh` 的 sandbox 更强，xAgent 当前是本机 workspace |
| Web search / fetch | 各搜索 API + Readability | 中 | `trafilatura` 抽取质量要重新验收 |
| 图像生成 / 压缩 | sharp + 各厂商 SDK | 中 | Pillow 路径和飞书/微信出站压缩要回归 |
| 文件调度器 | `fs` + 锁 | 中 | POSIX `fcntl` 可换成 `proper-lockfile` |
| Langfuse | `@langfuse/openai` 等 | 低 | |
| 飞书 | `@larksuiteoapi/node-sdk` | **高** | 官方有 Node SDK，但现有 adapter 行为密度高 |
| 微信 iLink | 无对等官方 Node 库 | **高** | 等于重写协议客户端 |
| 语音（mic / STT / TTS） | 原生绑定或 sidecar | **高** | `sounddevice` + Soniox Python SDK 是最不划算的端口 |

「能写」不等于「值得写」。高难度三项（飞书、微信、语音）正好是现在用户能感知的渠道，不是内部重构可以慢慢磨的部分。

---

## 为什么不值得整仓重写

1. **收益对不齐产品目标。** Node 的好处是 npm 插件生态、和 `dsh` 同语言、和前端同栈。xAgent 的护城河是身份、日记、多用户、跨渠道连续生命。换语言不增强这些。
2. **会停掉产品迭代。** 按现有渠道深度，做到行为接近需要把飞书 / 微信 / 语音 / 日记 / 潜意识全部再测一遍。这期间 Python 主线基本只能冻结。
3. **已有本地数据。** `~/.xagent/` 是产品合同。Node 重写必须字节级兼容 SQLite、日记目录、tasks、contacts，否则等于换产品。
4. **分发模型会断一次。** 现在是 `pip install myxagent` / `uv tool`。改 npm 后，现有安装脚本、更新命令、Windows/mac 音频依赖都要重做。
5. **Python 在这条产品线上并不吃亏。** 本地文件、asyncio、官方飞书 SDK、音频采集、HTML 抽取，Python 都够用。xAgent 不是高并发网关。
6. **前端已经是 TypeScript。** 「统一成一种语言」只对后端开发者手感有意义，对用户和 GOAL.md 没有意义。

---

## 为什么也不该「迁到 dsh 上」

把 xAgent 做成一组 `dsh-plugin`，表面上能少写一个 loop。实际上要硬塞进去的是 `dsh` 没有一等建模的东西：

| xAgent 能力 | 在 `dsh` 里大致对应 | 缺口 |
|---|---|---|
| 统一日记记忆 | session log / inject | session 是任务会话，不是第一人称人生时间线 |
| 多用户身份边界 | 无 | 需要自研 user / room / channel 模型 |
| 群参与决策 | `agent/pre-step` 可拦截 | 决策 prompt 和飞书路由仍要自写 |
| 潜意识主动外发 | `ctx.jobs` | jobs 不是「对联系人发起生活向消息」 |
| 飞书 / 微信 | 无官方渠道插件 | 全部自研，且要挂在 Cordis 生命周期上 |
| 本地语音半双工 | 无 | 同上 |
| `~/.xagent` 数据布局 | harness home / session store | 不兼容，要写迁移或双写 |

还要接受：

- `dsh` 目前不收外部 PR，扩展路径是插件，但 **core API 会破**。
- 默认心智是 coding agent（sandbox、approval、PTY）。xAgent 默认心智是长期主体。
- 依赖 Cordis 等于把产品内核外包给仍在 rc 的框架。

`dsh` 值得学的是缝怎么切：`ctx.llm`、`ctx.tools`、`ctx.sessions`、waterfall 事件、profile/bundle 组装。不值得把 xAgent 变成它的一个 profile。

---

## 可选路径（按推荐顺序）

### A. 保持 Python 内核（推荐）

继续在本仓库演进。若要模块化，用 Python 插件边界对齐 `dsh` 的缝，而不是换语言：

- `llm`：provider adapter
- `tools`：注册与执行管道
- `session/messages`：已有 SQLite 流
- `channels`：feishu / weixin / voice / web / api
- `memory`：日记 + 关系
- `runtime`：scheduler / subconscious / heartbeat

工作量小，不打断用户，符合 GOAL.md。

### B. 混合：Python 主体 + 可选调用 `dsh`

只在需要「编程沙箱 / 强工具循环」时，把 `dsh` headless 当一个 tool。xAgent 仍拥有身份、日记、渠道。

适合以后做「agent 会写代码」的子集，不适合当架构主线。`dsh` 稳定前不要做。

### C. 自研 TypeScript 运行时（仅当明确要做 1.0 语言迁移）

不要 fork `dsh`。若未来真要 Node，应做 **xAgent-native TS**：

- 保留 GOAL.md 的主体 / 日记 / 多用户模型
- 插件缝可以像 Cordis，但 profile 叫 agent 而不是 coding session
- 数据目录继续是 `~/.xagent/`
- 飞书 / 微信 / 语音作为 channel 插件，而不是事后补丁

这是新产品大版本，不是探索分支能做完的事。启动条件见文末。

### D. 整仓迁到 `dsh` 插件树（不推荐）

把身份、日记、渠道都写成 Cordis 插件。短期能演示「我们也是 plugin-first」，长期会被 `dsh` 的 session/coding 假设拖着走，并承担 breaking changes。

---

## 若仍要 Node：最小可行切片（不要从 launcher 开始）

如果只是验证「TS 能不能表达 xAgent」，顺序应该是：

1. **数据兼容层**：读现有 `messages.sqlite3` 和 `memory/daily/*.md`。
2. **ModelClient + 单轮 `chat_events`**：对齐现有 event 形状，让 Web UI 不改。
3. **日记写入 + search_memory / write_memory。**
4. **一个渠道**：Web，而不是飞书。
5. 停。用这一层对比 Python 的延迟、prompt 组装、日记质量。

不要先搬 CLI launcher、飞书、语音。那些是重写税，不是验证项。

示意接口见 [`docs/nodejs-rewrite/seams.ts`](nodejs-rewrite/seams.ts)。那是设计草稿，不是可运行运行时。

---

## GOAL.md 检查

- **Identity：** 整仓改 Node / 迁 `dsh` 都不自动增强主体性。迁 `dsh` 还有把 agent 降成「会话工具」的风险。
- **Multi-user：** `dsh` 无一等用户模型；必须自研，重写期间容易回归串身份。
- **1:1 and group：** 飞书群参与是高风险端口；Node 重写最先伤这里。
- **Memory/journal perspective：** 日记文件格式可保持；危险的是 loop/prompt 重写后第一人称语气漂移。
- **Unified memory：** 不要在 TS 里按 user 分库。现有设计已是 agent 级一流。
- **Agent-governed sharing：** 与语言无关；重写时不得改成 RAG 分权。
- **Diary-only carrier：** 不要借重写引入向量库当主记忆。
- **Attribution and continuity：** SQLite + channel 元数据必须兼容；这是重写的硬约束。

GOAL.md 不支持「为了跟 `dsh` 同栈而重写」。

---

## 何时可以重新考虑 Node

同时满足再打开这个话题：

1. Python 渠道层已经稳定，飞书 / 微信 / 语音不再每周改协议细节。
2. 有明确的 Node-only 生态收益（例如必须发一组 npm channel 插件，且 Python 包无法参与）。
3. 有人愿意维护双栈或接受一次安装方式断裂，并写出 `~/.xagent/` 兼容测试。
4. 若想靠 `dsh`：等它结束 developer preview，core 插件 API 有版本承诺。

现在四条都不成立。

---

## 本探索的产出与非产出

产出：

- 本判断
- 子系统移植表
- 四条路径排序
- TypeScript 缝草稿（示意）

明确不做：

- 不新增 Node 运行时
- 不改 Python 行为
- 不引入 `dsh` 依赖
- 不迁移 `~/.xagent/`
