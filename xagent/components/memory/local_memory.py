import chromadb
import chromadb.utils.embedding_functions as embedding_functions
import logging
import os
import uuid
import re
from typing import List, Optional, Dict, Any, Set, Tuple
from datetime import datetime, timedelta, timezone
import dotenv
from pathlib import Path

from .base_memory import MemoryStorageBase
from .llm.llm_service import MemoryLLMService

dotenv.load_dotenv(override=True)

class MemoryStorageLocal(MemoryStorageBase):
    """Lo
    
    Args:
        path: Path to ChromaDB storage directory. Defaults to ~/.xagent/chroma
        collection_name: Name of the ChromaDB collection. Defaults to 'xagent_memory'
        memory_threshold: Number of messages to trigger long-term storage. Defaults to 10
        keep_recent: Number of recent messages to keep after storage. Defaults to 2
        max_scan_length: Maximum length of text content to scan for keywords. Defaults to 5000
    """


    # 最大扫描长度
    _MAX_SCAN_LENGTH = 5000
    # 多层级关键字配置，按重要性和常见性分级，避免不必要的大量监测
    _TRIGGER_KEYWORDS = [
        # 第一层：高频且重要的记忆触发词 - 对话中经常出现且需要优先记忆
        [
            # 记忆和记录指令 - 用户明确要求存储，最高优先级
            r'记住(?:这|那|一下)?', r'记得', r'请记住', r'帮我记住', r'别忘了',
            r'记一下', r'记下来', r'记录一下', r'保存一下', r'存下来', r'备忘一下', r'写下来',
            r'\bremember\s+this\b', r'\bremember\s+that\b', r'\bplease\s+remember\b', r'\bdon\'?t\s+forget\b',
            r'\bnote\s+this(?:\s+down)?\b', r'\bmake\s+a\s+note\b', r'\bsave\s+this\b', 
            r'\bkeep\s+this\b', r'\blog\s+this\b', r'\bstore\s+this\b', r'\bwrite\s+this\s+down\b',
            
            # 偏好表达 - 对话中高频出现的个人偏好，按强度排序
            r'我最喜欢', r'我最爱', r'我讨厌', r'我不喜欢', r'我最不喜欢', r'我痛恨', r'我厌恶',
            r'我喜欢', r'我爱(?!上)', r'我比较喜欢', r'我更喜欢', r'我偏爱', r'我钟爱',
            r'my\s+favorite', r'i\s+love', r'i\s+hate', r'i\s+really\s+like', r'i\s+dislike', r'i\s+despise',
            r'\bi\s+like\b', r'i\s+enjoy', r'i\s+would\s+prefer', r'i\s+rather\s+like', r'i\s+adore',
            
            # 习惯和特征 - 高频且稳定的个人特征
            r'我习惯', r'我总是', r'我从不', r'我一般', r'我常(?:常)?', r'我经常', r'我向来',
            r'i\s+usually', r'i\s+always', r'i\s+never', r'i\s+typically', r'i\s+tend\s+to', r'i\s+often', r'i\s+habitually',
            
            # 需求和目标 - 用户的重要需求
            r'我需要', r'我想要', r'我希望', r'我的目标是', r'我打算', r'我计划',
            r'i\s+need', r'i\s+want', r'i\s+wish', r'my\s+goal\s+is', r'i\s+plan\s+to', r'i\s+intend\s+to'
        ],
        
        # 第二层：重要但中等频率的个人信息
        [
            # 关键个人信息 - 基本身份信息，中等重要性
            r'我住在', r'我来自', r'我在(?:.*?)工作', r'我的职业是', r'我是(?:.*?)专业的',
            r'i\s+live\s+in', r'i\s+am\s+from', r'i\s+work\s+(?:at|for)', r'my\s+job\s+is', r'i\s+major\s+in',
            
            # 能力和技能 - 用户的专业能力
            r'我会(?:.*?)', r'我擅长', r'我精通', r'我不会', r'我不擅长', r'我的专长是',
            r'i\s+can', r'i\s+am\s+good\s+at', r'i\s+excel\s+at', r'i\s+cannot', r'i\s+am\s+not\s+good\s+at', r'my\s+specialty\s+is',
            
            # 个人状态和倾向 - 性格特征描述
            r'我是(?:一个)?(?:.*?)的人', r'我属于', r'我倾向于', r'我的性格(?:比较)?(?:.*?)',
            r'i\s+am\s+(?:a|an)\s+(?:.*?)\s+person', r'i\s+prefer', r'i\s+belong\s+to', r'my\s+personality\s+is',
            
            # 核心身份信息 - 姓名等关键标识，重要但低频
            r'我叫', r'我的名字是', r'你可以叫我', r'请叫我', r'称呼我', r'我姓',
            r'my\s+name\s+is', r'call\s+me', r'you\s+can\s+call\s+me', r'i\s+am\s+called', r'my\s+surname\s+is',

            # 年龄性别等信息
            r'我(?:是|的)?\d{1,3}岁', r'我(?:是|的)?男(?:性)?', r'我(?:是|的)?女(?:性)?',
            r'i\s+am\s+\d{1,3}\s+years\s+old', r'i\s+am\s+male', r'i\s+am\s+female',

            # 重要约束和禁忌 - 用户的限制
            r'我不能', r'我禁止', r'我不允许', r'我拒绝', r'我坚决不',
            r'i\s+cannot', r'i\s+refuse\s+to', r'i\s+will\s+not', r'i\s+am\s+not\s+allowed\s+to'
        ],
        
        # 第三层：低频但重要的信息
        [
            # 重要经历和事件 - 用户提到的重要经历
            r'我曾经', r'我以前', r'我经历过', r'对我来说很重要的是',
            r'i\s+used\s+to', r'i\s+once', r'i\s+have\s+experienced', r'what\'?s\s+important\s+to\s+me\s+is',
            
            # 情感状态 - 重要的情感表达（但不要太频繁触发）
            r'我很(?:开心|高兴|兴奋|难过|伤心|生气|愤怒|担心|焦虑)', r'让我(?:开心|难过|生气|担心)的是',
            r'i\s+am\s+(?:very\s+)?(?:happy|excited|sad|angry|worried|anxious)', r'what\s+makes\s+me\s+(?:happy|sad|angry)\s+is',
            
            # 日常信息 - 较弱的日常表达
            r'我有时', r'我偶尔', r'我可能',
            r'i\s+sometimes', r'i\s+occasionally', r'i\s+might'
        ]
    ]
    
    def __init__(self, 
                 path: str = None,
                 collection_name: str = "xagent_memory",
                 memory_threshold: int = 10,
                 keep_recent: int = 2,
                 max_scan_length: int = 5000):
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Use default path if none provided
        if path is None:
            path = os.path.expanduser('~/.xagent/chroma')
            self.logger.info("No path provided, using default path: %s", path)
        
        # Ensure the directory exists
        Path(path).mkdir(parents=True, exist_ok=True)
        
        # Initialize LLM service
        self.llm_service = MemoryLLMService()

        # Initialize OpenAI embedding function
        self.openai_ef = embedding_functions.OpenAIEmbeddingFunction(
            model_name="text-embedding-3-small"
        )
        
        # Initialize ChromaDB client and collection
        self.chroma_client = chromadb.PersistentClient(path=path)
        self.collection = self.chroma_client.get_or_create_collection(
            name=collection_name,
            embedding_function=self.openai_ef
        )
        
        # Initialize user's temporary message storage
        self._user_messages = {}
        
        # Memory management configuration
        self.memory_threshold = memory_threshold  # Store to long-term memory when reaching this many messages
        self.keep_recent = keep_recent  # Keep this many most recent messages after storage
        
        # 扫描长度上限配置，避免极长消息带来的性能抖动
        self.max_scan_length = self._MAX_SCAN_LENGTH

        # 编译正则表达式模式，提高匹配性能
        self._compiled_patterns = self._compile_keyword_patterns()
        
        self.logger.info("LocalMemory initialized with collection: %s, max scan length: %d", 
                        collection_name, self.max_scan_length)
    
    async def add(self,
                  user_id: str,
                  messages: List[Dict[str, Any]]
                  ):
        """
        Add new messages to user's session memory.
        When messages reach a threshold, or if a keyword is detected, trigger storage to long-term memory and keep only the latest 2 messages.
        
        Args:
            user_id: User identifier 
            messages: List of message dictionaries with 'role', 'content', etc.
        """
        if not messages:
            self.logger.debug("No messages provided for user %s", user_id)
            return

        if user_id not in self._user_messages:
            self._user_messages[user_id] = []

        # Add new messages to user's temporary storage
        self._user_messages[user_id].extend(messages)
        message_count = len(self._user_messages[user_id])

        self.logger.debug("Added %d messages for user %s, total messages: %d", 
                         len(messages), user_id, message_count)

        # 只监测 role 为 'user' 的消息是否有关键词触发，逆序遍历（从最新一条开始）
        keyword_triggered = False
        trigger_tier = ""
        
        for msg in reversed(messages):
            if msg.get("role", "") != "user":
                continue
            
            content = msg.get("content", "")
            is_triggered, tier = self._check_keyword_trigger(content)
            
            if is_triggered:
                keyword_triggered = True
                trigger_tier = tier
                self.logger.info(
                    "Keyword trigger detected for user %s - Tier: %s, Content: %s", 
                    user_id, tier, content[:100] + "..." if len(content) > 100 else content
                )
                break

        # Check if threshold is reached or keyword is triggered
        if message_count >= self.memory_threshold or keyword_triggered:
            self.logger.info("Triggering memory storage for user %s (reason: %s%s)", 
                            user_id, 
                            "keyword" if keyword_triggered else "threshold",
                            f" - {trigger_tier}" if trigger_tier else "")
            try:
                # Convert messages to conversation format for storage
                conversation_content = self._format_messages_for_storage(self._user_messages[user_id])

                # Store conversation to long-term memory
                await self.store(user_id, conversation_content)

                # Keep only the most recent messages
                self._user_messages[user_id] = self._user_messages[user_id][-self.keep_recent:]

                self.logger.info("Stored %d messages to long-term memory for user %s, kept %d recent messages", 
                               message_count - self.keep_recent, user_id, self.keep_recent)

            except Exception as e:
                self.logger.error("Failed to store messages to long-term memory for user %s: %s", user_id, str(e))
                # If storage fails, still trim to prevent memory overflow
                self._user_messages[user_id] = self._user_messages[user_id][-self.keep_recent:]


    async def store(self, 
              user_id: str, 
              content: str) -> str:
        """Store memory content with LLM-based extraction and return memory ID."""
        self.logger.info("Storing memory for user: %s, content length: %d", user_id, len(content))
        # Extract structured memories from content
        extracted_memories = await self.llm_service.extract_memories_from_content(content)

        # Prepare data for batch storage
        documents = []
        metadatas = []

        # Process extracted memories
        for memory_piece in extracted_memories.memories:
            documents.append(memory_piece.content)
            metadatas.append(self._create_base_metadata(user_id, memory_piece.type.value))

        # If no memories were extracted, do not store anything
        if not documents:
            self.logger.debug("No structured memories extracted, nothing stored for user %s", user_id)
            return ""  # Return empty string when no memories extracted

        # Batch store all memories
        memory_ids = self._batch_store_memories(documents, metadatas)

        log_msg = f"Stored {len(memory_ids)} {'extracted memory pieces' if len(memory_ids) > 1 else 'memory piece'} for user {user_id}"
        self.logger.debug(log_msg)

        return memory_ids[0] if len(memory_ids) == 1 else str(memory_ids)
    
    async def retrieve(self, 
                 user_id: str, 
                 query: str,
                 limit: int = 5,
                 query_context: Optional[str] = None,
                 enable_query_process: bool = False
                 ) -> List[Dict[str, Any]]:
        """Retrieve relevant memories based on query using query preprocessing for better results."""
        self.logger.info("Retrieving memories for user: %s, query: %s, limit: %d", user_id, query[:50] + "..." if len(query) > 50 else query, limit)
        
        # Preprocess the query to get variations and keywords
        preprocessed = await self.llm_service.preprocess_query(query, query_context,enable_query_process)

        self.logger.info("Preprocessed query: original='%s', rewritten=%s, keywords=%s", 
                         preprocessed.original_query, preprocessed.rewritten_queries, preprocessed.keywords)
        
        # Prepare all query texts for batch processing
        query_texts = [preprocessed.original_query]
        
        # Add rewritten queries if available
        if preprocessed.rewritten_queries:
            query_texts.extend(preprocessed.rewritten_queries)
        
        self.logger.debug("Searching with %d query variations in batch", len(query_texts))
        
        try:
            # Use ChromaDB's batch query capability
            results = self.collection.query(
                query_texts=query_texts,
                n_results=min(limit, 20), 
                where={"user_id": user_id},
                include=["documents", "metadatas", "distances"]
            )

            if preprocessed.keywords:
                # Build keyword query - use $or only if multiple keywords, otherwise use simple $contains
                if len(preprocessed.keywords) > 1:
                    keyword_query = {"$or": [{"$contains": kw} for kw in preprocessed.keywords]}
                else:
                    keyword_query = {"$contains": preprocessed.keywords[0]}
                    
                results_keyword_match = self.collection.get(
                    limit=min(limit, 20), 
                    where={"user_id": user_id},
                    where_document=keyword_query,
                    include=["documents", "metadatas"]
                )
            else:
                results_keyword_match = {"ids": [], "documents": [], "metadatas": []}

            # Collect memories with recall count and best distance
            memory_stats = {}
            
            if results.get("documents"):
                # Process results from all queries
                for ids, docs, metas, distances in zip(
                    results["ids"],
                    results["documents"], 
                    results["metadatas"], 
                    results["distances"]
                ):
                    for doc_id, content, metadata, distance in zip(ids, docs, metas, distances):
                        if doc_id not in memory_stats:
                            memory_stats[doc_id] = {
                                "content": content,
                                "metadata": metadata,
                                "best_distance": distance,
                                "recall_count": 1,
                            }
                        else:
                            # Update recall count and best distance
                            memory_stats[doc_id]["recall_count"] += 1
                            if distance < memory_stats[doc_id]["best_distance"]:
                                memory_stats[doc_id]["best_distance"] = distance
            
            # Convert to list and sort by recall count (desc) then by best distance (asc)
            memories = list(memory_stats.values())
            memories.sort(key=lambda x: (-x["recall_count"], x["best_distance"]))

            memories_enhanced = []
            if results_keyword_match.get("documents"):
                for doc_id, content, metadata in zip(
                    results_keyword_match["ids"], 
                    results_keyword_match["documents"], 
                    results_keyword_match["metadatas"]
                ):
                    if doc_id not in memory_stats:
                        memories_enhanced.append({
                            "content": content,
                            "metadata": metadata,
                            "best_distance": None,
                            "recall_count": 1,
                        })

            self.logger.info("Batch query search found %d unique memories, %d from keyword match (excluding overlaps), raw keyword matches count: %d",
                             len(memories), len(memories_enhanced), len(results_keyword_match.get("documents", [])))
            # Limit results and format output
            final_memories = []
            for memory in memories[:limit] + memories_enhanced:
                # Remove created_timestamp and user_id from metadata
                filtered_metadata = {k: v for k, v in memory["metadata"].items() 
                                   if k not in ["created_timestamp", "user_id"]}
                final_memories.append({
                    "content": memory["content"], 
                    "metadata": filtered_metadata
                })
            
            self.logger.debug("Retrieved %d memories from %d query variations for user %s, sorted by recall count and distance", 
                             len(final_memories), len(query_texts), user_id)
            return final_memories
            
        except Exception as e:
            self.logger.error("Error in batch query search: %s", str(e))
            # Fallback to single query search
            try:
                results = self.collection.query(
                    query_texts=[preprocessed.original_query],
                    n_results=limit,
                    where={"user_id": user_id},
                    include=["documents", "metadatas"]
                )
                
                memories = self._format_memory_results(results)
                self.logger.debug("Fallback: Retrieved %d memories for user %s", len(memories), user_id)
                return memories
                
            except Exception as fallback_e:
                self.logger.error("Fallback query also failed: %s", str(fallback_e))
                return []

    async def clear(self, user_id: str) -> None:
        """Clear all memories for a user."""
        self.collection.delete(where={"user_id": user_id})


    async def extract_meta(self, user_id: str, days: int = 1) -> List[str]:
        """Extract meta memory from recent memories and store it. Returns list of memory IDs."""
        self.logger.info("Extracting and storing meta memory for user: %s (last %d day(s))", user_id, days)
        
        # Get recent memories and extract meta memory
        recent_memories = await self._get_recent_memories(user_id, days)
        meta_memory = await self.llm_service.extract_meta_memory_from_recent(recent_memories)
        
        if not meta_memory.contents:
            self.logger.debug("No meta memory contents extracted for user %s", user_id)
            return []

        # Prepare batch data
        documents = []
        metadatas = []
        
        for piece in meta_memory.contents:
            documents.append(piece.content)
            metadatas.append(self._create_base_metadata(user_id, piece.type.value))
        
        # Batch store all meta contents
        memory_ids = self._batch_store_memories(documents, metadatas)
        
        self.logger.debug("Stored %d meta content pieces for user %s", len(memory_ids), user_id)
        return memory_ids


    def _compile_keyword_patterns(self) -> List[re.Pattern]:
        """编译关键字正则表达式模式，每个层级合并为一个 alternation 正则"""
        compiled_patterns = []
        
        for tier_patterns in self._TRIGGER_KEYWORDS:
            # 将同一层级的所有模式合并为一个 alternation 正则
            combined_pattern = '(?:' + '|'.join(tier_patterns) + ')'
            compiled_patterns.append(re.compile(combined_pattern, re.IGNORECASE | re.UNICODE))
        
        return compiled_patterns
    
    def _check_keyword_trigger(self, content: str) -> Tuple[bool, str]:
        """
        检查内容是否包含触发关键字，按层级优先检测
        使用优化的 alternation 正则，按层级索引顺序早退检测
        
        Args:
            content: 要检查的文本内容
            
        Returns:
            Tuple[bool, str]: (是否触发, 匹配的层级索引)
        """
        if not content:
            return False, ""
        
        # 应用扫描长度限制，避免极长消息的性能抖动
        if len(content) > self.max_scan_length:
            content = content[:self.max_scan_length]
            self.logger.debug("Content truncated to %d characters for keyword scanning", self.max_scan_length)
        
        # 预处理文本：去除多余空格，统一格式
        cleaned_content = re.sub(r'\s+', ' ', content.strip())
        
        # 按层级优先级检查，早退机制
        for tier_index, pattern in enumerate(self._compiled_patterns):
            if pattern.search(cleaned_content):
                tier_name = f"tier{tier_index + 1}"
                self.logger.debug(
                    "Keyword trigger detected - Tier: %s, Content: %s",
                    tier_name, cleaned_content[:100] + "..." if len(cleaned_content) > 100 else cleaned_content
                )
                return True, tier_name
        
        return False, ""

    def _format_messages_for_storage(self, messages: List[Dict[str, Any]]) -> str:
        """
        Format messages into a conversation string suitable for memory storage.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            Formatted conversation string
        """
        conversation_lines = []
        
        for msg in messages:
            role = msg.get('role', 'unknown')
            content = msg.get('content', '')
            
            # Format based on role
            if role == 'user':
                conversation_lines.append(f"User: {content}")
            elif role == 'assistant':
                conversation_lines.append(f"Assistant: {content}")
            elif role == 'system':
                conversation_lines.append(f"System: {content}")
            else:
                conversation_lines.append(f"{role.title()}: {content}")
        
        return "\n\n".join(conversation_lines)


    def _create_base_metadata(self, user_id: str, memory_type: str) -> Dict[str, Any]:
        """Create base metadata for memory storage."""
        now = datetime.now(timezone.utc)
        
        return {
            "user_id": user_id,
            "created_at": now.isoformat(),
            "created_timestamp": now.timestamp(),
            "memory_type": memory_type,
        }
    
    def _batch_store_memories(self, documents: List[str], metadatas: List[Dict[str, Any]], 
                             ids: Optional[List[str]] = None) -> List[str]:
        """Batch store multiple memories and return memory IDs."""
        if not ids:
            ids = [str(uuid.uuid4()) for _ in documents]
        
        self.collection.upsert(
            documents=documents,
            metadatas=metadatas,
            ids=ids
        )

        return ids
    
    def _format_memory_results(self, results: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Format ChromaDB results into standard memory format."""
        memories = []
        if results.get("documents"):
            # Handle both single query and batch query results
            ids = results["ids"]
            documents = results["documents"] 
            metadatas = results["metadatas"]
            
            # Flatten if nested (batch query results)
            if isinstance(ids[0], list):
                ids = [item for sublist in ids for item in sublist]
                documents = [item for sublist in documents for item in sublist]
                metadatas = [item for sublist in metadatas for item in sublist]
            
            for doc_id, content, metadata in zip(ids, documents, metadatas):
                # Remove created_timestamp and user_id from metadata
                filtered_metadata = {k: v for k, v in metadata.items() 
                                   if k not in ["created_timestamp", "user_id"]}
                memories.append({
                    "content": content,
                    "metadata": filtered_metadata,
                })
        return memories

    async def _get_recent_memories(self, user_id: str, days: int = 1) -> List[Dict[str, Any]]:
        """Get all memories for a user created within the last N days."""
        self.logger.info("Retrieving last %d day(s) memories for user: %s", days, user_id)
        
        # Calculate timestamp range - much simpler approach
        now = datetime.now(timezone.utc)
        start_timestamp = (now - timedelta(days=days)).timestamp()
        end_timestamp = now.timestamp()
        
        try:
            results = self.collection.get(
                where={
                    "$and": [
                        {"user_id": user_id},
                        {"created_timestamp": {"$gte": start_timestamp}},
                        {"created_timestamp": {"$lte": end_timestamp}}
                    ]
                },
                include=["documents", "metadatas"]
            )
            
            memories = []
            if results.get("documents"):
                for doc_id, content, metadata in zip(results["ids"], results["documents"], results["metadatas"]):
                    memories.append({
                        "id": doc_id,
                        "content": content,
                        "metadata": metadata,
                    })
            
            self.logger.debug("Retrieved %d memories for last %d day(s) for user %s", len(memories), days, user_id)
            return memories
            
        except Exception as e:
            self.logger.error("Error retrieving recent memories: %s", str(e))
            return []
