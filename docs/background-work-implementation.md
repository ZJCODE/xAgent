# 后台任务：第 1-2 步实施计划

对应《自然对话与后台任务方案》的实施顺序第 1、2 步。本文只细化这两步；
第 3-5 步（主动委派、自动转后台、steering 移除、UI）不在本计划内，但第 2 步的
设计为它们留好接缝。

原则：**每一步都保持现有渠道行为不变（或只有明确的、有意的行为变化），
复用现有存储与回执机制，不引入新的抽象层。**

> **状态：第 1、2 步已完成并全量验证（1397 tests OK）。**
> 实现与本文的偏差见 §8「实现偏差记录」。

---

## 0. 相对原方案的简化决策

以下决策有意偏离原方案的表述，理由都是"实现更简单且不损失目标语义"：

- **D1 前台预算改为单一墙钟预算**，不做"工具执行时间累计"。原方案"进入工具执行后
  累计 8 秒"需要维护跨批次计时器；单一预算（回合开始计时，覆盖模型+工具时间，
  默认 8 秒、可配置）实现只需一个 deadline 比较，且顺带解决"慢模型占住主对话"问题。
  副作用：纯长推理回合也会转后台——这正是我们想要的行为。
- **D2 事件总线收在 `chat_events` 内部**。所有渠道都直接迭代 `chat_events`
  （`chat_service.py:189`、`weixin/adapter.py:569`、`task_dispatch.py:138`、
  `host.py:779`），所以把它内部从"直接生成"改成"broker 持有执行、调用方订阅"，
  一处改动全员生效，不新增第二套执行 API。
- **D3 不建独立事件存储**。结构事件增量追加进现有 `TurnStore` 的 `events` 列表；
  工作记录（work）只存摘要、状态、控制消息和 turn 引用。三套 store 都是
  "一张表 + record_json" 的既有模式（`turns.py`、`task_store.py`）。
- **D4 `tasks.sqlite3` 零迁移**。定时执行的 work 关联放进 run 的 JSON payload
  （schema 不变）；新增的 work 存独立文件 `work.sqlite3`。现有 `user_version=1`
  校验（`task_store.py:126`）不动。
- **D5 后台并发池一个 semaphore(2)**，聊天后台任务与定时 agent 任务共用；
  message 型定时任务不占位（无模型调用）。前台沿用 inbox 锁 + chat 信号量，不动。
- **D6 只新增一个 inbox kind：`work_turn`**。提示词分支
  （`config.py:575` 的 `build_current_task`、`prompt_registry.py:173`）
  把 `work_turn` 与 `scheduled_turn` 同等处理；工具侧 `ToolCallContext.inbox_kind`
  自然透传（`agent.py:1170`）。
- **D7 断线语义统一为"执行不中断"**。原方案里"客户端断线不会停止后台执行"，
  实现上最简做法是前台、后台一致：订阅者断开只摘掉订阅，broker 持有的执行继续跑完，
  结果按正常渠道持久化/交付。这会改变现有行为（今天断线 → 回合标 `interrupted`，
  `agent.py:898-907`），属于**有意的行为变化**，需同步更新相关测试（见 §6）。
- **D8 资源锁规则用 retry_safety 兜底**，不要求为每个工具补声明：
  `read_only` → 免锁；有 `write_scope` → 按资源 key 锁；其余（`external`，
  含 `idempotent` 未声明者）→ 全局写锁。新增工具属性 `write_scope`（字符串，
  与 `retry_safety` 同款声明方式），首批只标注现有写工具。
- **D9 授权规则从紧**：work 的查询/控制/交付要求请求的 channel + user 与创建者一致，
  跨渠道一律 404（与 turn 校验同规则，`inbox.py:161-176`）。后续要放宽再放宽。

---

## 1. 第 1 步：执行上下文隔离收尾（行为不变）

目标：`ToolExecutor` 不残留任何实例级会话状态，为第 2 步的并行执行铺路。

### 1.1 see_image 状态隔离

现状：`_see_image_already_visible` / `_see_image_paths` 是 executor 实例状态
（`executor.py:57-59`），`begin_see_image_turn` 每回合重置（`agent.py:980`），
`pending_see_image_paths` 批次后消费（`agent.py:1172`）。并行执行会互踩。

改法：与已提交的 call scope 同款——`ContextVar("see_image_turn_state")` 持
一个可变 dict（`{"visible": int, "paths": list}`）；`begin_see_image_turn` 设置
新 dict，`_queue_see_image_path` / `pending_see_image_paths` 读当前上下文
（get 时为空则惰性创建）。`_see_image_lock` 保留（同回合批次内仍可能并发）。

改动面：`executor.py` 一处；`agent.py` 调用点签名不变。

### 1.2 新增 `InboxKind.WORK_TURN`

- `inbox.py`：enum 加值 `WORK_TURN = "work_turn"`。
- `config.py:575` `build_current_task`：`if inbox_kind in {"scheduled_turn", "work_turn"}`
  走任务模板。
- `prompt_registry.py`：凡按 `scheduled_turn` 分支处同样放行 `work_turn`。
- 检查 `is_scheduled_work`（`inbox.py:34`）等按 kind 判断的辅助函数是否需要并列。

本步不接任何调用方——只是枚举与分支就绪。

### 1.3 验收

- 现有测试全绿（重点 `test_agent_inbox.py`、`test_agent_chat_flow.py`、
  `test_tool_execution_scope.py`）。
- 新增：两个交错回合的 see_image 状态互不可见；异常后 ContextVar 复位。

---

## 2. 第 2 步 A：ExecutionBroker（事件总线，最小实现）

目标：执行由 host 持有的 task 驱动，前端/渠道只是订阅者；订阅者可中途接入、
可断开，执行不受影响。**本步所有执行仍是前台语义**（无预算、无转后台），
预算参数留空，第 3 步启用。

### 2.1 组件（新文件 `xagent/core/runtime/broker.py`，约 100 行）

```
ExecutionBroker(turn_iterator, *, turn_id, event_key, budget_seconds=None)
```

- **drain task**：broker 内部启动一个 asyncio.Task 消费 turn_iterator
  （即 `_run_inbox_turn` 生成器），对每个事件：
  1. 赋单调 `seq`；
  2. 结构事件（`accepted`/`message_start`/`message_done`/`tool_call`/
     `tool_result`/`error`/`aborted`/`done`）经 `turn_store.update` 增量追加；
     `message_delta` 不持久化，只转发；
  3. 推给所有订阅者队列（`put_nowait`，队列满则关闭该订阅者，**绝不阻塞执行**）；
  4. 若设了 `budget_seconds` 且到期、尚未后台化：标记 backgrounded，
     生成合成事件 `{"type": "backgrounded", ...}` 按结构事件处理
     （第 3 步才有预算，此处只是接缝）。
- **`subscribe()` → AsyncIterator[dict]**：持 broker 内部锁一次性完成
  "快照已持久化事件 + 注册新队列"（锁内无 await，保证无重无漏），先回放快照
  （带 seq 去重），再消费实时队列。晚接入者拿不到 `message_delta`，
  用 `message_done` 的完整内容兜底（回放已含）。
- **异常/结束**：turn_iterator 正常耗尽 → 发 `done` 后关闭订阅者；
  异常 → 经 `turn_store.update(state=...)` 落错误态后向订阅者发 error 事件。
  broker 自身被取消（进程关停）→ 生成器 aclose，走现有 `interrupted` 路径。
- **budget 检查点**：drain 循环里对每个事件与每次 wait 超时（复用现有
  `_TurnStepTimeout` 的 `wait_for` 模式，`agent.py:844`）检查 deadline。

### 2.2 `chat_events` 集成

`chat_events` 保持 async generator 签名不变；内部改为：

```
broker = ExecutionBroker(self._run_inbox_turn(...), ...)
async for event in broker.subscribe(): yield event
```

所有调用方（HTTP/WS/weixin/定时/task_dispatch）零改动。`_run_inbox_turn`
本身的 inbox 锁获取/释放、steering、TurnStore.begin/end 全部不变。

**行为变化（有意）**：订阅者 aclose（客户端断开）只摘队列，drain task 继续。
回合照常持有 inbox 锁直到完成，主对话有序性不变。

### 2.3 持久化增量事件

`TurnStore.update` 增加 append 语义：对每个结构事件 `events` 列表追加
（带 `seq`）。回合结束时照旧写 `state`/`final_events`。写放大约每结构事件一次
UPDATE（read-modify-write record_json），体量可接受；不加批量优化，等观测数据说话。

### 2.4 续接快照（为第 2 步 C 的恢复铺路）

`_drive_claimed_turn` 在每次模型调用边界前，把 `input_messages`（sanitize 后）
存进 turn 记录的 `resume_snapshot` 字段。快照是恢复"从完整边界续接"的最小状态：
重放快照 + 重跑模型，已执行工具不重跑（其结果在快照里）。

简化规则：快照序列化后超过 1MB 则丢弃并标记该 turn `resumable=false`，
崩溃恢复时直接进 `needs_review`（不做流式/增量快照）。

---

## 3. 第 2 步 B：WorkStore + WorkRunner（本步只建不接）

第 3 步才会真正有后台任务运行；本步把存储与管理面建好、可单测。

### 3.1 WorkStore（新文件 `xagent/core/runtime/work_store.py`，约 100 行）

独立文件 `workspace/.runtime/work.sqlite3`，一张表：

```sql
CREATE TABLE work(id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
```

record_json 内容（与 `task_store.py` 的 payload 模式一致）：

- 来源与会话：`channel`、`user_id`、`room_name`、`source`（delegated /
  converted / scheduled）
- `goal`、`completion_criteria`、`context_refs`、`attachment_refs`
- 状态机：`queued → running → completed | failed | needs_review`，
  分支 `running → stopping → completed(stopped)`、`running → waiting_user → running`
- `turn_ids: list[str]`（一次执行一个活跃 turn）
- 控制消息 `controls: [{control_id, content, state: received|incorporated, ts}]`
  ——**只有这两个状态**，绝不写"已完成"
- 结果与交付：`result_ref`、`delivery: {status, receipt_ref}`
  （交付状态独立于执行状态，复用 `task_receipts.journal_delivery` 的
  per-part 回执与 `strict_task_delivery`）
- `resumable`、`needs_review_reason`

所有读写走 `asyncio.to_thread` + `BEGIN IMMEDIATE` + WAL（下同，见 §4.1）。

### 3.2 WorkRunner（约 80 行，放 `runtime/` 下）

- `semaphore(max_background_work)`（默认 2，配置项新增加在 `AgentConfig`）。
- `run(work_id)`：占位 → 以 `inbox_kind=work_turn` 走 broker 驱动一条
  `_drive_claimed_turn` 路径（**不经过 AgentInbox、不占 chat 信号量**）；
  执行上下文的"补充要求队列"复刻 inbox 的 steering 三件套
  （`request_steer`/`take_steering`/`requeue_steering`，`inbox.py:216-263`），
  把这三段抽成一个小 `SteeringQueue` 类供 inbox 与执行上下文共用——纯搬移，
  行为不变。
- 控制操作：`supplement(work_id, content)` → 持久化 `received`；
  drain 循环在下一模型边界消费（对应 `_consume_steers` 的位置）并标 `incorporated`。
  `cancel(work_id)` → 状态 `stopping`，置执行内 abort 标志（禁止新工具步骤，
  当前批次结束后收敛）。`waiting_user` 的问答路由是第 4 步的事，本步只有状态值。

### 3.3 控制 API（挂进现有 control app，`host.py:707`）

```
GET  /work          # 当前 channel+user 的 work 摘要列表
GET  /work/{id}     # 状态、摘要、最近进展、结果引用、最新 turn 事件引用
POST /work/{id}/messages   # {content} → {accepted, control_id, state:"received"}
POST /work/{id}/cancel     # → {stopping: true}
```

授权（D9）：channel + user 与创建者一致，否则 404。全部只读/控制、不交付。

### 3.4 事件 schema 预留

`chat_events` 的结构事件在 broker 层补两个可选字段：`work_id`、
`work_status`（第 3 步起非空）。`done` 语义不变（回合结束 ≠ 工作完成），
另立 `backgrounded` 事件（第 3 步启用）。

---

## 4. 第 2 步 C：恢复、资源协调、SQLite 加固

### 4.1 SQLite 三个改动（无 schema 迁移）

1. `TaskStore.connect`（`task_store.py:126-135`）加 `PRAGMA journal_mode=WAL`、
   `PRAGMA busy_timeout`（沿用 timeout=10 的语义）；
2. `TurnStore`/`WorkStore` 同款；
3. 调度器与 store 的写调用点（`claim`/`save_run`/`save_delivery` 等）统一
   `asyncio.to_thread` 包裹（`TurnStore` 已是此模式，`turns.py:36-61`），
   避免并行执行时同步 SQLite 阻塞事件循环。

### 4.2 恢复顺序与规则（host 启动，`host.py:186` 附近）

先 `WorkStore.recover()` 后 `TurnStore.recover()`：

- work `queued` → 保持排队，重启后重跑；
- work `running` 且活跃 turn 可续接（`resumable=true`、无 `unsafe_tools_started`、
  快照完整）→ 从快照边界续接（重跑模型调用，重放工具结果）；
- 其余非终态 → `needs_review`，**不盲目重试**（沿用 `task_store.py` 的保守语言）；
- 已确定结果只重试交付，不重算（交付回执按 `journal_delivery` 指纹去重）。

### 4.3 资源协调（`ResourceLocks`，约 40 行）

- 规则见 D8。key：`write_scope` 声明的资源名（如 `workspace_files`、
  `browser`）；未声明且非 read_only → `default_write` 全局锁。
- 挂点：`ToolExecutor.execute_single` 执行 `func(**args)` 前后获取/释放
  （`asyncio.Lock`，读工具零开销）。`subconscious` 不带工具（`subconscious.py:561`），
  无需覆盖。
- 首批 `write_scope` 标注：`run_command`、文件写入类工具、浏览器类工具、
  todo/记事类工具（以 `xagent/tools/` 实际清单为准）。
- 已知接受项：前台回合等资源锁时仍持有 inbox 锁（自己的工具在跑，本就要等）；
  8 秒预算兜底后转后台（第 3 步）自然解决长等待场景。不为此加提前释放逻辑。

---

## 5. 验收（第 1-2 步）

沿用原方案对应场景，另加两条：

1. 现有全部测试 + 渠道联调脚本（`scripts/runtime_soak.py`）通过；
2. 断线行为变化：HTTP/WS 断开后回合继续完成、结果入历史、重连可查
   （更新 `test_runtime_faults.py` 等受影响的断线测试）；
3. 两个 work 单测并行：see_image、supplement 队列、上下文互不串用；
4. 重启恢复：queued 重跑 / 可续接续接 / 不确定进 needs_review，各一条用例；
5. 资源协调：两个写任务按 key 串行、读任务并行；
6. 授权：同 channel+user 可查可控，跨渠道 404。

## 6. 预期需要更新的现有测试

`test_runtime_faults.py`（断线语义）、`test_agent_inbox.py`（若 SteeringQueue
抽取）、`test_voice_stage5.py` / `test_voice_runtime.py`（voice 断线路径）、
`test_tasks_api.py` / `test_channel_steer.py`（若事件字段变化）。
逐一确认是"断言过时"而非"行为回归"再改。

## 7. 明确不做（本两步）

- 自动转后台、主动委派、任务控制内部工具（第 3 步）
- steering 移除、等待用户问答路由、跨渠道控制（第 4 步）
- 任务卡片、任务页、文本/语音通知、metrics（第 5 步）
- 通用 pub/sub 框架、独立事件存储、tasks.sqlite3 schema 迁移、
  心跳/潜意识改造、多进程/分布式队列

## 8. 实现偏差记录（与本文初稿的差异，均已验证）

1. **see_image 状态改回实例属性（撤销 §1.1 的 ContextVar 化）**。
   `_drive_claimed_turn` 生成器每次 `__anext__` 都经由 `_run_inbox_turn` 里的
   `asyncio.wait_for`（`agent.py:844`）在**新 task** 中执行，ContextVar 值在
   迭代间不传播，导致视觉回合图片注入失效（`test_agent_chat_flow` 回归）。
   inbox 锁保证同一 agent 的回合严格串行，实例属性本无互踩风险，故回退为
   HEAD 的实例属性实现。`_call_scope` 的 ContextVar 保留（set/read 都在同一次
   迭代内，语义成立）。
2. **broker 的 drain 不按事件建 task，`__anext__` 直接在 drain task 中执行**。
   同样的 task 跳跃问题会污染未来生成器内的 ContextVar 状态。预算检查因此是
   **事件粒度**的累计墙钟（每个事件之间检查 deadline），不中断进行中的步骤；
   单步超预算时 `backgrounded` 事件在该步骤结束后发出。这同时避免了
   `wait_for` 超时取消 `__anext__` 会误伤生成器的隐患。
3. **budget 对所有 inbox kind 生效**（含 scheduled_turn）：超时同样发出
   `backgrounded` 事件，对已后台的调用方只是多一条已持久化事件，无行为影响。
4. **调度器 store 调用只在异步路径包 to_thread**（`tick`/`_run`/`start`/`stop`）。
   `_done` 完成回调与 `note_task_tool`（同步函数，改异步会波及 ToolExecutor）
   保持同步小写入，体量可忽略。
5. **重放事件统一带 `replay: true`**：broker 回放快照时补打该标记，与
   `_run_inbox_turn` 的 event_key 去重路径一致；带 `seq` 或 `replay` 的事件
   不再二次持久化。
6. **断线语义更新后的测试**：`test_runtime_faults`、`test_runtime_host`、
   `test_model_limits`、`test_agent_chat_flow` 中 5 处断言按新语义更新
   （重放带 replay 标记、断线后回合继续到自身 deadline、清理失败以 error
   事件呈现而非抛给调用方）。
