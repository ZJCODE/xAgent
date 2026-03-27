# 记忆系统优化

## 1. 当前记忆系统架构

```
用户消息
    ↓
MessageBuffer（短期缓冲）
    ↓（达到阈值或关键词触发）
LLM 提取（extract_memories_from_content）
    ↓
向量检索（查找相关旧记忆）
    ↓
LLM 合并（merge_memories）
    ↓
VectorStore 存储（ChromaDB/Upstash）

检索时：
用户查询
    ↓
LLM 查询预处理（preprocess_query）
    ↓
向量检索（多查询变体）
    ↓
返回相关记忆
```

### 1.1 架构优点

- 基于 LLM 的智能提取，记忆质量高
- 多查询变体提升检索召回率
- 支持本地（ChromaDB）和云端（Upstash）两种向量存储
- 记忆合并避免重复存储

### 1.2 已识别的问题

---

## 2. 记忆提取质量问题

### 2.1 System Prompt 过长且可能失效

`extract_memories_from_content` 的 System Prompt 超过 2000 字，这会导致：
- Token 消耗高
- 模型在长提示中容易"遗忘"早期规则
- 结构化程度低，难以维护

**改进方案**：精炼提示词并使用少样本示例（Few-shot）：

```python
MEMORY_EXTRACTION_SYSTEM_PROMPT = """You are a memory extraction system. Extract only facts worth remembering long-term.

Memory types:
- PROFILE: User traits, preferences, habits (e.g., "User exercises daily at 7 AM")
- EPISODIC: Specific events/plans with dates (e.g., "User has dinner 2025-08-25 at 8 PM")

Rules:
1. Extract ONLY genuinely important information
2. Each memory = ONE specific topic
3. Use original conversation language
4. For multiple users, prefix each memory with user identifier
5. Include dates (YYYY-MM-DD) for time-bound events

BAD: "User likes various outdoor activities and sometimes goes fishing but doesn't always enjoy it"
GOOD: "User prefers hiking and outdoor sports"
GOOD: "User does not enjoy fishing"
"""
```

### 2.2 记忆过期机制缺失

当前记忆系统没有过期机制。存储的 EPISODIC 记忆（如"用户今晚有饭局"）会永久保留，未来检索时仍会出现，产生误导：

**改进方案**：为记忆添加时效性元数据：

```python
class MemoryTTL(Enum):
    PERMANENT = -1      # 永久（个人偏好、背景信息）
    LONG_TERM = 30      # 30 天（习惯、定期事件）
    SHORT_TERM = 7      # 7 天（近期计划）
    EPHEMERAL = 1       # 1 天（今日计划）

def _create_base_metadata(
    self, 
    user_id: str, 
    memory_type: str,
    ttl_days: Optional[int] = None
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    metadata = {
        "user_id": user_id,
        "created_at": now.isoformat(),
        "created_timestamp": now.timestamp(),
        "memory_type": memory_type,
    }
    
    if ttl_days and ttl_days > 0:
        expire_at = now + timedelta(days=ttl_days)
        metadata["expire_timestamp"] = expire_at.timestamp()
    
    return metadata

async def retrieve(self, user_id: str, query: str, ...) -> List[Dict]:
    """Retrieve memories, filtering out expired ones."""
    now = datetime.now(timezone.utc).timestamp()
    
    meta_filter = {
        "$and": [
            {"user_id": user_id},
            # 过滤已过期的记忆
            {
                "$or": [
                    {"expire_timestamp": {"$exists": False}},
                    {"expire_timestamp": {"$gt": now}}
                ]
            }
        ]
    }
    ...
```

---

## 3. 记忆检索质量问题

### 3.1 记忆注入位置不当

当前实现将记忆直接注入 System Prompt：

```python
# AgentConfig.DEFAULT_SYSTEM_PROMPT
"- Retrieve relevant memories for user: {retrieved_memories}\n\n"
```

问题：
1. System Prompt 的记忆对 LLM 的影响权重较低
2. 记忆和系统指令混在一起，可能相互干扰
3. 格式不直观，LLM 难以清晰引用

**改进方案**：将记忆作为独立消息注入（Memory-Augmented Conversation）：

```python
async def _build_messages_with_memory(
    self,
    base_messages: List[dict],
    retrieved_memories: List[dict],
    user_message: str
) -> List[dict]:
    """Inject memories as a dedicated context message."""
    
    if not retrieved_memories:
        return base_messages
    
    memory_content = self._format_memories_as_context(retrieved_memories)
    
    # 将记忆作为最后一个 user 消息之前的上下文插入
    messages_with_memory = base_messages[:-1] + [
        {
            "role": "user",
            "content": f"[Relevant background about you]:\n{memory_content}\n\n[User message]: {user_message}"
        }
    ]
    
    return messages_with_memory

def _format_memories_as_context(self, memories: List[dict]) -> str:
    """Format memories clearly for model consumption."""
    lines = []
    for memory in memories:
        mem_type = memory.get("metadata", {}).get("memory_type", "").upper()
        content = memory.get("content", "")
        lines.append(f"[{mem_type}] {content}")
    return "\n".join(lines)
```

### 3.2 Embedding 模型固定为 OpenAI

当前向量存储硬编码使用 `text-embedding-3-small`，用户无法自定义 Embedding 模型，限制了在私有/本地部署场景中的使用。

**改进方案**：Embedding 模型可插拔：

```python
from abc import ABC, abstractmethod

class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""
    
    @abstractmethod
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for given texts."""
        pass
    
    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Return the dimensions of the embedding vectors."""
        pass

class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model: str = "text-embedding-3-small"):
        self.model = model
        self.client = AsyncOpenAI()
    
    async def embed(self, texts: List[str]) -> List[List[float]]:
        response = await self.client.embeddings.create(
            model=self.model, input=texts
        )
        return [item.embedding for item in response.data]
    
    @property
    def dimensions(self) -> int:
        return 1536  # text-embedding-3-small

class OllamaEmbeddingProvider(EmbeddingProvider):
    """Use Ollama for local embedding generation."""
    
    def __init__(self, model: str = "nomic-embed-text", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url
    
    async def embed(self, texts: List[str]) -> List[List[float]]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts}
            )
        return response.json()["embeddings"]

class VectorStoreLocal:
    def __init__(
        self,
        path: str = None,
        collection_name: str = "xagent_memory",
        embedding_provider: Optional[EmbeddingProvider] = None
    ):
        self.embedding_provider = embedding_provider or OpenAIEmbeddingProvider()
```

---

## 4. 记忆存储效率问题

### 4.1 每次存储都触发合并 LLM 调用

即使没有相关旧记忆，也会在 `store()` 中调用向量检索（虽然不会触发 LLM 合并），但这增加了延迟：

```python
async def store(self, user_id: str, content: str) -> str:
    extracted_memories = await self.llm_service.extract_memories_from_content(content)
    
    # 总是执行向量查询
    related_vector_docs = await self.vector_store.query(...)
    
    # 只有有相关记忆时才调用 LLM 合并
    if all_related_memories:
        merged_result = await self.llm_service.merge_memories(...)
```

**这个逻辑已经相对合理**，但可以进一步优化：

```python
async def store(self, user_id: str, content: str) -> str:
    # 并发执行提取和相关查询准备（查询文本需要先提取，所以无法完全并行）
    extracted_memories = await self.llm_service.extract_memories_from_content(content)
    
    if not extracted_memories.memories:
        return ""
    
    query_texts = [m.content for m in extracted_memories.memories]
    
    # 查询和最终存储可以结构化优化
    related_vector_docs = await self.vector_store.query(
        query_texts=query_texts,
        n_results=2,
        meta_filter={"user_id": user_id}
    )
    
    # 快速路径：没有相关记忆，直接存储
    if not related_vector_docs:
        return await self._batch_store(user_id, extracted_memories.memories)
    
    # 慢路径：有相关记忆，执行 LLM 合并
    all_related = self._deduplicate_vector_docs(related_vector_docs)
    merged = await self.llm_service.merge_memories(extracted_memories, all_related)
    
    # 并发执行：存储新记忆 + 删除旧记忆
    old_ids = [doc.id for doc in related_vector_docs]
    await asyncio.gather(
        self._batch_store(user_id, merged.memories),
        self.delete(old_ids)
    )
    
    return "stored"
```

### 4.2 MessageBuffer 的 `add_messages` 逻辑

`add_messages` 在 `message_buffer` 中无法高效地进行批量操作追踪。

**建议改进**：

```python
class MessageBufferLocal:
    """Thread-safe local message buffer with efficient batching."""
    
    def __init__(self, max_messages: int = 100):
        self.max_messages = max_messages
        self._buffers: Dict[str, List[dict]] = {}
        self._lock = asyncio.Lock()
    
    async def add_messages(
        self, 
        user_id: str, 
        messages: Union[dict, List[dict]]
    ) -> int:
        """Add one or more messages. Returns new total count."""
        if isinstance(messages, dict):
            messages = [messages]
        
        async with self._lock:
            if user_id not in self._buffers:
                self._buffers[user_id] = []
            
            self._buffers[user_id].extend(messages)
            
            # Auto-trim if exceeds max
            if len(self._buffers[user_id]) > self.max_messages:
                self._buffers[user_id] = self._buffers[user_id][-self.max_messages:]
            
            return len(self._buffers[user_id])
```

---

## 5. Meta 记忆系统改进

### 5.1 当前 Meta 记忆的触发机制

`extract_meta` 需要用户手动调用，没有自动触发机制：

```python
# 用户需要手动调用
await memory.extract_meta(user_id="user1", days=1)
```

**改进方案**：基于时间的自动 Meta 记忆生成：

```python
class MemoryStorageBasic:
    def __init__(self, ...):
        self._last_meta_extraction: Dict[str, float] = {}
        self.meta_extraction_interval_hours = 24  # 每 24 小时自动提取一次
    
    async def _auto_extract_meta_if_needed(self, user_id: str) -> None:
        """Automatically extract meta memories on schedule."""
        last_extraction = self._last_meta_extraction.get(user_id, 0)
        interval = self.meta_extraction_interval_hours * 3600
        
        if time.time() - last_extraction >= interval:
            task = asyncio.create_task(
                self._safe_extract_meta(user_id)
            )
            task.add_done_callback(
                lambda t: self.logger.error("Meta extraction failed: %s", t.exception())
                if t.exception() else None
            )
            self._last_meta_extraction[user_id] = time.time()
    
    async def _safe_extract_meta(self, user_id: str) -> None:
        try:
            await self.extract_meta(user_id, days=1)
        except Exception as e:
            self.logger.error("Auto meta extraction failed for %s: %s", user_id, e)
```

---

## 6. 记忆系统架构升级建议

### 6.1 三层记忆架构

参考认知科学中的记忆模型，建议实现三层记忆架构：

```
┌─────────────────────────────────────┐
│          Working Memory              │  ← 当前对话上下文（MessageStorage）
│    （最近 N 条对话，极快访问）         │
└────────────────┬────────────────────┘
                 ↓ 定期压缩
┌─────────────────────────────────────┐
│         Episodic Memory              │  ← 向量存储（ChromaDB/Upstash）
│    （具体事件和偏好，向量检索）        │
└────────────────┬────────────────────┘
                 ↓ 周期性抽象
┌─────────────────────────────────────┐
│         Semantic Memory              │  ← Meta 记忆（高层摘要）
│    （用户画像，行为模式，价值观）      │
└─────────────────────────────────────┘
```

### 6.2 记忆重要性评分

为记忆添加重要性评分，用于优先级排序：

```python
class MemoryImportanceScorer:
    """Score memory importance for retrieval prioritization."""
    
    IMPORTANCE_FACTORS = {
        "recency": 0.3,      # 越新越重要
        "frequency": 0.3,    # 被多次提及越重要
        "semantic_match": 0.4 # 语义相关度
    }
    
    def score(
        self,
        memory: dict,
        query: str,
        semantic_score: float,
        now: float
    ) -> float:
        """Calculate composite importance score."""
        
        # Recency score (exponential decay)
        created_ts = memory.get("metadata", {}).get("created_timestamp", 0)
        age_days = (now - created_ts) / 86400
        recency_score = math.exp(-age_days / 30)  # 半衰期 30 天
        
        # Frequency score (how many times this memory was retrieved)
        recall_count = memory.get("recall_count", 1)
        frequency_score = min(recall_count / 10, 1.0)  # Cap at 10 recalls
        
        return (
            self.IMPORTANCE_FACTORS["recency"] * recency_score +
            self.IMPORTANCE_FACTORS["frequency"] * frequency_score +
            self.IMPORTANCE_FACTORS["semantic_match"] * semantic_score
        )
```

---

## 7. 记忆系统优先级汇总

| 优化项 | 收益 | 难度 | 优先级 |
|--------|------|------|--------|
| 记忆 TTL（过期机制） | 防止记忆污染 | 低 | P1 |
| 快速路径（无相关记忆直接存储） | 提升存储效率 | 低 | P1 |
| Embedding 模型可插拔 | 支持本地部署 | 中 | P2 |
| 记忆注入位置优化 | 提升记忆利用率 | 低 | P2 |
| Meta 记忆自动触发 | 改善用户体验 | 低 | P2 |
| 三层记忆架构 | 更精准的记忆管理 | 高 | P3 |
| 记忆重要性评分 | 提升检索质量 | 中 | P3 |
| System Prompt 精炼 | 降低 Token 成本 | 低 | P3 |
