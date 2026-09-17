# 上下文架构更新方案（Context Architecture Plan）

状态：设计定稿，待实施
适用范围：主对话 / tool loop、群聊参与决策、潜意识、working-context 压缩、日记 / 关系卡 / 笔记维护
不在范围内：模型 provider 协议本身、工具实现、渠道适配器的收发逻辑

---

## 0. 这份文档解决什么

当前六类模型调用共用一套 prompt 层（`PromptRegistry`），但上下文的**职责、选择、预算和信任边界**没有统一：

- 同一个群聊事件会以 observation、room context、触发消息三种形态同时进入模型输入。
- 没有全局输入预算；多个局部字符上限互不知晓，唯一的总量限制（`MAX_SYSTEM_PROMPT_LENGTH`）在生产链路不生效。
- 当前用户输入埋在历史里，`current_task` 只说"关注刚才那句话"却不含正文；飞书群聊里最后一条输入甚至是可信策略（`channel_instructions`）而不是用户输入。
- diary、working summary、raw history 三层长期覆盖同一段经历。
- 多人现场最需要披露边界，presence turn 却完全不注入关系信息。
- 大量语义冗余与迁移残留（死 API、死路径、文档与代码不一致）。

本方案的目标不是"删文案"，而是让每一条进入模型的信息都有**唯一归属、明确信任级别、可计量的成本**，然后在这个基础上收敛文案。

---

## 1. 现状问题清单（已逐条对照代码核实）

| # | 问题 | 位置 | 性质 |
|---|------|------|------|
| P1 | presence 路径先 `observe` 再回复，去重只比较 `timestamp+sender+role`，存储的是 ENVIRONMENT 角色 event，临时触发消息是 USER 角色，永远匹配不上 | `handlers/message.py:345-353`, `:555-567`；`world/inhabitant.py:432-440`；`feishu/adapter.py:613-639` | Bug |
| P2 | observation 正文 `Alice: xxx` + header `[from=Alice]` + room context `Alice HH:mm: xxx`，说话人出现三次 | `handlers/message.py:623-636` | 冗余 |
| P3 | `MAX_SYSTEM_PROMPT_LENGTH` 只在无人调用的 `build_instructions()` 里 warning | `handlers/message.py:1086` | 失效限制 |
| P4 | diary 8000 字符上限对首条 entry 无效；relationship card / 单条消息 / room event 无上限；tool result 单条 16000 但 turn 内累计无上限 | `handlers/memory.py:164-176`；`tooling/executor.py:169` | 预算漏洞 |
| P5 | hot window 名义 12，`safety_cap = max(12+6, 36)`，conversation 与 events 各一个 cap，后台 compaction 失败进入 60s cooldown 时 raw 可涨到 72 条 | `handlers/message.py:694-702`；`working_context.py:272-290` | 预算漏洞 |
| P6 | `current_task` 不含当前消息正文；scheduled turn 正文在 `recent_experience` | `config.py:361-383` | 结构 |
| P7 | `channel_instructions` 是适配器写死的可信策略，却以 `role=user` 放在 order 40（在 `current_task` 之后） | `prompt_registry.py:314-320` | 信任边界倒置 |
| P8 | working summary 与 `.journal_cursor` 独立推进，summary 是 `previous + 新滚出消息` 的永久滚动，与近两天 diary 长期重叠且有漂移 | `working_context.py:329-339`, `:375-379`；`handlers/memory.py:300-372` | 所有权不清 |
| P9 | presence turn 直接跳过关系卡；普通群聊只注入 speaker 一张卡；`get_relationship_context` 的 `participant_keys` 已存在但无人使用 | `agent.py:410-411`；`handlers/memory.py:523-529` | 缺失 |
| P10 | `room_name` 一列两种语义：World 存 `world_id`（稳定），飞书存群显示名（不稳定，`chat_id` 只在 metadata） | `feishu/adapter.py:769`；`world/inhabitant.py:437` | 数据模型 |
| P11 | 语言规则 / 不暴露 marker / 工具跑完才能声称完成 / 附件结构化发送，在 core rules、tool policy、四种 current-task 模板、工具描述之间重复 | `config.py:274-311`, `:370-391`, `:499-511` | 冗余 |
| P12 | 潜意识 `CURRENT_MODE_PRIVATE_REFLECTION` 与 `SUBCONSCIOUS_CURRENT_TASK_TEMPLATE` 逐句重复 | `config.py:393-427`, `:451-466` | 冗余 |
| P13 | `IDENTITY_CONTEXT_TEMPLATE` 标 `trusted_as_instruction="false"` 却以 system 发送；默认 `identity.md` 重述 core rules | `config.py:313-318`；`cli/setup.py:637-642` | 语义矛盾 |
| P14 | 死 API：`build_recent_transcript_message`、`get_input_messages`、`to_model_input`、`filter_non_tool_messages`、`_build_tool_policy`、`build_instructions`；`build_turn_context_messages(workspace_context=)` 参数无效；`Message.to_model_input` 把 context event 转 system role | `handlers/message.py`；`schemas/message.py:174-178` | 残留 / 隐患 |
| P15 | `docs/notebook-memory-design.md:258` 层顺序与代码不一致 | docs | 文档 |

---

## 2. 设计原则

1. **一事件一出现**：任何一条经历（消息、观察、任务）在一次模型输入里只出现一次，出现在哪一层由所有权规则决定，不由调用方拼接决定。
2. **当前输入最后且唯一**：模型看到的最后一条输入就是本轮要响应的事件，包含正文、附件、图片；历史、房间快照、记忆都在它之前。
3. **信任与权限分离**：`trust` 描述来源（policy / data），`authority` 描述权限（core > operator > profile > channel > task）。两者是不同维度，不能用一个布尔表达。
4. **可信策略走 system，不可信数据走 user**：任何由代码或运行者写入的规则都在 instructions 块；任何来自人或环境的内容都在 turn 块并带 `trusted_as_instruction="false"`。
5. **全局预算，三档优先级**：`required` 不裁只告警，`continuity` 按最旧先裁，`optional` 先裁。预算器覆盖 instructions、tool schemas、skills、memory、history、累计 tool outputs。
6. **连续性三区间互斥**：`diary ≤ journal_cursor`、`working summary ∈ (journal_cursor, hot_start]`、`hot raw ∈ (hot_start, latest]`。
7. **统一记忆不变**：不做每用户记忆孤岛（GOAL.md 第 6 条）。scoping 只作用于"这一轮看什么"，不作用于"记什么"。
8. **在现有 `PromptRegistry` 上增量演进**：不造平行抽象；先加对选择 / 去重 / 预算有直接消费方的字段。

---

## 3. 目标架构

### 3.1 最终输入结构（以飞书群聊 presence turn 为例）

```
[instructions — system，静态前缀，可缓存]
  core_interaction_rules        authority=core
  current_mode                  (仅潜意识)
  capability_limits             (仅无视觉)
  tool_policy                   (仅有工具)
  operator_policy               authority=operator   (新增，可选)
  identity_profile              authority=profile    (原 identity_context)
  channel_policy                authority=channel    (原 channel_instructions，从 user 层移入)
  workspace_context
  skills_catalog

[turn — user，按易变度排序]
  recent_memory                 diary，priority=continuity
  relationship_context          speaker 完整卡 + audience 精简卡，priority=optional(audience)/continuity(speaker)
  notebook_context              priority=optional
  recent_experience             working summary + hot raw（已排除 room snapshot 覆盖的条目和当前事件），priority=continuity
  room_snapshot                 房间实时快照（含 event ids），priority=continuity
  current_input                 当前事件正文 + 图片 + 附件 + ≤3 行模式指引，priority=required
```

`recent_experience` 中不再出现任何与 `room_snapshot` 同房间且 event id 在快照内的条目，也不出现 `current_input` 对应的事件。

### 3.2 各层所有权

| 层 | 覆盖范围 | 来源 | 谁负责边界 |
|----|----------|------|------------|
| recent_memory | 最近 N 天 diary（按天） | `MarkdownMemory` | MemoryHandler |
| working summary | `(journal_cursor, latest - hot_window]` 的 cursor 区间 | `WorkingContextCompactor` | Compactor 读 journal cursor |
| hot raw | `(latest - hot_window, latest]` 且不在 room snapshot、不是当前事件 | `MessageStorage` | `build_turn_context_messages` |
| room_snapshot | 当前房间最近 K 条（渠道提供） | 适配器 | 适配器给 ids，核心做 join |
| current_input | 恰好一条事件 | InboxItem | 核心 |

### 3.3 `PromptSection` 元数据（增量）

```python
@dataclass(frozen=True)
class PromptSection:
    name: str
    role: str
    order: int
    kind: str
    render: Callable[[PromptAssembleContext], str | RenderedSection]
    # 新增
    trust: Literal["policy", "data"] = "data"
    priority: Literal["required", "continuity", "optional"] = "continuity"
    authority: Literal["core", "operator", "profile", "channel", "task", "none"] = "none"
```

`RenderedSection` 允许渲染函数返回结构化结果（文本 + 可裁剪的条目列表 + provenance），供预算器按条目裁剪而不是按字符截断：

```python
@dataclass
class RenderedSection:
    text: str                                   # 完整渲染
    items: list[RenderedItem] | None = None     # 可裁剪条目（旧→新）
    provenance: dict = field(default_factory=dict)  # cursor range / diary dates / event ids
    def render_with(self, keep: int) -> str     # 保留最新 keep 条并重渲染
```

`authority` / `volatility` 一开始只记录到 manifest，不参与选择逻辑。

---

## 4. 数据模型变更

### 4.1 `Message`（`schemas/message.py`）

```python
source_event_id: Optional[str]   # 渠道原生事件 ID；无则 None
room_id: Optional[str]           # 稳定房间 ID；room_name 退回纯显示名
```

- 命名约定：`world:{world_id}:{seq}`、`feishu:{message_id}`、`weixin:{msg_id}`；CLI / Web 留空。
- `event_key` property：`source_event_id or (f"cursor:{storage_cursor}" if storage_cursor else None)`。
- 存储列是 `message_json`，pydantic 默认 `None`，**无需 SQLite migration**；旧行反序列化正常。
- 删除 `Message.to_model_input()`（P14）。

### 4.2 `InboxItem`（`core/inbox.py`）

新增 `source_event_id: Optional[str]`、`room_id: Optional[str]`、`room_snapshot: Optional[RoomSnapshot]`。`message_metadata()` 把前两者写进 metadata 以便 legacy 消费方读取。

### 4.3 `RoomSnapshot`（`core/formatters/context.py`）

```python
@dataclass(frozen=True)
class RoomContextEntry:
    speaker_label: str
    occurred_at: datetime
    text: str
    is_self: bool = False
    event_id: Optional[str] = None      # 新增
    speaker_key: Optional[str] = None   # 新增：channel:user_id，供 audience 使用

@dataclass(frozen=True)
class RoomSnapshot:
    room_id: str
    room_name: str
    entries: list[RoomContextEntry]
    present_labels: list[str]
    present_keys: list[str]             # channel:user_id
    @property
    def event_keys(self) -> set[str]
    def render(self) -> str             # 现有 format_room_context 的输出
```

`chat_events(room_context=...)` 同时接受 `str`（兼容）和 `RoomSnapshot`。字符串输入没有 event ids，退化为现有的 `_is_same_message` 去重。

### 4.4 公共 API 签名

```python
Agent.observe(..., room_id=None, source_event_id=None)
Agent.chat_events(..., room_id=None, source_event_id=None, room_context: str | RoomSnapshot = "")
Agent.chat(...)  # 同上透传
MessageHandler.store_user_message(..., room_id=None, source_event_id=None)
MessageHandler.store_context_event(..., room_id=None, source_event_id=None)
MessageHandler.store_model_reply(..., room_id=None)
```

全部为可选参数，默认行为不变。

---

## 5. Phase 1 — 事件身份、去重、current_input

**目标**：P1、P2、P6、P7、P10。可独立合并，不依赖其他阶段。

### 5.1 适配器传 id

- `world/inhabitant.py`
  - `_hear_utterance` / `_observe`：`room_id=self.world_id`，`room_name=self.world_name`，`source_event_id=f"world:{world_id}:{seq}"`；`context` 改为纯正文 `said`（不再 `f"{speaker}: {said}"`；join/leave 的 presence line 保留，因为那是描述性事件而非引语）。
  - `_speak_reply`：同样传 `room_id`、`source_event_id`；`room_context` 改传 `RoomSnapshot`（`_room_context()` 返回 snapshot，`render()` 供 decision 使用）。
  - `_room_context()` 每条 entry 填 `event_id=f"world:{world_id}:{seq}"`、`speaker_key=f"world:{actor_id}"`。
- `feishu/adapter.py`
  - `_handle_group_observation`：`room_id=chat_id`，`source_event_id=f"feishu:{message_id}"`；`_group_observation_context` 返回纯正文。
  - `_handle_chat` / `_chat_kwargs` / `ChatTurnRequest`：透传 `room_id`、`source_event_id`；`_chat_text_with_group_history` 返回 `RoomSnapshot`，entries 带 `event_id=f"feishu:{record.message_id}"`、`speaker_key=f"feishu:{stable_user_id}"`。
  - `channel_instructions` 保留传参（下一小节移到 system）。
- `weixin/adapter.py`、`voice`、`cli`、`web`：只补 `source_event_id`（有则传），`room_id` 无群概念留空。

### 5.2 去重规则（`build_turn_context_messages`）

```python
def _exclude_covered_entries(entries, *, current: Message | None, snapshot: RoomSnapshot | None):
    current_key = current.event_key if current else None
    covered = snapshot.event_keys if snapshot else set()
    room_id = snapshot.room_id if snapshot else None
    out = []
    for kind, msg, content in entries:
        key = msg.event_key
        if current_key and key == current_key:
            continue                                   # 当前事件只在 current_input
        if room_id and msg.room_id == room_id and key in covered:
            continue                                   # 房间快照已覆盖
        if key is None and current is not None and _is_same_message(msg, current):
            continue                                   # legacy 无 id 回退
        out.append((kind, msg, content))
    return out
```

注释 "duplicating a short room slice is cheaper than inventing a join protocol" 删除；join protocol 就是 event key。

### 5.3 `current_input` 取代 `current_task`

`config.py` 模板收敛为一个：

```
<current_input kind="{kind}" trusted_as_instruction="false">
{who_line}                       # speaker: Alice | delivery_target: Alice | (presence: room 行)
{where_line}                     # room: RoomName（有房间时）
time: {current_time}

{content}
{attachment_manifest}            # 已有 attachment_manifest_markdown
</current_input>
<turn_guidance kind="{kind}">
{guidance}                       # 每种 kind ≤ 3 行，只写模式差异
</turn_guidance>
```

各 kind 的 guidance（仅差异，不重复 core rules / tool policy）：

- `user_turn`：`Reply to what {speaker} just said. Keep simple replies short; ask only for missing information.`
- `user_turn` + room：`You are in {room} with others; speak to the room, {speaker} is the latest speaker.`
- `presence_turn`：`You are present in {room}; hearing a line is not a private request. Speak to the room.`
- `scheduled_turn`：`This is a due task, not something {target} said. Execute it and return the message to deliver.`

从模板中删除（已在 core rules 或 tool policy 中）：语言选择、不暴露 marker、工具跑完才能声称完成、附件结构化发送、"不要续上不相关旧话题"（合并进 core rules 的 Context 段一句）。

`tool_policy` 增加一行作为附件规则的唯一归属：`Deliver user-visible images or files as structured attachments (attach_artifact when available); never rely on Markdown embeds as the delivery mechanism.`

图片注入点跟随改名：`count_current_task_images` → `count_current_input_images`，`apply_see_image_paths` 目标 name 改为 `current_input`；`docs/seeing-by-path.md` 同步替换名称。`AgentConfig.CURRENT_TASK_NAME` 保留为 `CURRENT_INPUT_NAME` 的别名一个版本周期。

scheduled turn：`content` 用 `scheduled_task_display_content()` 取任务正文；存储的 USER 行照旧持久化（供 diary），但因 event_key 相同而从 `recent_experience` 排除。

### 5.4 `channel_instructions` 移入 system

- `prompt_registry.py`：删除 `KIND_TURN` 的 `channel_instructions` section；新增 `KIND_INSTRUCTIONS` 的 `channel_policy`（order 30，`trust="policy"`，`authority="channel"`），渲染为 `<channel_policy>...</channel_policy>`。
- `build_instruction_messages(..., channel_instructions="")` 新增参数；`_build_turn_context` 透传。
- 代价：instructions 前缀在飞书群聊与私聊之间不同，缓存命中分成两组。可接受。

### 5.5 验收测试（新增 `tests/test_context_dedupe.py`）

- `test_presence_turn_event_appears_exactly_once`：World 路径，先 `observe` 再 presence `chat_events`，断言最终 `input_messages` 拼接文本中 `anyone here?` 出现 1 次，speaker 名出现次数 ≤ 2（snapshot 一次 + current_input 一次）。
- `test_feishu_agent_decision_path_event_appears_exactly_once`：同上飞书路径。
- `test_room_snapshot_covered_entries_excluded_from_recent_experience`。
- `test_last_input_message_is_current_input`：任意 kind，最后一条 turn 消息 name 为 `current_input`（`turn_guidance` 合并进同一条消息，避免它成为最后一条）。
- `test_current_input_contains_message_body_for_all_kinds`：user / presence / scheduled。
- `test_channel_instructions_rendered_as_system_policy`。
- `test_legacy_string_room_context_still_dedupes_trigger`：向后兼容。
- `test_observation_content_has_no_speaker_prefix`。

---

## 6. Phase 2 — Context manifest（只观测，不改行为）

**目标**：为 Phase 3 定阈值；长期作为回归防线。

### 6.1 `ContextManifest`（新文件 `core/context_manifest.py`）

```python
@dataclass
class ManifestEntry:
    name: str; role: str; kind: str
    chars: int; est_tokens: int
    trust: str; priority: str; authority: str
    provenance: dict            # cursor range / dates / event ids / card keys
    dropped: list[str]          # 被裁条目的 provenance
    reason: str = ""            # "over_budget:optional" 等

@dataclass
class ContextManifest:
    turn_id: str; task_mode: str; inbox_kind: str
    entries: list[ManifestEntry]
    tools_chars: int; tools_count: int
    total_chars: int; total_est_tokens: int
    budget_tokens: int | None
    provider_shape: dict        # {"system_messages": n, "user_messages": n, "images": n}
```

### 6.2 采集点

- `PromptRegistry.assemble()` 返回 `(messages, entries)`；现有调用方通过 thin wrapper 保持 `list[dict]` 返回。
- `_build_turn_context` 组装 `ContextManifest`，附上 `tool_specs` 大小。
- `ObservabilityRuntime` 协议新增 `set_context_manifest(manifest)`（Noop 默认空实现；Langfuse 写入 span metadata）。
- `logger.debug` 输出一行汇总；`XAGENT_CONTEXT_MANIFEST=1` 时输出完整 JSON 到 `workspace/messages/.context_manifest.jsonl`（滚动保留最近 200 条）。
- 潜意识与 decision 调用同样产出 manifest（`task_mode` 区分）。

### 6.3 Token 估算器（`core/context_budget.py` 的一部分，Phase 3 复用）

```python
def estimate_tokens(text: str) -> int:
    ascii_n = sum(1 for c in text if ord(c) < 128)
    other_n = len(text) - ascii_n
    return ceil(ascii_n / 4 + other_n / 1.5)
```

可插拔：若安装 `tiktoken` 且 provider 为 OpenAI 系，使用精确计数。估算误差不阻塞方案。

### 6.4 验收

- `test_manifest_covers_every_assembled_section`。
- `test_manifest_totals_match_compiled_request`（与 `_build_chat_messages` 结果字符数一致，误差 0）。
- 用真实 fixture 跑一轮，记录基线：instructions、tool schemas、skills、diary、raw、room snapshot 的各自体量，写入本文件附录 A。

---

## 7. Phase 3 — 全局预算 `ContextBudget`

**目标**：P3、P4、P5。

### 7.1 配置（`AgentConfig` + `config.yaml: agent.context_budget_tokens`）

| 键 | 默认 | 说明 |
|----|------|------|
| `CONTEXT_BUDGET_TOKENS` | 32000 | 全局输入预算（不含模型输出）；用户可配 |
| `CONTEXT_BUDGET_RESERVE_RATIO` | 0.15 | 为 tool loop 内增长预留 |
| `MAX_CURRENT_INPUT_CHARS` | 12000 | 超出截断中段并注明 |
| `MAX_RAW_MESSAGE_CHARS` | 4000 | hot raw 单条 |
| `MAX_ROOM_ENTRY_CHARS` | 600 | room snapshot 单条 |
| `MAX_RELATIONSHIP_CARD_CHARS` | 1500 | speaker 完整卡 |
| `MAX_AUDIENCE_CARD_CHARS` | 300 | 精简卡 |
| `MAX_DIARY_ENTRY_CHARS` | 2500 | 单条 diary entry（修复首条不受限） |
| `MAX_TURN_TOOL_OUTPUT_CHARS` | 48000 | turn 内累计 tool result |
| 既有 | — | `MAX_TOOL_RESULT_CHARS=16000`、`MAX_SKILLS_CATALOG_CHARS=8000`、`NOTEBOOK_CONTEXT_MAX_CHARS=1500`、`WORKING_CONTEXT_SUMMARY_MAX_CHARS=1500`、`MEMORY_RECENT_MAX_CHARS=8000` 保留为层内上限 |

删除：`MAX_SYSTEM_PROMPT_LENGTH`、`build_instructions()`、`_budget_by_coverage` 中的 `safety_cap`（由预算器接管）。

### 7.2 算法（`core/context_budget.py`）

```
输入：instructions sections, turn sections(含 RenderedSection.items), tool_specs, budget
1. 单条硬保护：对每个 RenderedItem 应用对应 MAX_*_CHARS（截断中段，保留首尾，注明 omitted）。
2. 计算 required 总量 = core + mode + capability + tool_policy + operator + identity + channel + tools + current_input。
   若 required > budget*(1-reserve)：不裁，manifest.reason="required_over_budget"，warning 日志。
3. remaining = budget*(1-reserve) - required。
4. 依次装入 continuity（顺序：speaker card → working summary → hot raw(最新优先) → diary(最新优先) → room snapshot(最新优先)）：
   每层先整体尝试；放不下则按 items 从最旧开始丢，直到放下；hot raw 至少保留 MIN_HOT_RAW=4 条。
5. 装入 optional（顺序：notebook → audience cards → skills catalog → workspace_context）：放不下整层丢弃。
6. 被丢的 raw 条目若 cursor > covers_through（尚未被 summary 覆盖），在 recent_experience 顶部插入
   "[N earlier messages omitted; not yet summarized]"（复用现有 omitted note）。
7. 产出 manifest。
```

优先级里 `skills_catalog` 放在 optional 是刻意的：技能目录可由模型通过工具查询，不是每轮必需。

### 7.3 tool loop 内累计折叠（`tooling/executor.py` + `agent.py`）

每次 `handle_tool_calls` 追加结果后调用：

```python
def fold_tool_outputs(input_messages: list, *, max_total_chars: int) -> int:
    """从最早的 tool result 开始，把正文替换为一行占位，直到累计 ≤ 上限。
    保留 call/result 配对与 call_id；同时处理 chat 格式 (role=tool) 与 Responses 格式
    (type=function_call_output)。返回折叠条数。"""
```

占位文本：`[earlier tool output elided: {n} chars; re-run the tool if needed]`。折叠记录进 manifest。

### 7.4 `_budget_by_coverage` 调整

保留"`cursor > covers` 全部选出"的语义（这是与 summary 的所有权契约），删除 `safety_cap`；超量由 `ContextBudget` 第 4 步处理并注明。

### 7.5 验收（`tests/test_context_budget.py`）

- `test_total_never_exceeds_budget_when_required_fits`：构造超量 diary / raw / cards，断言 `total_est_tokens ≤ budget`。
- `test_required_sections_never_trimmed`。
- `test_trim_order_optional_before_continuity`。
- `test_hot_raw_keeps_minimum_entries`。
- `test_first_diary_entry_capped`（回归 P4）。
- `test_tool_output_folding_preserves_call_result_pairs`（chat 与 Responses 两种格式）。
- `test_unsummarized_dropped_rows_get_omitted_note`。
- `test_raw_growth_bounded_when_compactor_fails`（模拟 summarizer 持续抛错，断言 raw 不超预算而不是涨到 72 条，回归 P5）。

---

## 8. Phase 4 — 连续性三区间

**目标**：P8。

### 8.1 `WorkingContextState` 新字段

```python
covers_from_cursor: int = 0      # 生成时的 journal cursor
covers_through_cursor: int = 0   # 不变
```

旧文件无 `covers_from_cursor` → 0 → 首次运行触发重生成。

### 8.2 `WorkingContextCompactor`

- 构造新增 `journal_cursor_reader: Callable[[], Awaitable[int]]`；`MemoryHandler` 暴露 `journaled_through_cursor()`（读 `.journal_cursor`，已有 `_read_state_sync`）。
- 触发条件（任一满足）：
  - `latest - hot_window > covers_through + roll_slack`（原条件）
  - `journal_cursor > covers_from`（diary 前进了，summary 应收缩）
- 生成范围：`(max(journal_cursor, 0), latest - hot_window]`。
  - 范围为空 → summary = ""，写入状态。
  - 范围 ≤ `2 × DIARY_WRITE_BATCH` 条 → 一次性**重新生成**（不带 previous summary）。
  - 范围更大（diary 维护长期失败）→ 分段：先对前 `DIARY_WRITE_BATCH` 条生成，其余段用 `previous + 新段` 滚动，滚动只发生在 gap 内部，不跨 journal cursor。
- summarizer prompt 增加一句：`Everything before this window is already in the diary; do not restate it. Keep only what is still open or still needed to act.`

### 8.3 diary 侧配套

`build_diary_system_prompt` 增加一行：`If a commitment, path, identifier, or decision is still open at the end of this slice, keep it in the entry in a form you could act on later.` 这是 working summary 不再永久承载"任务态"后 diary 必须补上的责任（GOAL.md 第 8 条：diary 必须自足）。

### 8.4 `recent_experience` 渲染

`[Earlier working context]` 段头改为 `[Working context: since last diary entry]`，帮助模型理解它与 diary 的分界。

### 8.5 验收（扩展 `tests/test_working_context.py`）

- `test_summary_range_starts_after_journal_cursor`。
- `test_summary_shrinks_when_journal_cursor_advances`。
- `test_summary_regenerated_not_rolled_within_batch_size`（断言 summarizer 收到的 `previous_summary` 为空）。
- `test_large_gap_is_chunked_and_rolls_only_inside_gap`。
- `test_no_cursor_appears_in_both_summary_range_and_hot_raw`（三区间互斥）。
- `test_legacy_state_without_covers_from_triggers_regeneration`。

---

## 9. Phase 5 — Audience context

**目标**：P9。保持统一记忆库与统一 diary。

### 9.1 卡片精简渲染

- 约定：关系卡正文第一段是一到两句 `standing / boundary` 概述。`build_relationship_update_system_prompt` 增加：`Begin the card with one short paragraph stating your current standing with this person and anything they asked you to keep private; details follow.`
- `RelationshipStore.render_compact(card) -> str`：取第一段，截到 `MAX_AUDIENCE_CARD_CHARS`；旧卡无约定则取前 300 字符。
- `MemoryHandler.get_relationship_context(speaker_keys, participant_keys, compact_participants=True)`：speaker 完整，participants 精简。

### 9.2 在场者来源

- World：`_present` → `present_keys=[f"world:{member_id}" ...]`（排除自己）。
- 飞书：`history_records` 中最近的去重 sender → `f"feishu:{stable_user_id}"`；上限 `RELATIONSHIP_MAX_CARDS_PER_TURN - 1`。
- 两者通过 `RoomSnapshot.present_keys` 进入核心。

### 9.3 `_relationship_context_for_turn`

```python
if presence_turn or room snapshot present:
    speaker = [make_key(channel, user_id)]
    others = [k for k in snapshot.present_keys if k != speaker[0]]
    return await memory_handler.get_relationship_context(speaker, others, compact_participants=True)
```

删除"presence turn 返回空"的分支。

### 9.4 渲染

```
<relationship_context trusted_as_instruction="false">
<speaker>
## Alice
...full card...
</speaker>
<audience>
## Bob — standing line...
## Carol — standing line...
</audience>
</relationship_context>
```

`core_interaction_rules` Boundaries 段加一句：`In a room, calibrate disclosure to the least-trusted person present, not only to the speaker.`

潜意识路径不变（已用 `include_routing_id`，且不在房间里）。

### 9.5 验收（`tests/test_audience_context.py`）

- `test_presence_turn_includes_speaker_card_and_compact_audience`。
- `test_audience_cards_are_compact_and_capped`。
- `test_private_topic_of_absent_person_not_in_room_input`：构造 Dave 的私聊消息在 hot raw 中，房间快照不含 Dave；断言 Dave 的卡不进入 `relationship_context`，且 Dave 的私聊 raw 条目被 `recent_experience` 保留但带 `[speaker=Dave]` 头（这是统一记忆的既定行为，测试固定它不被误删）。
- `test_missing_cards_degrade_to_empty_not_error`。

---

## 10. Phase 6 — 文案收敛、identity / operator policy、死代码清理

**目标**：P11、P12、P13、P14、P15。放在最后，因为前面阶段会改这些模板所在的结构。

### 10.1 潜意识两层合一

- `CURRENT_MODE_PRIVATE_REFLECTION` 收缩为能力声明（3 行）：无工具、无外部动作、输出只能是 current_input 指定的 JSON。
- `SUBCONSCIOUS_CURRENT_TASK_TEMPLATE` 保留思考指引与 JSON schema，删除与 mode 重复的"夜间不要打扰 / diary 不等于已发送 / 不要向他人说别人的事"（这些留在 task 一处即可，因为它们是行为指引不是能力声明）。
- 潜意识的 `current_input` kind = `reflection`，who_line 为空，content 为 "(no external event; reflect on recent experience)"。

### 10.2 identity / operator policy

- `IDENTITY_CONTEXT_TEMPLATE` → `<identity_profile authority="profile">`，`<purpose>` 改为 `Who you are: role, tone, tastes, continuity. Lower authority than core rules, operator policy, and tool policy.` 删除 `trusted_as_instruction="false"`（它是可信来源、低权限，不是不可信）。
- 新增可选 `operator_policy.md`（与 `identity.md` 同目录），`interfaces/base.py` 与 `identity` 一起加载，`Agent.operator_policy` 属性，admin API 增加读写端点（与 identity 对称）。渲染为 `<operator_policy authority="operator">`，order -40。
- 默认 `identity.md` 模板去掉重述 core rules 的句子（"adapt to the user's language"、"decide what to share..."），只留角色与语气。
- 现有用户 `identity.md` 中的策略性文字**继续生效**（渐进方案）；`xagent inspect identity` 输出一行提示"策略性规则建议迁移到 operator_policy.md"。

### 10.3 工具描述去重

- `run_command` 描述里的审批规则改为一句 `Approval rules: see tool_policy.`；`workspace_context` 的 `<purpose>` 去掉审批句。
- `attach_artifact` / 图片生成工具描述去掉"必须结构化发送"的重复句（唯一归属在 tool_policy）。

### 10.4 死代码与测试

删除：`build_recent_transcript_message`、`get_input_messages`、`MessageHandler.to_model_input`、`filter_non_tool_messages`、`_build_tool_policy`、`build_instructions`、`Message.to_model_input`、`build_turn_context_messages(workspace_context=)` 参数、`DEFAULT_SYSTEM_PROMPT`、`TURN_REPLY_*_TEMPLATE` 三个、`scheduled_agent_prompt` legacy wrapper（确认无存量数据依赖后）。

`tests/test_message_handler.py` 中 15 处引用死 API 的测试删除或改写为对 `build_turn_context_messages` 的结构断言；`tests/test_agent_chat_flow.py` 2 处同理。

### 10.5 文档

- `docs/notebook-memory-design.md:258` 层顺序更新为本文件 3.1。
- `docs/seeing-by-path.md` 中 `current_task` → `current_input`。
- 本文件 3.1 成为层顺序的唯一权威描述；`prompt_registry.py` 模块 docstring 指向本文件。

---

## 11. 测试矩阵（结构性断言，不依赖 LLM 输出）

| 不变量 | 测试 | 阶段 |
|--------|------|------|
| 当前事件在最终输入中恰好出现一次 | `test_*_event_appears_exactly_once` | 1 |
| 最后一条 turn 消息是 `current_input` 且含正文 | `test_last_input_message_is_current_input` | 1 |
| 可信策略全部在 system 块 | `test_no_policy_text_in_user_role_messages`（扫描 turn 层不含 `<channel_policy>` / `<tool_policy>`） | 1 |
| room snapshot 覆盖的条目不在 recent_experience | `test_room_snapshot_covered_entries_excluded` | 1 |
| manifest 与实际编译请求字符数一致 | `test_manifest_totals_match_compiled_request` | 2 |
| 总 token ≤ 预算（required 放得下时） | `test_total_never_exceeds_budget_when_required_fits` | 3 |
| required 永不被裁 | `test_required_sections_never_trimmed` | 3 |
| compactor 失败时 raw 有界 | `test_raw_growth_bounded_when_compactor_fails` | 3 |
| tool 折叠保留 call/result 配对 | `test_tool_output_folding_preserves_call_result_pairs` | 3 |
| 三区间互斥 | `test_no_cursor_appears_in_both_summary_range_and_hot_raw` | 4 |
| diary 前进后 summary 收缩 | `test_summary_shrinks_when_journal_cursor_advances` | 4 |
| presence turn 有 speaker 卡 + 精简 audience | `test_presence_turn_includes_speaker_card_and_compact_audience` | 5 |
| 跨供应商语义一致 | `test_compiled_request_equivalent_across_providers`：同一 manifest 编译到 chat / Responses / Anthropic，system 文本相同、user 文本序列相同、图片数相同 | 6 |
| 潜意识输入不含工具 schema 且 current_input.kind=reflection | `test_subconscious_input_shape` | 6 |

---

## 12. 兼容与迁移

- **消息存储**：新增字段默认 `None`，旧行可读；旧行的 `event_key` 退化为 `cursor:{id}`，去重仍成立（同一行只有一个 cursor）。
- **`room_name` 旧值**：飞书旧行仍是显示名，新行 `room_name` 显示名 + `room_id=chat_id`。relationship key 不受影响（基于 `channel:user_id`）。
- **working context 文件**：缺 `covers_from_cursor` → 首次重生成，一次额外 LLM 调用。
- **公共 API**：`chat_events` / `observe` / `store_*` 只增可选参数；`room_context: str` 继续接受。
- **`AgentConfig.CURRENT_TASK_NAME`**：保留为别名一个版本，`grep` 外部用法后再删。
- **Langfuse**：span metadata 新增 `context_manifest`；`_turn_input_payload` 不变。
- **identity.md**：内容与加载方式不变，仅包装标签变化；`operator_policy.md` 不存在时不渲染任何内容。

---

## 13. 风险与回退

| 风险 | 缓解 | 回退 |
|------|------|------|
| Phase 1 去掉 raw 中的房间条目后，presence 回复失去快照之外的房间历史 | 快照 K=20（World）/ 飞书 history 拉取深度已覆盖 hot window；working summary 仍含更早内容 | 配置 `CONTEXT_DEDUPE_ROOM_ENTRIES=false` 恢复旧行为（仅一个版本周期） |
| Phase 3 预算过紧导致 diary 被大量裁掉 | 默认 32000 token 远高于当前实测总量；manifest 可见裁剪原因 | 调高 `agent.context_budget_tokens` |
| Phase 4 gap-only summary 丢失长期任务态 | diary prompt 补"open items"；notebook 承载可复用结论 | 状态文件加 `mode: rolling` 兜底开关 |
| Phase 5 关系卡首段约定对旧卡不成立 | `render_compact` 退化为前 300 字符；下次卡片更新自然迁移 | — |
| `channel_policy` 进 system 使缓存前缀分组 | 分组数 = 渠道策略种类（当前 2） | — |
| Anthropic 连续 user 消息 | 现有 `_coalesce_anthropic_messages` 已处理 | — |

每个阶段独立 PR、独立可回退；阶段之间只通过数据字段（`source_event_id` / `room_id` / `RenderedSection`）耦合。

---

## 14. GOAL.md 目标检查

- **Identity impact**：identity 从"伪不可信"变为明确的 profile 权限层；agent 自我声明的位置和内容不变。
- **Multi-user impact**：`source_event_id` / `room_id` / `speaker_key` 让"谁在哪说了什么"在数据层可追溯，不再依赖正文前缀。
- **1:1 and group coverage**：`current_input.kind` 与 `room_snapshot` 显式区分；audience context 只在有房间时出现。
- **Memory / journal perspective impact**：diary 写入源与格式不变；observation 正文去前缀后归因由 header 承担（journal transcript header 已带 `[from=]`）。
- **Unified-memory impact**：无用户隔离；hot raw 仍是全局流，只排除被更高层覆盖的条目。
- **Agent-governed sharing impact**：多人现场首次获得在场者关系边界信息；core rules 增加"按在场最低信任者校准"。
- **Diary-anchored memory-carrier impact**：working summary 收缩为 diary 未覆盖的 gap，diary 重新成为唯一长期承载；diary prompt 补 open-items 责任。
- **Attribution and continuity impact**：事件一次出现、三区间互斥、manifest 记录 provenance。

---

## 15. 待拍板事项

1. identity.md 中现有策略性文字：渐进（继续生效，提示迁移）还是立即降级。本方案按**渐进**实施。
2. `CONTEXT_BUDGET_TOKENS` 默认值 32000 是否合适；是否按 provider 给不同默认。
3. `skills_catalog` 归 optional（可被裁）是否接受；替代是归 continuity 但放在最后。
4. Phase 4 是否同时把 `DIARY_CONTEXT_DAYS`（按天）改为按 cursor 与 summary 对齐。本方案**不改**：diary 是叙事层，按天更符合其语义。

---

## 附录 A：体量基线（Phase 2 完成后填写）

| 层 | chars | est_tokens | 备注 |
|----|-------|------------|------|
| core_interaction_rules | | | |
| tool_policy | | | |
| identity_profile | | | |
| skills_catalog | | | |
| tool schemas（10 基础工具） | | | |
| tool schemas（+search/image/skills） | | | |
| recent_memory（2 天） | | | |
| relationship_context | | | |
| notebook_context | | | |
| recent_experience（hot 12 + summary） | | | |
| room_snapshot（20 条） | | | |
| current_input | | | |
