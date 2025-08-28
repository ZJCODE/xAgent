import logging
import os
import uuid
import re
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone
import dotenv

from upstash_vector import Index, Vector

from .base_memory import MemoryStorageBase
from .utils.llm_service import MemoryLLMService
from .utils.memory_config import TRIGGER_KEYWORDS, MAX_SCAN_LENGTH
from .utils.messages_for_memory import RedisMessagesForMemory

dotenv.load_dotenv(override=True)

class MemoryStorageUpstash(MemoryStorageBase):
    """
    Upstash Vector memory storage with LLM-based memory extraction.
    
    Args:
        memory_threshold: Number of messages to trigger long-term storage. Defaults to 10
        keep_recent: Number of recent messages to keep after storage. Defaults to 2
    """
    
    def __init__(self, 
                 memory_threshold: int = 10,
                 keep_recent: int = 2):
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Initialize LLM service
        self.llm_service = MemoryLLMService()

        # Initialize Upstash Vector index from environment variables
        try:
            self.index = Index.from_env()
            self.logger.info("Successfully initialized Upstash Vector index")
        except Exception as e:
            self.logger.error("Failed to initialize Upstash Vector index: %s", str(e))
            raise
        
        # Initialize Redis-based user message storage
        try:
            self.user_messages = RedisMessagesForMemory()
            self.logger.info("Successfully initialized Redis user message storage for memory")
        except Exception as e:
            self.logger.error("Failed to initialize Redis user message storage for memory: %s", str(e))
            raise
        
        # Memory management configuration
        self.memory_threshold = memory_threshold  # Store to long-term memory when reaching this many messages
        self.keep_recent = keep_recent  # Keep this many most recent messages after storage
        
        # 编译正则表达式模式，提高匹配性能
        self._compiled_patterns = self._compile_keyword_patterns()
        
        self.logger.info("MessagesForMemory initialized with threshold: %d, keep_recent: %d", 
                        memory_threshold, keep_recent)
    
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

        # Add new messages to user's Redis storage
        await self.user_messages.add_messages(user_id, messages)
        message_count = await self.user_messages.get_message_count(user_id)

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
                # Get all messages from Redis storage for processing
                all_user_messages = await self.user_messages.get_messages(user_id)
                
                # Convert messages to conversation format for storage
                conversation_content = self._format_messages_for_storage(all_user_messages)

                # Store conversation to long-term memory
                await self.store(user_id, conversation_content)

                # Keep only the most recent messages in Redis
                await self.user_messages.keep_recent_messages(user_id, self.keep_recent)

                self.logger.info("Stored %d messages to long-term memory for user %s, kept %d recent messages", 
                               message_count - self.keep_recent if message_count > self.keep_recent else message_count, user_id, self.keep_recent)

            except Exception as e:
                self.logger.error("Failed to store messages to long-term memory for user %s: %s", user_id, str(e))
                # If storage fails, still trim to prevent memory overflow
                await self.user_messages.keep_recent_messages(user_id, self.keep_recent)


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

        self.logger.debug(f"extracted_memories: {extracted_memories}")

        # Collect all related memories for all extracted memories
        all_related_memories = []
        related_memory_ids = set()

        # Prepare all queries first (similar to local memory approach)
        query_data = []
        for memory_piece in extracted_memories.memories:
            query_data.append(memory_piece.content)

        # Process queries for related memories (Upstash requires individual queries)
        try:
            for query_idx, query_content in enumerate(query_data):
                # Query for related memories (max 2 per memory piece)
                results = self.index.query(
                    data=query_content,
                    top_k=2,  # Max 2 related memories per query
                    filter=f"user_id = '{user_id}'",
                    include_vectors=False,
                    include_metadata=True,
                    include_data=True
                )

                # Extract related memories with their IDs for this query
                for result in results:
                    if result.id not in related_memory_ids:
                        related_memory_ids.add(result.id)
                        related_memory = {
                            "id": result.id,
                            "content": result.data if hasattr(result, 'data') else "",
                            "metadata": result.metadata,
                            "distance": getattr(result, 'score', 0.0)  # Use score as distance equivalent
                        }
                        all_related_memories.append(related_memory)

        except Exception as e:
            self.logger.error("Error during batch memory query: %s", str(e))
            # Continue without related memories

        self.logger.debug(f"all_related_memories: {all_related_memories}")

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

        # Prepare vectors for batch storage of final merged memories
        vectors = []
        memory_ids = []

        self.logger.debug(f"final_memories_to_store: {final_memories_to_store}")

        for memory_piece in final_memories_to_store:
            memory_id = str(uuid.uuid4())
            metadata = self._create_base_metadata(user_id, memory_piece.type.value)
            
            vector = Vector(
                id=memory_id,
                data=memory_piece.content,
                metadata=metadata
            )
            vectors.append(vector)
            memory_ids.append(memory_id)

        # Batch store all final memories
        try:
            self.index.upsert(vectors=vectors)
            
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
            
        except Exception as e:
            self.logger.error("Failed to upsert vectors to Upstash: %s", str(e))
            raise
    
    async def retrieve(self, 
                 user_id: str, 
                 query: str,
                 limit: int = 5,
                 query_context: Optional[str] = None,
                 enable_query_process: bool = False
                 ) -> List[Dict[str, Any]]:
        """Retrieve relevant memories based on query using query preprocessing for better results."""
        self.logger.info("Retrieving memories for user: %s, query: %s, limit: %d", 
                        user_id, query[:50] + "..." if len(query) > 50 else query, limit)
        
        # Preprocess the query to get variations and keywords
        preprocessed = await self.llm_service.preprocess_query(query, query_context, enable_query_process)

        self.logger.info("Preprocessed query: original='%s', rewritten=%s", 
                         preprocessed.original_query, preprocessed.rewritten_queries)
        
        # Prepare all query texts for processing
        query_texts = [preprocessed.original_query]
        
        # Add rewritten queries if available
        if preprocessed.rewritten_queries:
            query_texts.extend(preprocessed.rewritten_queries)
        
        self.logger.debug("Searching with %d query variations", len(query_texts))
        
        try:
            # Collect memories with recall count and best score
            memory_stats = {}
            
            # Process each query variation with metadata filtering
            for query_text in query_texts:
                # Use Upstash Vector's metadata filtering to only get vectors for this user
                results = self.index.query(
                    data=query_text,
                    top_k=min(limit * 2, 20),
                    filter=f"user_id = '{user_id}'",  # Use Upstash metadata filtering
                    include_vectors=False,
                    include_metadata=True,
                    include_data=True
                )
                
                # Collect statistics for each result
                for result in results:
                    vector_id = result.id
                    score = getattr(result, 'score', 0.0)
                    
                    if vector_id not in memory_stats:
                        memory_stats[vector_id] = {
                            "content": result.data if hasattr(result, 'data') else "",
                            "metadata": result.metadata,
                            "best_score": score,
                            "recall_count": 1,
                        }
                    else:
                        # Update recall count and best score
                        memory_stats[vector_id]["recall_count"] += 1
                        if score > memory_stats[vector_id]["best_score"]:
                            memory_stats[vector_id]["best_score"] = score
            
            # Convert to list and sort by recall count (desc) then by best score (desc)
            memories = list(memory_stats.values())
            memories.sort(key=lambda x: (-x["recall_count"], -x["best_score"]))
            
            self.logger.info("Query search found %d unique memories for user %s from %d query variations",
                           len(memories), user_id, len(query_texts))
            
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
            self.logger.error("Error in query search: %s", str(e))
            # Fallback to single query search
            try:
                results = self.index.query(
                    data=preprocessed.original_query,
                    top_k=limit,
                    filter=f"user_id = '{user_id}'",
                    include_vectors=False,
                    include_metadata=True,
                    include_data=True
                )
                
                # Format results
                memories = []
                for result in results:
                    filtered_metadata = {k: v for k, v in result.metadata.items() 
                                       if k not in ["created_timestamp", "user_id"]}
                    memories.append({
                        "content": result.data if hasattr(result, 'data') else "",
                        "metadata": filtered_metadata,
                    })
                
                self.logger.debug("Fallback: Retrieved %d memories for user %s", len(memories), user_id)
                return memories
                
            except Exception as fallback_e:
                self.logger.error("Fallback query also failed: %s", str(fallback_e))
                return []

    async def clear(self, user_id: str) -> None:
        """Clear all memories for a user."""
        self.logger.info("Clearing all memories for user: %s", user_id)
        
        try:
            # Clear temporary messages from Redis
            await self.user_messages.clear_messages(user_id)
            
            # Use Upstash Vector's delete with metadata filter to delete all vectors for this user
            # Note: This requires the delete method to support metadata filtering
            # If not supported, we'll need to query first then delete by IDs
            try:
                # Try direct delete with filter (if supported by the SDK)
                self.index.delete(filter=f"user_id = '{user_id}'")
                self.logger.info("Deleted all memories for user %s using filter", user_id)
            except Exception as filter_delete_error:
                self.logger.warning("Direct filter delete not supported, falling back to query-then-delete: %s", 
                                  str(filter_delete_error))
                
                # Fallback: query all vectors for this user first, then delete by IDs
                results = self.index.query(
                    data="",  # Empty query to get general results
                    top_k=10000,  # Large number to get all results
                    filter=f"user_id = '{user_id}'",
                    include_vectors=False,
                    include_metadata=True,
                    include_data=True
                )
                
                # Collect IDs of vectors belonging to this user
                user_vector_ids = [result.id for result in results]
                
                # Delete vectors by IDs in batches
                if user_vector_ids:
                    batch_size = 100  # Process in batches to avoid potential limits
                    for i in range(0, len(user_vector_ids), batch_size):
                        batch_ids = user_vector_ids[i:i + batch_size]
                        try:
                            self.index.delete(ids=batch_ids)
                        except Exception as e:
                            self.logger.warning("Failed to delete batch %d-%d: %s", 
                                              i, min(i + batch_size, len(user_vector_ids)), str(e))
                    
                    self.logger.info("Deleted %d vectors for user %s", len(user_vector_ids), user_id)
                else:
                    self.logger.info("No vectors found for user %s", user_id)
                
        except Exception as e:
            self.logger.error("Error clearing memories for user %s: %s", user_id, str(e))
            raise

    async def delete(self,memory_ids: List[str]):
        """Delete memories by their IDs."""
        self.index.delete(ids = memory_ids)

    async def close(self) -> None:
        """Close Redis connection and cleanup resources."""
        try:
            await self.user_messages.close()
            self.logger.info("MessagesForMemory resources cleaned up successfully")
        except Exception as e:
            self.logger.error("Error closing MessagesForMemory resources: %s", str(e))

    async def extract_meta(self, user_id: str, days: int = 1) -> List[str]:
        """Extract meta memory from recent memories and store it. Returns list of memory IDs."""
        self.logger.info("Extracting and storing meta memory for user: %s (last %d day(s))", user_id, days)
        
        # Get recent memories and extract meta memory
        recent_memories = await self._get_recent_memories(user_id, days)
        meta_memory = await self.llm_service.extract_meta_memory_from_recent(recent_memories)
        
        if not meta_memory.contents:
            self.logger.debug("No meta memory contents extracted for user %s", user_id)
            return []

        # Prepare vectors for batch storage
        vectors = []
        memory_ids = []
        
        for piece in meta_memory.contents:
            memory_id = str(uuid.uuid4())
            metadata = self._create_base_metadata(user_id, piece.type.value)
            
            vector = Vector(
                id=memory_id,
                data=piece.content,
                metadata=metadata
            )
            vectors.append(vector)
            memory_ids.append(memory_id)
        
        # Batch store all meta contents
        try:
            self.index.upsert(vectors=vectors)
            self.logger.debug("Stored %d meta content pieces for user %s", len(memory_ids), user_id)
            return memory_ids
        except Exception as e:
            self.logger.error("Failed to store meta memories: %s", str(e))
            raise

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
        
        try:
            # Use Upstash Vector's metadata filtering to get vectors for this user within time range
            results = self.index.query(
                data="",  # Empty query to get general results
                top_k=10000,  # Large number to get all results
                filter=f"user_id = '{user_id}' AND created_timestamp >= {start_timestamp}",
                include_vectors=False,
                include_metadata=True,
                include_data=True
            )
            
            memories = []
            for result in results:
                memories.append({
                    "id": result.id,
                    "content": result.data if hasattr(result, 'data') else "",
                    "metadata": result.metadata,
                })
            
            self.logger.debug("Retrieved %d memories for last %d day(s) for user %s", len(memories), days, user_id)
            return memories
            
        except Exception as e:
            self.logger.error("Error retrieving recent memories: %s", str(e))
            return []
