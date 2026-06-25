# Code Review Findings — feature-opt-subconscious-v2

> Generated 2026-06-25 | `feature-opt-subconscious-v2` vs `main`
> 10 finder angles + verify + sweep

## High Severity

### 1. WeixinAdapter/VoiceRuntime `deliver_subconscious_message` 不持久化消息

**文件:** `xagent/integrations/weixin/adapter.py:672`, `xagent/interfaces/voice/runtime.py:535`

Subconscious loop 投递消息后，Weixin 和 Voice 适配器没有调用 `store_model_reply`，导致消息发送给用户后不会存入 agent 的消息历史。agent 后续检索消息时没有这条记录，可能重复发送相同内容或丢失对话连续性。

对比：Feishu adapter (`feishu/adapter.py:2330`) 和 Server (`app.py:480`) 都正确调用了 `store_model_reply`。

---

### 2. 投递失败时空 `internal_content` 导致 `external_content` 静默丢失

**文件:** `xagent/core/runtime/subconscious.py:530`

```python
except Exception:
    self._logger.warning("Subconscious delivery failed; recording internal thought", exc_info=True)
    if internal_content:
        await self._write_internal_thought(internal_content)
```

当 LLM 返回 `"internal_content": ""` 但 `"external_content": "Hello!"` 时，`internal_content` 为 falsy，`_write_internal_thought` 不会被调用。LLM 精心组合的外发消息永久丢失。

---

### 3. 无重试/持久化机制 — 瞬时故障导致消息永久丢失

**文件:** `xagent/core/runtime/subconscious.py:294`

旧系统在投递前将 thought 作为 JSON task 文件写入磁盘，AsyncTaskScheduler 在后续 tick 中重试。新系统调用 `delivery_sink` 仅一次，失败后直接降级为内部独白。瞬时网络错误或 API 限流导致用户永远收不到该消息。

---

### 4. 空字符串 `recipient_hint` 回退到最近联系人

**文件:** `xagent/core/runtime/subconscious.py:542`

```python
hint = str(recipient_hint or "").strip().lower()
if hint:  # "" 为 falsy，跳过匹配
    ...
return max(contacts, key=lambda c: c.last_seen)  # 回退到最近联系人
```

LLM 设置 `"recipient_hint": ""` 意图表示"无特定接收人"，但代码将其等同于 `None`，将消息投递给最近联系人。

---

### 5. 非 pure-thought 模式下，模型同时产出文本和工具调用时文本被丢弃

**文件:** `xagent/core/runtime/subconscious.py:313`

当模型在 streaming 中先输出推理文本再调用工具时，累积的 `text_parts` 已拼接为 `text`，但因 `tool_calls` 为 truthy 且非 `pure_thought`，`text` 变量被丢弃。下一轮迭代仅从工具结果开始，丢失模型的中间推理。可能导致迭代浪费。

---

## Medium Severity

### 6. FeishuAdapter `deliver_subconscious_message` 持久化时缺少 `channel` 参数

**文件:** `xagent/integrations/feishu/adapter.py:2330`

`store_model_reply` 调用没有传 `channel=`。存储的消息 `channel=None`，后续 transcript 格式化时跳过 `[channel=...]` 标记。对比 `app.py:494` 正确传递了 `channel=delivery.recipient.channel`。

---

### 7. `_is_all_internal_monologue` 遗漏旧格式内部消息

**文件:** `xagent/core/handlers/memory.py:244`

新检查使用 `r.get("role") == "assistant"`，但升级前的内部独白消息使用 `role=ENVIRONMENT`。旧消息不会被识别为内部独白，导致 diary compressor 为其生成独立的 LLM 日记条目，浪费调用并碎片化日志。

---

### 8. Server `deliver_subconscious_message` 缺少 try-except

**文件:** `xagent/interfaces/server/app.py:479`

`store_model_reply` 调用没有 try-except 包裹。数据库瞬态错误会向上传播到 `_route_subconscious_thought`，导致 WebSocket 订阅者收不到通知。Feishu adapter 正确包裹了 try-except。

---

### 9. `_is_internal_monologue` 检测语义扩大

**文件:** `xagent/core/handlers/message.py:2032`

检测从 `metadata.event_type == "internal_monologue"` 变为 `role == ASSISTANT`。任何 `CONTEXT_EVENT + ASSISTANT` 都被归类为内部独白，无论 `event_type` 是什么。将来新增的非内部独白 ASSISTANT context_event 会被错误格式化。

---

### 10. `MessageStorage.__init__` 破坏性 API 变更

**文件:** `xagent/components/message/sqlite_messages.py:32`

`path` 参数从 `Optional[str] = None`（有默认值 `DEFAULT_PATH`）变为必需的 `str`。任何调用 `MessageStorage()` 无参的外部代码会抛出 `TypeError`。

---

### 11. `store_user_message` 默认 `recipient_id` 为 `"agent"`

**文件:** `xagent/core/handlers/message.py:78`

```python
msg.recipient_id = recipient_id or "agent"
```

所有不传 `recipient_id` 的调用方现在都会得到 `"agent"`。之前为 `None`。下游检查 `recipient_id is None` 来区分直接消息的逻辑会失效。

---

### 12. Feishu 子意识投递先发送后持久化 — 存储失败导致消息记录缺失

**文件:** `xagent/integrations/feishu/adapter.py:2319`

`_send_markdown` 成功后 `store_model_reply` 失败（被 try-except 捕获），用户收到了消息但 agent 历史中没有记录。Server 通过先持久化后广播避免了此问题。

---

## Low Severity

### 13. `json.loads` 仅捕获 `JSONDecodeError`，未捕获 `RecursionError`

**文件:** `xagent/core/runtime/subconscious.py:404`

LLM 生成深度嵌套的畸形 JSON（如 `{{{{{{{{{{{{{{{{...}}}}}}}}}}}}}}}}}}` 会触发 `RecursionError`，绕过 `JSONDecodeError` 处理器，导致整个子意识循环中断。

---

### 14. `_is_appropriate_time` start==end 边界情况

**文件:** `xagent/core/runtime/subconscious.py:586`

设置 `SUBCONSCIOUS_QUIET_HOURS_START=0, SUBCONSCIOUS_QUIET_HOURS_END=0` 期望 24 小时安静，实际结果 `not (0 <= hour < 0)` 对所有 hour 都为 `True`，相当于"始终活跃"。

---

### 15. 联系人文件路径在 adapter 和 SubconsciousLoop 之间可能不一致

**文件:** `xagent/core/runtime/heartbeat.py:159`, `xagent/integrations/feishu/adapter.py:218`

FeishuAdapter 和 SubconsciousLoop 通过不同逻辑计算 `_contacts_file` 路径，可能导致写入和读取不同的文件。SubconsciousLoop 看不到实际存在的联系人，所有 worthy thought 都降级为内部独白。
