# 持续听，但不逐条排队回答

Status: proposal. 只做分析，不改代码。范围限定在今天真实存在的渠道：飞书群 /
飞书私聊 / 微信私聊 / Web、API / 语音。对应 GOAL.md 的 Environment-Aware 与
Multi-Party 两条原则。

## 1. 现状：听到一句话 = 排一次"要不要接话"

### 1.1 飞书群（唯一有"听而不必答"的地方）

飞书群里未被 @ 的消息走 `xagent/integrations/feishu/adapter.py::_route`：

```
飞书推一条群消息
  → _on_message → asyncio.create_task(_dispatch(msg))     # 每条消息一个 task
  → async with chat_lock                                    # 同一群串行
  → _group_decision_context()                               # 拉一次飞书历史（外部 API，默认 10 条）
  → agent.decide_participation()                            # 一次模型调用，输入是"这一条 + 历史"
  → should_reply ? _handle_chat() : agent.observe()
```

`agent.decide_participation`（`xagent/core/agent.py`）无状态：拿一段房间文本，返回
`{"should_reply", "reason"}`。它不知道自己上一次评估过什么，也不知道评估期间群里
又发生了什么。

别人连续说五句话时：

1. 五个 `_dispatch` task 立刻创建，在 `chat_lock` 后排队。
2. 第 1 句的决策在跑时，第 2–5 句已经在群里，但决策问的是"要不要回应第 1 句"。
3. 第 1 句若决定回复，`_handle_chat` 进入 `agent.chat_events`，持有 agent 级
   `AgentInbox` 轮次锁，说完才放。
4. 锁一放，第 2 句的决策开始——评估的是一条已被 agent 自己的回复越过的消息。
   历史再拉一次，模型再调一次。
5. 依次到第 5 句。

代价不只是 5 次模型调用和 5 次外部历史拉取。更本质的是**判断对象错了**：
agent 不是在判断"此刻我要不要参与这段对话"，而是连续五次判断"我要不要回应
第 N 句"。结果是重复接话、对已被后续消息推翻的话接话、或把本可以听完一起回的
问题拆成几段零碎回复。

`observe` 今天是决策为"否"之后的落地动作，不是听到即记录。

### 1.2 私聊（飞书 p2p、微信 DM、Web/API）

私聊没有决策环节，每条消息直接 `chat`。飞书按 `chat_id`、微信按 `user_id` 加锁
串行。同一个人连发五条（"帮我看下这个" / "哦还有" / "算了先看第二个"），今天是
五个回合五次回复，第 1 条的回复生成时还不知道第 3 条已经改了主意。

这和群里的问题是同一个：把"到达"当成"回合"。

### 1.3 语音

Soniox 端点检测已经在音频层做了"这一波说完了"的合并，一次 utterance 一次回合。
语音不在本提案范围内，但它恰好是这个方案的参照：**先等人说完，再决定回什么**。

### 1.4 记忆侧不是问题

`MemoryHandler.schedule_experience_write` 只触发一次带门槛的 maintenance，五条观察
不会写五次日记。问题集中在"听 → 决定 → 回"这一段。

## 2. 对提案方向的判断

方向对。`AgentInbox` 已经把"一个身份同时只有一张嘴"做成了锁；"最多一个发言意图"
是把这个事实前移到"想说"阶段，而不是只在"正在说"时才生效。

对四条提案的修正与明确：

**"所有消息正常进入观察和记忆"** — 同意，改动只有一处：`observe` 要先于决策发生。
听到即记录，不依赖判断结果。`silence_reason` 从"每条一个"变成"每个情境一个"，
日记为记忆载体，没有损失。

**"每个 Agent 同时最多维护一个待评估的发言意图"** — "正在评估 / 正在说"的槽位是
agent 级的（一张嘴），同意。但**候选情境要按房间分**：否则群 A 的闲聊会替换掉
群 B 里酝酿中的考虑，也会替换掉某个私聊里还没回的问题。GOAL.md 要求
"Keep people, rooms ... separate"。建议：

- 每个 `(agent, room)` 一个 `Situation`，纯数据，不调模型；
- 每个 agent 一个 `Attention`，负责挑一个房间的情境去评估、去说。

私聊也是房间：一个只有两个人、每句话都是直接给我的房间。

**"新消息到来时更新当前情境，合并或替换过时的考虑"** — 同意，补一个关键细节：
**过时性用序号判断，不用时间**。评估针对"情境截至 cursor N"；评估完发现该房间
已有 N 之后的新消息，结论就是陈旧的，重评一次（有上限），不直接说出去。
本地消息表已有单调 cursor（`sqlite_messages.get_latest_message_cursor`），
每个房间记自己最新一条的 cursor 即可，不需要外部渠道提供序号。

**"明确交给自己的未完成问题单独保留"** — 同意，但"保留"不等于"逐条回答"。
定义为一种独立对象 **Obligation**：@ 我、回复我的消息、点我名字、以及私聊里的
每一条。它：

- 永不被合并进普通情境，也不会被新消息替换掉；
- 但同一个人在一个连续片段里的多条直接提问，在一个回合里一起答；
- 触发评估时不走完整的安静窗口，只留一个很短的"接住补充"窗口。

这意味着私聊连发五条按此模型是一个回合。提案没有明说，但是同一机制的自然推论，
建议一并纳入。

## 3. 概念模型

把今天在 `_dispatch` / `_handle_dm` 里揉在一起的事拆开：

| 对象 | 粒度 | 调模型 | 作用 |
| --- | --- | --- | --- |
| Perception | 每条消息 | 否 | 落消息表、进 experience 流。今天的 `observe` |
| Situation | 每个房间 | 否 | 我看到哪了、评估到哪了、上次说到哪了、哪些话是直接给我的、这一波还在不在继续 |
| Intent | 每个 agent 一个槽位 | 是 | 针对某个房间"截至 cursor N 的情境"决定说不说、说什么 |
| Obligation | 每条直接给我的话 | 否 | 挂在 Situation 上，不可合并掉，说完才清 |

`Situation` 的最小字段：

```
room_key            # channel + chat_id
seen_through        # 该房间最新一条消息的 cursor
evaluated_through   # 上一次决策覆盖到的 cursor
spoken_through      # 我上一次发言时房间的 cursor
burst               # evaluated_through 之后的消息（决策的直接输入）
obligations         # 直接给我、尚未回应的消息
last_event_at       # 判定"这一波说完了没有"
```

`Intent` 的状态机：

```
idle ──(某房间有新消息)──▶ armed(room, target)
armed ──(安静窗口到 / 有 obligation 且短窗口到 / 达到最大等待)──▶ evaluating(room, N)
evaluating ──(不回)──▶ idle                                 # 记一条 situation 级 silence
evaluating ──(回，且 seen_through == N)──▶ speaking
evaluating ──(回，但 seen_through  > N)──▶ evaluating(room, N')   # 最多重评一次
speaking ──(说完，自己的发言进 Situation)──▶ idle
```

`armed` 期间同一房间来新消息：更新 `target`、重置安静计时（合并）。
其他房间来新消息：那个房间各自 `armed`，`Attention` 决定先评谁——有 obligation
的优先，其次最久未评估的。

`speaking` 期间来新消息：不再各自开决策，进 Situation，说完再看。
`InboxKind.STEER` 和 `AgentInbox.request_abort()` 已存在但无人调用；若说话期间
到达的 obligation 与正在说的内容冲突，这两个 seam 正是"打断并纳入"的入口。
后续可选，不是第一步。

`decide_participation` 的输入从"这一条 + 历史"变成"这一波 + 历史 + 我上次说了
什么 + 哪些是直接给我的"；输出多一个 `addressing`（我打算回应其中哪几条），
用于清 obligation，也让回复回合知道自己在回什么。

私聊不需要 `decide_participation`——每条都是 obligation，直接进 speaking。
它只借用合并与陈旧检查：短窗口收齐这一波，一个回合答完。

## 4. 情境从哪来

今天群决策的上下文来自每次调飞书历史 API。改成 Situation 后，每条群消息都已经
先 `observe` 进了本地消息表，房间的近期记录本地就有（带 `room_name`、`channel`、
`sender_id`）。建议：

- Situation 的 `burst` 与近期历史优先从本地消息表按 `room_key` 取；
- 飞书历史拉取只用来补洞（进程刚起、离线期间、bot 刚入群），且每波至多一次；
- 这样也消除了"5 句话 5 次外部 API 调用"。

本地消息表今天没有按房间查询的接口（`get_messages` 只按 limit），第 2 步要加一个
`room_key + cursor 范围` 的读法。这是纯读取，不引入新的存储。

## 5. 风险与对策

- **延迟**。安静窗口让回复晚一两秒。群里旁听式参与这是人类习惯；直接提问不能等，
  obligation 触发时窗口缩到只够接住"哦对了还有…"。语音已经证明"等说完再答"的
  体验是可接受的。
- **合并后漏掉一句**。决策输入是整波 + 历史；`addressing` 输出让漏掉可被观察。
- **群里别人抢答**。agent 评估时别人已经回答了那个问题。cursor 陈旧重评覆盖这一点。
- **私聊里"改主意"**。第 3 条推翻第 1 条时，今天会先回第 1 条。合并后一个回合看到
  全部三条，模型自然只回最终意图。
- **Obligation 丢失**。进程重启时 in-memory 的 obligations 会丢。第一步可接受
  （今天也没有），后续可从消息表里 `addressed_to_agent=True` 且其后没有 `ME`
  回复的记录重建，符合 GOAL.md "derived views are regenerable"。
- **私聊里两条独立问题**。合并回合里模型需要分别回答；今天的 prompt 已经能处理
  多问一答，`addressing` 让它明确。极端情况（第 1 条是长任务、第 2 条是闲聊）
  可以让决策输出"分两次说"，但先不做。

## 6. 放在哪里

不应做成飞书 adapter 内部优化：

- 私聊合并要同时覆盖飞书 p2p、微信 DM、Web/API，三处各写一份 debounce 会分叉；
- Situation / Intent 是 agent 的注意力，属于 mind，不属于任何一个 channel；
- 下一个接进来的渠道不应再抄一遍"每条消息一个 task"。

建议新增 `xagent/core/attention.py`（与 `inbox.py` 并列）。adapter 只做两件事：
把到达的消息喂进 `attention.hear(item)`，提供 `speak(room, text)` 回调。
`hear` 内部同步完成 Perception 并更新 Situation；评估与发言由 `Attention` 自己
的循环驱动。`AgentInbox` 继续是最后一道"一张嘴"锁，不动。

## 7. 行动建议

按侵入程度递增，每一步可独立落地、独立验证：

**第 0 步：把这份文档定为口径。** 统一 Perception / Situation / Intent /
Obligation 四个词。

**第 1 步：飞书群未 @ 分支做最小验证，不动核心。** 收到消息先 `observe`，再
`arm(chat_id)`；每群一个 debounce task，安静窗口到后把这一波消息拼成一个上下文，
调一次 `decide_participation`。@ 消息保持即时路径。改动局限在 `_route` 的未 @
分支和一个每群的小状态。用它确认安静窗口取值和决策 prompt 对整波输入的表现。
这一步就把"五句话五次决策"变成"五句话一次决策"。

**第 2 步：上提到核心。** 新增 `xagent/core/attention.py`：Situation / Intent /
Obligation 与状态机；`decide_participation` 接受情境形状的输入、返回 `addressing`；
基于 cursor 的陈旧重评；消息表增加按房间读取。飞书群切到这个 seam；飞书历史
API 降级为补洞。

**第 3 步：私聊接入。** 飞书 p2p、微信 DM、Web/API 的每条消息作为 obligation 进
`hear`，短窗口合并后一个回合答完。这一步的收益是"连发五条一次回"和"改主意不
再先回旧的"。

**验收指标**（第 1 步起即可记日志）：

- decisions / message（群，目标：一波内 ≪ 1）
- replies / burst（目标：≤ 1，除非决策明确要求分开答）
- 直接提问与私聊的首字延迟（不能明显比今天差；短窗口的上限要定死）
- "回应了一条已被别人回答 / 已被发言者自己推翻的消息"的次数

## 8. GOAL.md check

- **Identity** — 一个 agent 一个 Intent 槽位，等于一张嘴；不引入 persona 变化。
- **Multi-user** — Situation 按房间分，obligation 按发言者归属；不混人。
- **1:1 and group** — 同一机制：私聊是每条都是 obligation 的房间。
- **Memory / journal** — Perception 不变，日记仍是唯一记忆载体；silence 从每条
  一条变成每个情境一条。
- **Unified memory** — 不新增按用户切分的存储；Situation 是可丢弃的运行态。
- **Sharing** — 说不说、说什么仍由 agent 判断，只是判断对象从一句话变成一段情境。
- **Diary-anchored** — Situation / Obligation 是可重建的运行态投影，重建来源是
  消息表，不是第二个真相源。
- **Attribution / continuity** — 决策以 cursor 为锚，知道自己评估到哪、说到哪；
  `addressing` 让回复能追溯到具体哪几句。
- **Environment-aware** — 群消息先作为观察进入，再决定是否变成对话；观察与
  直接请求的区别由 Obligation 显式表达。
