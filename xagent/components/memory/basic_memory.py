import logging
import os
import uuid
import re
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone
import dotenv

from .base_memory import MemoryStorageBase
from .helper.llm_service import MemoryLLMService
from .config.memory_config import TRIGGER_KEYWORDS, MAX_SCAN_LENGTH
from .message_buffer import MessageBufferBase
from .vector_store import VectorStoreBase

dotenv.load_dotenv(override=True)

class MemoryStorageBasic(MemoryStorageBase):
    """
    Basic memory storage implementation with common functionality.
    Provides shared methods for both local and cloud-based implementations.
    """
    
    def __init__(self, 
                 memory_threshold: int = 10,
                 keep_recent: int = 2,
                 message_buffer: Optional[MessageBufferBase] = None,
                 vector_store: Optional[VectorStoreBase] = None):
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Initialize LLM service
        self.llm_service = MemoryLLMService()
        
        # Set vector store and message buffer (initialized by subclasses)
        self.vector_store = vector_store
        self.message_buffer = message_buffer
        
        # Memory management configuration
        self.memory_threshold = memory_threshold  # Store to long-term memory when reaching this many messages
        self.keep_recent = keep_recent  # Keep this many most recent messages after storage
        
        # 编译正则表达式模式，提高匹配性能
        self._compiled_patterns = self._compile_keyword_patterns()
        
        self.logger.info("Basic memory initialized with threshold: %d, keep_recent: %d", 
                        memory_threshold, keep_recent)
    
    @abstractmethod
    def _initialize_vector_store(self, **kwargs) -> VectorStoreBase:
        """Initialize the vector store. To be implemented by subclasses."""
        pass
    
    @abstractmethod
    def _initialize_message_buffer(self, **kwargs) -> MessageBufferBase:
        """Initialize the message buffer. To be implemented by subclasses."""
        pass
    
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

        # Add new messages to user's message buffer
        await self.message_buffer.add_messages(user_id, messages)
        message_count = await self.message_buffer.get_message_count(user_id)

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
                # Get all messages from buffer for storage
                all_messages = await self.message_buffer.get_messages(user_id)
                
                # Convert messages to conversation format for storage
                conversation_content = self._format_messages_for_storage(all_messages)

                # Store conversation to long-term memory
                await self.store(user_id, conversation_content)

                # Keep only the most recent messages in buffer
                await self.message_buffer.keep_recent_messages(user_id, self.keep_recent)

                self.logger.info("Stored %d messages to long-term memory for user %s, kept %d recent messages", 
                               message_count - self.keep_recent if message_count > self.keep_recent else message_count, user_id, self.keep_recent)

            except Exception as e:
                self.logger.error("Failed to store messages to long-term memory for user %s: %s", user_id, str(e))
                # If storage fails, still trim to prevent memory overflow
                await self.message_buffer.keep_recent_messages(user_id, self.keep_recent)

    async def store(self, 
              user_id: str, 
              content: str) -> str:
        """Store memory content with LLM-based extraction, memory fusion, and return memory ID."""
        self.logger.info("Storing memory for user: %s, content length: %d", user_id, len(content))
        # Extract structured memories from content
        extracted_memories = await self.llm_service.extract_memories_from_content(content)

        if not extracted_memories.memories:
            self.logger.info("No structured memories extracted, nothing stored for user %s", user_id)
            return ""  # Return empty string when no memories extracted

        # Collect all related memories for all extracted memories
        all_related_memories = []
        related_memory_ids = set()

        # Prepare query texts for batch query
        query_texts = []
        for memory_piece in extracted_memories.memories:
            query_texts.append(memory_piece.content)

        # Query for related memories in batch (max 2 per memory piece)
        try:
            related_vector_docs = await self.vector_store.query(
                query_texts=query_texts,
                n_results=2,  # Max 2 related memories per query
                meta_filter={"user_id": user_id}
            )

            if related_vector_docs:
                # Process results from vector store
                for vector_doc in related_vector_docs:
                    if vector_doc.id not in related_memory_ids:
                        related_memory_ids.add(vector_doc.id)
                        related_memory = {
                            "id": vector_doc.id,
                            "content": vector_doc.document,
                            "metadata": vector_doc.metadata,
                            "score": vector_doc.score
                        }
                        all_related_memories.append(related_memory)

        except Exception as e:
            self.logger.error("Error during batch memory query: %s", str(e))
            # Continue without related memories

        # Merge all extracted memories with all related memories using LLM (single call)
        try:
            merged_result = await self.llm_service.merge_memories(
                extracted_memories=extracted_memories,
                related_memories=all_related_memories
            )
            final_memories_to_store = merged_result.memories
        except Exception as e:
            self.logger.error("Error during memory fusion: %s", str(e))
            # Fallback: store the original extracted memories without fusion
            final_memories_to_store = extracted_memories.memories

        # Prepare data for batch storage of final merged memories
        documents = []
        metadatas = []
        memory_ids = []

        for memory_piece in final_memories_to_store:
            memory_id = str(uuid.uuid4())
            metadata = self._create_base_metadata(user_id, memory_piece.type.value)
            
            documents.append(memory_piece.content)
            metadatas.append(metadata)
            memory_ids.append(memory_id)

        # Batch store all final memories
        await self.vector_store.upsert(
            ids=memory_ids,
            documents=documents,
            metadatas=metadatas
        )

        # Delete old related memories after storing new merged ones
        if related_memory_ids:
            try:
                await self.delete(list(related_memory_ids))
                self.logger.info("Deleted %d old memories that were merged", len(related_memory_ids))
            except Exception as e:
                self.logger.error("Error deleting old memories: %s", str(e))

        log_msg = f"Stored {len(memory_ids)} {'merged memory pieces' if len(memory_ids) > 1 else 'memory piece'} for user {user_id} (deleted {len(related_memory_ids)} old memories)"
        self.logger.info(log_msg)

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
        preprocessed = await self.llm_service.preprocess_query(query, query_context, enable_query_process)

        self.logger.info("Preprocessed query: original='%s', rewritten=%s", 
                         preprocessed.original_query, preprocessed.rewritten_queries)
        
        # Prepare all query texts for batch processing
        query_texts = [preprocessed.original_query]
        
        # Add rewritten queries if available
        if preprocessed.rewritten_queries:
            query_texts.extend(preprocessed.rewritten_queries)
        
        self.logger.debug("Searching with %d query variations in batch", len(query_texts))
        
        try:
            # Use vector store's batch query capability
            vector_docs = await self.vector_store.query(
                query_texts=query_texts,
                n_results=min(limit * 2, 20), 
                meta_filter={"user_id": user_id}
            )

            # Collect memories with recall count and best score
            memory_stats = {}
            
            if vector_docs:
                # Process results from vector store
                for vector_doc in vector_docs:
                    doc_id = vector_doc.id
                    score = vector_doc.score if vector_doc.score is not None else 0.0
                    
                    if doc_id not in memory_stats:
                        memory_stats[doc_id] = {
                            "content": vector_doc.document,
                            "metadata": vector_doc.metadata,
                            "best_score": score,
                            "recall_count": 1,
                        }
                    else:
                        # Update recall count and best score
                        memory_stats[doc_id]["recall_count"] += 1
                        if score > memory_stats[doc_id]["best_score"]:
                            memory_stats[doc_id]["best_score"] = score
            
            # Convert to list and sort by recall count (desc) then by best score (desc)
            memories = list(memory_stats.values())
            memories.sort(key=lambda x: (-x["recall_count"], -x["best_score"]))

            self.logger.info("Vector query search found %d unique memories for user %s", len(memories), user_id)
            
            # Limit results and format output
            final_memories = []
            for memory in memories[:limit]:
                # Remove created_timestamp and user_id from metadata
                filtered_metadata = {k: v for k, v in memory["metadata"].items() 
                                   if k not in ["created_timestamp", "user_id"]}
                final_memories.append({
                    "content": memory["content"], 
                    "metadata": filtered_metadata
                })
            
            self.logger.debug("Retrieved %d memories from %d query variations for user %s, sorted by recall count and score", 
                             len(final_memories), len(query_texts), user_id)
            return final_memories
            
        except Exception as e:
            self.logger.error("Error in vector query search: %s", str(e))
            # Fallback to single query search
            try:
                fallback_docs = await self.vector_store.query(
                    query_texts=[preprocessed.original_query],
                    n_results=limit,
                    meta_filter={"user_id": user_id}
                )
                
                final_memories = []
                for vector_doc in fallback_docs:
                    filtered_metadata = {k: v for k, v in vector_doc.metadata.items() 
                                       if k not in ["created_timestamp", "user_id"]}
                    final_memories.append({
                        "content": vector_doc.document, 
                        "metadata": filtered_metadata
                    })
                
                self.logger.debug("Fallback: Retrieved %d memories for user %s", len(final_memories), user_id)
                return final_memories
                
            except Exception as fallback_e:
                self.logger.error("Fallback query also failed: %s", str(fallback_e))
                return []

    async def clear(self, user_id: str) -> None:
        """Clear all memories for a user."""
        # Clear long-term memories from vector store
        await self.vector_store.delete_by_filter({"user_id": user_id})
        
        # Clear temporary messages from message buffer
        await self.message_buffer.clear_messages(user_id)
        
        self.logger.info("Cleared all memories and messages for user: %s", user_id)

    async def delete(self, memory_ids: List[str]):
        """Delete memories by their IDs."""
        await self.vector_store.delete(memory_ids)

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
        memory_ids = []
        
        for piece in meta_memory.contents:
            memory_id = str(uuid.uuid4())
            metadata = self._create_base_metadata(user_id, piece.type.value)
            
            documents.append(piece.content)
            metadatas.append(metadata)
            memory_ids.append(memory_id)
        
        # Batch store all meta contents
        await self.vector_store.upsert(
            ids=memory_ids,
            documents=documents,
            metadatas=metadatas
        )
        
        self.logger.debug("Stored %d meta content pieces for user %s", len(memory_ids), user_id)
        return memory_ids

    def _compile_keyword_patterns(self) -> List[re.Pattern]:
        """编译关键字正则表达式模式，每个层级合并为一个 alternation 正则"""
        compiled_patterns = []
        
        for tier_patterns in TRIGGER_KEYWORDS:
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
        if len(content) > MAX_SCAN_LENGTH:
            content = content[:MAX_SCAN_LENGTH]
            self.logger.debug("Content truncated to %d characters for keyword scanning", MAX_SCAN_LENGTH)

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

    async def _get_recent_memories(self, user_id: str, days: int = 1) -> List[Dict[str, Any]]:
        """Get all memories for a user created within the last N days."""
        self.logger.info("Retrieving last %d day(s) memories for user: %s", days, user_id)
        
        # Calculate timestamp range
        now = datetime.now(timezone.utc)
        start_timestamp = (now - timedelta(days=days)).timestamp()
        end_timestamp = now.timestamp()
        
        try:
            # Use unified meta_filter structure
            meta_filter = {
                "$and": [
                    {"user_id": user_id},
                    {"created_timestamp": {"$gte": start_timestamp}},
                    {"created_timestamp": {"$lte": end_timestamp}}
                ]
            }
            
            # Query using vector store abstraction with empty query to get all results
            vector_docs = await self.vector_store.query(
                query_texts=[""],  # Empty query to get all matching by filter
                n_results=1000,    # Large number to get all memories
                meta_filter=meta_filter
            )
            
            memories = []
            for vector_doc in vector_docs:
                memories.append({
                    "id": vector_doc.id,
                    "content": vector_doc.document,
                    "metadata": vector_doc.metadata,
                })
            
            self.logger.debug("Retrieved %d memories for last %d day(s) for user %s", len(memories), days, user_id)
            return memories
            
        except Exception as e:
            self.logger.error("Error retrieving recent memories: %s", str(e))
            return []