# xAgent 潜在缺陷与优化修复报告

审计范围：当前 `main`（`myxagent` 0.3.27）。结论来自源码核对，不是静态扫描清单。优先级按「真实用户伤害 × 触发难度 × 修复侵入性」排序。

## 总览

系统作为本地优先、多通道、日记式记忆的独立主体，主路径大体可用。真正危险的问题集中在三处：**身份键不稳定**、**历史数据可能被静默销毁**、**多进程/锁语义导致交错或重复投递**。安全面在默认绑定 `127.0.0.1` 时风险较低，一旦把 API 绑到非本机地址就会立刻变成高危。

建议按四批推进，不要平行大改：

| 批次 | 主题 | 建议顺序 |
| --- | --- | --- |
| P0 | 数据丢失 / 身份错绑 | 消息库 schema 迁移、飞书 `user_id` |
| P1 | 并发与投递正确性 | 微信锁、日记 checkpoint、跨进程回合锁、潜意识收件人 |
| P2 | 前端可复现故障 | 轮询把切换器打空、聊天连发、interval 编辑 400、任务分页被刷掉 |
| P3 | 加固与测试债 | CORS、shell 边界、QR 过期、时区、hardlink 回退 |

已在途、未合入 `main` 的相关工作：#42（潜意识与醒着回合重叠）、#36（工具草稿泄漏到用户可见回复）、#37（DeepSeek Responses API）。本报告不重复实现这些 PR，只把它们标成已知缺口。

---

## P0 — 必须先修

### 1. 消息库遇到非预期 schema 会 DROP 整表

**位置：** `xagent/components/message/sqlite_messages.py`（`_initialize_database`）

列集合只要和 `{"id", "timestamp", "message_json"}` 不完全相等，就会：

```text
DROP TABLE IF EXISTS messages
→ 再建空表
```

升级、部分迁移、手工加列、旧构建残留，都会把对话历史清掉。没有备份，没有导出，测试里也没有覆盖这条路径。

**建议：**

- 引入版本化 migration，只 `ALTER` / 重建索引。
- 无法识别时 fail-closed：拒绝启动并提示备份路径，绝不 `DROP`。
- 加回归测试：额外列、缺列、空库三种情况。

**影响：** 对话时间线永久丢失，日记 checkpoint 也会对不上。

---

### 2. 飞书把显示名当成稳定 `user_id`

**位置：** `xagent/integrations/feishu/adapter.py`（p2p / group 回复约 541、707 行；观察路径约 756 行）

```python
user_id=sender_name
```

稳定身份变成 `"Alice"` / `"飞书用户"`，而不是 `ou_…`。后果：

- 同名用户被合成一个人。
- 改昵称后关系卡、联系人、记忆全部断裂。
- 解析失败时所有人塌缩到同一个 fallback 名（`FEISHU_USER_FALLBACK_NAME`）。

这直接违反 `GOAL.md` 的「多用户区分」和「归因必须稳定」。关系键会变成 `feishu:Alice` 这类不可迁移的字符串。

**建议：**

- `user_id` 使用 Feishu `open_id` / `user_id`。
- 显示名只进 `sender_name`、关系卡 `display_name`、联系人注解。
- 写一次性迁移：能对上 `sender_id` 的旧卡合并过去；对不上的保留并打标记，不要静默丢。

**影响：** 多用户串记忆、潜意识发错人、关系卡污染。这是产品原则级缺陷，不是边角。

---

## P1 — 高优先级（正确性 / 竞态）

### 3. 微信按用户锁在释放后立刻 `pop`，可并行处理同一人

**位置：** `xagent/integrations/weixin/adapter.py` 约 299–306 行

```python
lock = self._chat_locks.get(user_id) or asyncio.Lock()
self._chat_locks[user_id] = lock
async with lock:
    await self._handle_dm(...)
self._chat_locks.pop(user_id, None)  # 等待者还握着旧锁时，新消息会再造一把锁
```

典型交错：A 处理完 `pop`；B 仍持旧锁；C 进来拿到新锁，与 B 同时跑。飞书侧则是另一极端：`_chat_locks` 只增不删，长期群聊会 unbounded 增长。

**建议：** 引用计数或「仅当 dict 里仍是这把锁且无人等待时再删」。飞书用同一套安全回收，不要再写 Weixin 这种 `pop`。

---

### 4. 日记分批写入，checkpoint 只在全部成功后推进

**位置：** `xagent/core/handlers/memory.py` 约 319–335、522–561 行；cursor 写入约 789–792 行

前面的 batch 已经 append 进 markdown 日记；后面的 batch 或 `_commit_processed_message_id` 失败会 `return False`，cursor 不动。下次维护会把同一批消息再写一遍，日记重复。进程若在 append 与 checkpoint 之间崩溃，同类问题。

`.journal_cursor` 还是原地 `Path.write_text`，崩在半截会得到坏 cursor，下次当 0 或错位。

**建议：**

- 每个 batch 成功后立刻推进 cursor（或给日记条目幂等 id / 内容哈希）。
- cursor 用 temp + `os.replace` + fsync，与任务文件同一套原子写。

---

### 5. 多通道是多进程，回合锁只在进程内

**位置：** `xagent/interfaces/cli/processes.py`（`api` / `feishu` / `weixin` / `voice` 分进程）；`xagent/core/inbox.py`（`asyncio.Lock`）；日记 append 同样是进程内锁。只有 journal flock 跨进程。

同一 agent 同时开 API + 飞书时，两条醒着的回合可以交错写入同一 SQLite 消息流和同一日记文件。WAL 能减少损坏，但不能保证「一个主体同一时刻只有一个活回合」。

**建议：** 对 waking turn 加跨进程 flock（或收成单 runtime 多路复用）。日记 append 也走 flock。这是架构债，修完能消掉一类「记忆时间线对不齐」的玄学 bug。

---

### 6. 潜意识收件人第二轮是子串匹配，先命中者获胜

**位置：** `xagent/core/runtime/subconscious.py` `_pick_recipient` 约 799–805 行

精确匹配之后：

```python
if any(token and (hint in token or token in hint) for token in partial_tokens):
    return contact
```

`"李"` 会打到 `"李明"`；短 token、通道注解（`Telos (feishu)`）也会误中。这是隐私/错发，不是体验毛刺。

**建议：** 去掉裸子串。只保留 exact / `channel:user_id` / 唯一 display name；多个候选则不发并记日志。与 #42（inbound 未回复时不要 outbound）一起做更合适。

---

### 7. 调度 crash recovery 会把已投递任务抢回来再跑

**位置：** `xagent/core/runtime/tasks.py` `recover_running_tasks` 约 747–764 行

`.running-*` 被 rename 回 pending。若崩溃发生在「通道已发出、complete 尚未落盘」之间，`message` 类任务会再发一次。这是 at-least-once，当前没有投递回执，也没有测试。

**建议：** 先写 delivery receipt 再 complete；通道侧用幂等 key。至少给 `recover_running_tasks` 补单测（成功投递后崩溃 / 半文件 / 重名）。

任务文件移动依赖 `os.link`（约 1084、1115、1129 行），在不支持 hardlink 的文件系统上 enqueue/归档会直接失败。应在 `OSError` 时回退 `os.replace` 或 copy+unlink。

---

### 8. 管理 API 无鉴权 + CORS `*` 且带 credentials

**位置：** `xagent/interfaces/server/app.py` 约 50–56 行；`admin_routes.py` 暴露 identity / config / workspace / memory / tasks。

默认 host 是 `127.0.0.1`，本机风险可控。一旦 `--host 0.0.0.0` 或配置里改掉 bind：

- 任意能打到端口的客户端可读写 agent 数据、清记忆、改配置、触发投递。
- `allow_origins=["*"]` + `allow_credentials=True` 本身就不合法，浏览器行为也不确定。

Shell 工具（`run_command`）只限制 cwd 在 workspace 内，命令本身可以 `cat`/`curl` 任意路径。本地 agent 需要 shell 是产品选择；和「无鉴权 HTTP」叠在一起才危险。

**建议：**

- 非 loopback bind 必须有本地 token。
- CORS 收敛到 web client origin，禁止 `*` + credentials。
- shell：对 workspace 外路径、破坏性命令走 ASK/拒绝策略（可后续批次）。

---

## P2 — 前端与调度交互（用户能直接碰到）

### 9. `/api/agents` 一次失败会把已加载的 Agent 切换器打成空态

**位置：** `frontend/src/context/AgentSessionContext.tsx` 28–36 行；`App.tsx` 每 5s `refresh()`；`AgentSwitcher.tsx` 44–59 行只要 `error` 就渲染 “Cannot reach service”。

后台轮询失败时 `agents` 其实还在，UI 却整块替换。体感是「Agent 突然消失」。

**建议：** 已有列表时把 error 当 toast/banner；只有 `agents.length === 0` 才进空态。

---

### 10. 聊天连按 Enter 会重复发送

**位置：** `ChatPage.tsx` `submitMessage` 先 `setMessageText("")` 再 `sendMessage`；`ChatContext.tsx` 用 React state `panel.sending` 做闸门。

按键重复发生在 re-render 前，两次调用都看到 `sending === false`。没有 Stop/interrupt：socket 存在 `socketsRef` 里，UI 从不关闭它。

**建议：** 同步 `sendingRef`；忽略 `repeat`；发送中提供 Stop（关 socket + 标记 assistant 为 cancelled）。这与未合入的 #36（草稿泄漏）是同一条「用户可见回复」线。

---

### 11. 编辑已有 interval 任务时，带「立即/延迟首次运行」会 400

**位置：** `frontend/src/lib/taskFormUtils.ts` `intervalPayload()` 会同时带 `delay_seconds` 与 `interval_seconds`/`end_at`；后端 `update_scheduled_task`（`tasks.py` 548–552 行）拒绝 schedule retarget 与 interval patch 混用。

UI 在编辑态仍提供 “Run once immediately” / 自定义 delay，保存必失败。

**建议：** 更新已有 interval 时不要发 `delay_seconds`；改首次运行走 `recurrence` / `start_at`。补一条 frontend 或 API 往返测试。

---

### 12. 任务列表 20s 轮询会丢掉 “Load more”

**位置：** `frontend/src/pages/TasksPage.tsx` 155–160 行

轮询永远 `offset=0` 然后 `setData(response)`，已翻页内容被第一页覆盖。

**建议：** 只刷新已加载窗口，或翻页后暂停轮询。

---

### 13. 其它前端正确性

| 问题 | 位置 | 修复 |
| --- | --- | --- |
| 图片上限 5 张只在后端拒绝，前端可多选 | `ChatContext.tsx` `MAX_IMAGES_PER_MESSAGE` 未在 `addAttachments` 强制 | 按 kind=image 计数，失败要提示（现在 `.catch(() => undefined)`） |
| 聊天 `user_id` 可被清空，任务 WS 却 fallback 到默认 id | `ChatContext.tsx` 231 vs 529 | 统一 `trim() \|\| DEFAULT_WEB_USER_ID` |
| QR 过期文案说「马上会出新码」，但 `startChannelQr` 只在 mount 调一次 | `QrAuthPanel.tsx` 32–88 | `expired` 后 backoff 再 `begin()` |
| 连通性条只看 `health.web`，agent API 挂了仍显示在线 | `ConnectivityContext.tsx` | `web && api_reachable`，或单独 degraded 态 |
| `requestJson` 把非 JSON 当成 `{}`，除 health 外无超时 | `frontend/src/lib/api.ts` | 解析失败当错误；mutation 加 AbortController |

月度循环（刚合入）前后端规则对齐（`day` / `nth+weekday` / `day=-1`），没有发现独立逻辑 bug。缺口是测试：HTTP create、nth/last-day `tick()`、失败后 reschedule、表单往返都还没有。

---

## P3 — 加固、时区、测试债

### 14. 消息按日搜索用 UTC 日界，展示用本地时间

**位置：** `sqlite_messages.py` 337–348 行（UTC midnight）；453 行 `fromtimestamp` 本地格式化。

非 UTC 用户按「某一天」搜索会错一天。消息 `timestamp` 是 `time.time()` 的 epoch，存储本身没问题，过滤边界错了。

**建议：** 搜索日期按本地 TZ 解释，或存储与过滤统一用 aware datetime。

---

### 15. `web_fetch` 先解析再 `follow_redirects=True`，存在 DNS rebinding / 跳转 SSRF

**位置：** `xagent/tools/web_fetch_tool.py`：校验 hostname 解析结果后，httpx 仍跟随重定向，不绑定已解析 IP。

本机 agent 危害低于云端，但仍可打到 `169.254.169.254` 或内网。修复：对每个 hop 再查一次，或禁用 redirect 后手动校验。

---

### 16. Inbox `release_turn` 无所有权

**位置：** `xagent/core/inbox.py` 123–125 行。谁都能 `release()` 当前锁。`Agent.chat_events` 的 `try/finally` 目前用法是对的，但误用会解开别人的回合。改成 `async with` 或记录 owner task id。

---

### 17. 潜意识 habituation 只在进程内存

`_stale_streak` 重启清零。高 `subconscious_activity` 的 agent 重启后会再进入密集 rumination。若要保留 #40 的效果，把 streak 写到 workspace 状态文件。

---

### 18. 前端依赖审计

`npm audit`：markdown-it / linkify-it ReDoS、vite/postcss/nanoid 工具链。聊天/记忆会渲染不可信 Markdown，ReDoS 值得升。vite 主要影响 dev server。

---

## 建议实施顺序（按 PR 切开）

不要做一个「全能修复」PR。建议：

1. **消息 schema 迁移（P0-1）** — 纯存储，可单独测，先挡住数据丢失。
2. **飞书稳定 user_id + 关系卡迁移（P0-2）** — 需要数据迁移脚本和飞书适配测试。
3. **微信锁 + 日记 per-batch checkpoint + 原子 cursor（P1-3/4）** — 行为修复，补竞态测试。
4. **跨进程 turn flock（P1-5）** — 稍大，但能收掉多通道交错。
5. **潜意识匹配收紧**，并 rebase/合入 #42（P1-6）。
6. **前端一批：切换器、发送闸门、interval 400、任务分页、QR、图片上限（P2）** — 全是局部 UI，互不阻塞。
7. **调度 recover 回执 + `os.link` 回退（P1-7）**。
8. **bind 非 localhost 时强制 token + 收紧 CORS（P1-8）**。

每条都应对一下 `GOAL.md`：身份边界、1:1/群、日记视角、统一记忆流、归因。P0-2 和 P1-6 是目标文档里已经写死的不变量，不是风格问题。

## 测试缺口（修的同时补）

当前约 939 个单测、49 个文件，scheduler/memory/subconscious 相对厚，下面几条是盲区：

- `DROP TABLE` 路径、schema 漂移
- `recover_running_tasks`
- 飞书 `user_id` 必须是 open_id（现在测试还在固化 display name）
- 微信锁 eviction 交错
- 日记多 batch 失败重放
- 月度：`POST /api/tasks`、nth/last-day `tick()`、失败 reschedule
- 前端 `taskFormUtils` 无测试
- `enabled: false` 的 reasoning 打开设置再保存会被写成 Default

Jobs 整套不在 `main`（#19/#20），合入前不要在本报告范围里再开一条线。
