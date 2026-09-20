# 持续听，但不逐条排队回答

Status: proposal. 只做分析，不改代码。本文对应 GOAL.md 中的 Environment-Aware /
Multi-Party 两条原则，以及 `docs/agents-env.md` 的世界物理第 2、3 条。

## 1. 现状：听到一句话 = 排一次"要不要接话"

今天唯一实现了"听而不必答"的地方是飞书群聊未被 @ 的分支
（`xagent/integrations/feishu/adapter.py::_route`）。它的流程是：

```
飞书推一条群消息
  → _on_message → asyncio.create_task(_dispatch(msg))     # 每条消息一个 task
  → async with chat_lock                                    # 同一群串行
  → _group_decision_context()                               # 拉一次飞书历史（外部 API）
  → agent.decide_participation()                            # 一次模型调用，输入是"这一条 + 历史"
  → should_reply ? _handle_chat() : agent.observe()
```

`agent.decide_participation`（`xagent/core/agent.py`）本身是无状态的：拿一段房间
文本，返回 `{"should_reply", "reason"}`。它不知道自己上一次刚评估过什么，也不知道
房间里在它评估期间又发生了什么。

别人连续说五句话时会发生什么：

1. 五个 `_dispatch` task 立刻创建，在 `chat_lock` 后面排队。
2. 第 1 句的决策在跑时，第 2–5 句已经在群里，但决策问的是"要不要回应第 1 句"。
3. 如果第 1 句决定回复，`_handle_chat` 进入 `agent.chat_events`，持有 agent 级
   `AgentInbox` 轮次锁，说完才放。
4. 锁一放，第 2 句的决策开始——此时它评估的是一条已经被 agent 自己的回复"越过"了的
   消息。历史重新拉一次，模型再调一次。
5. 依次重复到第 5 句。

代价不只是 5 次模型调用和 5 次历史拉取。更本质的问题是：**判断对象错了**。
Agent 不是在判断"此刻我要不要参与这段对话"，而是在判断"我要不要回应第 N 句"，
连续五次。这会导致：对同一段话重复接话、对已经被后续消息推翻的话接话、
或对一段本可以听完再一起回的问题拆成几段零碎回复。

对照 `docs/agents-env.md` 的第一性原则，这正是它明确反对的模式：

> Hearing does not imply answering.
> Silence is real wall-clock time, not a turn queue.

现在的实现把"听到"和"排队决定要不要答"绑死了，等价于把沉默做成了轮次队列。

需要说明的是，记忆这一侧已经是聚合式的：`MemoryHandler.schedule_experience_write`
只是触发一次带门槛的 maintenance，五条观察不会写五次日记。问题集中在
"听 → 决定"这一段。

## 2. 对提案方向的判断

方向是对的，而且与仓库已有的两个设计事实吻合：

- agents_env 世界只会推送事件，永远不会"要求"任何人回答（`mentions` 是元数据，
  "The world never forces a reply"）。如果 xAgent 的 env adapter 照搬飞书的
  逐条决策模式，在一个有几个 agent、`SPEAK_RATE_PER_SEC = 10` 的房间里，
  每个 agent 都会被自己的决策队列淹没。
- `AgentInbox` 已经把"一个身份同时只有一张嘴"做成了锁。发言意图只维护一个，是把
  这个物理事实往前挪到"想说"的阶段，而不是只在"正在说"的阶段才生效。

把提案的四条拆开看，需要修正或明确的地方：

**"所有消息正常进入观察和记忆"** — 同意，且今天已经基本如此。唯一要改的是
`observe` 要先于决策发生，而不是作为决策"否"之后的落地。听到即记录，不依赖任何
判断结果。`silence_reason` 从"每条消息一个"变成"每个情境一个"，日记为记忆载体，
没有损失。

**"每个 Agent 同时最多维护一个待评估的发言意图"** — 同意"一个正在评估/正在说的
槽位"是 agent 级的（一张嘴）。但"候选情境"应该是**每个房间一个**。否则房间 A 的
闲聊会把房间 B 里酝酿中的考虑替换掉，两个独立对话互相干扰，违反
GOAL.md 的"Keep people, rooms ... separate"。建议：

- 每个 `(agent, room)` 一个 `Situation`，纯数据，无模型调用；
- 每个 agent 一个 `Attention`，负责挑一个房间的情境去评估、去说。

**"新消息到来时更新当前情境，合并或替换过时的考虑"** — 同意，需要补上一个关键
细节：**过时性要用序号判断，而不是用时间**。评估是针对"情境截至 seq N"做的；
评估结束时若房间 `room_seq` 已经超过 N，结论就是陈旧的，应该重评一次（有上限），
而不是直接说出去。飞书没有 `room_seq`，可以用 `message_id` 的到达顺序或本地
消息表的 cursor 代替。

**"明确交给自己的未完成问题单独保留"** — 同意，但"保留"不等于"逐条回答"。
建议把它定义为一种不同的对象——**Obligation**（被直接点名、@、reply-to-me、
1:1 私聊里的每一条）——它：

- 永不被合并进普通情境也不会被新消息替换掉；
- 但同一个人在一个连续片段里的多条直接提问，可以在一个回合里一起答；
- 触发评估时跳过或大幅缩短安静窗口（不让人等）。

也就是说，1:1 私聊里连续五条消息，今天是五个回合五次回复；按这个模型是五个
obligation 在一次回合里被一起处理。这一点提案里没有明说，但是同一个机制的
自然推论，建议一起纳入。

## 3. 概念模型

把今天在 `_dispatch` 里揉在一起的三件事拆开：

| 对象 | 粒度 | 是否调模型 | 作用 |
| --- | --- | --- | --- |
| Perception | 每条事件 | 否 | 落消息表、进 experience 流。今天的 `observe` |
| Situation | 每个房间 | 否 | 我看到哪了、我评估到哪了、我上次说到哪了、哪些话是直接给我的、这一波还在不在继续 |
| Intent | 每个 agent 一个槽位 | 是 | 针对某个房间"截至 seq N 的情境"决定说不说、说什么 |
| Obligation | 每条直接给我的话 | 否 | 挂在 Situation 上，不可被合并掉，说完才清 |

`Situation` 的最小字段：

```
room_id
seen_through        # 最新听到的 seq
evaluated_through   # 上一次决策覆盖到的 seq
spoken_through      # 我上一次发言时房间的 seq
burst               # evaluated_through 之后的消息（decision 的直接输入）
obligations         # 直接给我的、尚未回应的消息
last_event_at       # 用来判定"这一波说完了没有"
```

`Intent` 的状态机：

```
idle ──(room 有新事件)──▶ armed(room, target_seq)
armed ──(安静窗口到 / obligation / 达到最大等待)──▶ evaluating(room, seq N)
evaluating ──(should_reply=false)──▶ idle          # 记一条 situation 级 silence
evaluating ──(should_reply=true, seen_through == N)──▶ speaking
evaluating ──(should_reply=true, seen_through  > N)──▶ evaluating(room, seq N')  # 最多重评一次
speaking ──(说完，自回声入 Situation)──▶ idle
```

`armed` 期间新消息到来：同一房间 → 更新 `target_seq`、重置安静计时（合并）；
其他房间 → 那个房间各自 `armed`，由 `Attention` 决定先评谁（obligation 优先，
其次最久未评估）。

**新消息在 `speaking` 期间到来**：不再各自开决策。进 Situation，等这次说完再看。
`InboxKind.STEER` 和 `AgentInbox.request_abort()` 已经存在但无人调用；
如果这波新消息里出现了直接给我的 obligation 且与正在说的内容冲突，这两个 seam
正是"打断并纳入"的入口。这是后续可选项，不是第一步。

`decide_participation` 的输入从"这一条 + 历史"变成"这一波 + 历史 + 我上次说过
什么 + 哪些是直接给我的"，输出可以多一个字段：`addressing`（我打算回应其中哪
几条），用于把 obligation 标记为已处理，也让 reply 回合知道自己在回什么。

## 4. 风险与对策

- **延迟**。安静窗口会让回复晚一两秒。对群里的旁听式参与这是符合人类习惯的；
  对直接提问不能等——obligation 触发时窗口缩到很短（只够接住"哦对了还有…"）。
- **合并后漏掉一句**。decision 的输入是整波 + 历史，不是一条；`addressing`
  输出让漏掉可被观察到。
- **多 agent 同时"等安静再说"然后撞车**。这是 agents_env 房间里必然出现的。
  对策是上面的 seq 陈旧检查（评估完发现别人已经答了 → 丢弃）加少量随机抖动。
  世界负责把并发发言串行化，agent 负责在说之前再看一眼。
- **飞书历史拉取**。今天每次决策拉一次外部 API；合并后每波一次。更进一步可以
  优先用本地消息表（`sqlite_messages`）构造情境，外部拉取只做补洞。
- **Obligation 丢失**。进程重启时 in-memory 的 obligations 会丢。第一步可以接受
  （今天也没有），后续可从消息表里 `addressed_to_agent=True` 且没有后续
  `ME` 回复的记录重建，这与 GOAL.md "derived views are regenerable" 一致。

## 5. 放在哪里

这不该是飞书 adapter 内部的优化。原因：

- 同样的问题会在 agents_env 的 xAgent adapter、微信群里重复出现；
- Situation/Intent 是 agent 的"注意力"，属于 mind，不属于任何一个 channel；
- 今天 `agents_env` 文档给 adapter 的契约只有一句 "hear → own `observe` /
  `chat`; speak → `speak`"，如果核心不提供这个 seam，下一个 adapter 会再抄一遍
  逐条决策。

建议新增 `xagent/core/attention.py`（与 `inbox.py` 并列）。adapter 只做两件事：
把事件喂进 `attention.hear(event)`，提供 `speak(room, text)` 回调。
`hear` 内部同步完成 Perception（`observe`）并更新 Situation；评估与发言由
`Attention` 自己的循环驱动。

## 6. 行动建议

按侵入程度递增，每一步都能独立落地、独立验证：

**第 0 步：把这份文档定为口径。** 统一 Perception / Situation / Intent /
Obligation 四个词，后面 PR 用同一套词。

**第 1 步：在飞书 adapter 内做最小验证。** 不动核心。未 @ 的群消息：立刻
`observe`，然后 `arm(chat_id)`；每个群一个 debounce task，安静窗口到后把这一波
消息拼成一个上下文调一次 `decide_participation`。@ 消息保持今天的即时路径。
这一步就能把"五句话五次决策"变成"五句话一次决策"，改动局限在 `_route` 的
未 @ 分支和一个每群的小状态。用来确认安静窗口的取值区间和 decision prompt
对"整波输入"的表现。

**第 2 步：上提到核心。** 新增 `xagent/core/attention.py`，实现 Situation /
Intent / Obligation 与状态机；`decide_participation` 改为接受 situation 形状的
输入并返回 `addressing`；加入基于 seq 的陈旧重评。飞书 adapter 切到这个 seam，
1:1 私聊的连续消息也走 obligation 合并。

**第 3 步：agents_env 的 xAgent adapter 直接建在这个 seam 上。** 用
`agents-env dummy` 做 burst 脚本（一个 dummy 连续说五句），验证一个 agent 只
评估一次、只回一次；再放两个 agent 进同一房间验证撞车处理。

**验收指标**（第 1 步起就能记日志）：

- decisions / utterance（目标：一波内 ≪ 1）
- replies / burst（目标：≤ 1，除非 obligation 明确要求分开答）
- 直接提问的首字延迟（不能比今天差）
- "回应了一条已被别人回答的消息"的次数（陈旧检查有效性）

## 7. GOAL.md check

- **Identity** — 一个 agent 一个 Intent 槽位，等于一张嘴；不引入 persona 变化。
- **Multi-user** — Situation 按房间分，obligation 按发言者归属；不混人。
- **1:1 and group** — 同一机制：1:1 是每条都是 obligation 的房间。
- **Memory / journal** — Perception 不变，日记仍是唯一记忆载体；silence 从每条
  变成每个情境一条。
- **Unified memory** — 不新增按用户切分的存储；Situation 是可丢弃的运行态。
- **Sharing** — 说不说、说什么仍由 agent 判断，只是判断的对象从一句话变成一段情境。
- **Diary-anchored** — Situation / Obligation 是可重建的运行态投影，不是第二个
  真相源；重建来源是消息表。
- **Attribution / continuity** — 决策以 seq 为锚，知道自己评估到哪、说到哪；
  `addressing` 让回复能追溯到具体哪几句。
- **Environment-aware** — 与 agents_env "hearing does not imply answering" 完全
  一致，是把这条物理规则在 mind 这一侧的对应实现。
