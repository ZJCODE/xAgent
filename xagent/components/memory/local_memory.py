import chromadb
import chromadb.utils.embedding_functions as embedding_functions
import logging
import os
import uuid
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import dotenv
from langfuse import observe
from pathlib import Path

from .base_memory import MemoryStorageBase
from .llm.llm_service import MemoryLLMService

dotenv.load_dotenv(override=True)

class MemoryStorageLocal(MemoryStorageBase):
    """Local memory implementation using ChromaDB with LLM-powered memory extraction.
    
    Args:
        path: Path to ChromaDB storage directory. Defaults to ~/.xagent/chroma
        collection_name: Name of the ChromaDB collection. Defaults to 'xagent_memory'
        memory_threshold: Number of messages to trigger long-term storage. Defaults to 10
        keep_recent: Number of recent messages to keep after storage. Defaults to 2
    """
    
    def __init__(self, 
                 path: str = None,
                 collection_name: str = "xagent_memory",
                 memory_threshold: int = 10,
                 keep_recent: int = 2):
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Use default path if none provided
        if path is None:
            path = os.path.expanduser('~/.xagent/chroma')
            logging.info("No path provided, using default path: %s", path)
        
        # Ensure the directory exists
        Path(path).mkdir(parents=True, exist_ok=True)
        
        # Initialize LLM service
        self.llm_service = MemoryLLMService()

        # Initialize OpenAI embedding function
        self.openai_ef = embedding_functions.OpenAIEmbeddingFunction(
            api_key=os.environ.get("OPENAI_API_KEY"),
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
        
        self.logger.info("LocalMemory initialized with collection: %s", collection_name)
    
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

        # 关键词列表（中英文）
        trigger_keywords = [
            # 中文 - 直接指令
            "记住", "记得", "记一下", "记下来", "记录一下", "帮我记", 
            "请记住", "请帮我记住", "保存一下", "存下来", "备忘一下", "写下来",

            # 中文 - 偏好/事实
            "我喜欢", "我爱", "我讨厌", "我最喜欢", "我常", "我是",

            # 英文 - 直接指令
            "remember this", "remember that", "please remember", "help me remember",
            "note this", "note this down", "make a note", "save this", 
            "keep this", "log this", "store this", "write this down",

            # 英文 - 偏好/事实
            "I like", "I love", "I hate", "I prefer", "my favorite is", "I usually", "I am"
        ]

        if user_id not in self._user_messages:
            self._user_messages[user_id] = []

        # Add new messages to user's temporary storage
        self._user_messages[user_id].extend(messages)
        message_count = len(self._user_messages[user_id])

        self.logger.debug("Added %d messages for user %s, total messages: %d", 
                         len(messages), user_id, message_count)

        # 只监测 role 为 'user' 的消息是否有关键词触发，逆序遍历（从最新一条开始）
        keyword_triggered = False
        for msg in reversed(messages):
            if msg.get("role", "") != "user":
                continue
            content = msg.get("content", "")
            if any(kw.lower() in content.lower() for kw in trigger_keywords):
                keyword_triggered = True
                self.logger.info("Keyword trigger detected in user message for user %s: %s", user_id, content)
                break

        # Check if threshold is reached or keyword is triggered
        if message_count >= self.memory_threshold or keyword_triggered:
            self.logger.info("Triggering memory storage for user %s (reason: %s)", 
                            user_id, "keyword" if keyword_triggered else "threshold")
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


    async def store(self, 
              user_id: str, 
              content: str) -> str:
        """Store memory content with LLM-based extraction and return memory ID."""
        self.logger.info("Storing memory for user: %s, content length: %d", user_id, len(content))
        
        # Extract structured memories from content
        extracted_memories = await self.llm_service.extract_memories_from_content(content)
        now = datetime.now()
        
        # Prepare data for batch storage
        documents = []
        metadatas = []
        
        # Process extracted memories
        for memory_piece in extracted_memories.memories:
            documents.append(memory_piece.content)
            metadatas.append(self._create_base_metadata(user_id, memory_piece.type.value, None, now))

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
                results_keyword_match = self.collection.get(
                    limit=min(limit, 20), 
                    where={"user_id": user_id},
                    where_document={"$or": [{"$contains": kw} for kw in preprocessed.keywords] } if preprocessed.keywords else None,
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
            return await self._fallback_retrieve(preprocessed.original_query, user_id, limit)

    async def clear(self, user_id: str) -> None:
        """Clear all memories for a user."""
        self.collection.delete(where={"user_id": user_id})

    async def _fallback_retrieve(self, query: str, user_id: str, limit: int) -> List[Dict[str, Any]]:
        """Fallback retrieval method using single query."""
        try:
            results = self.collection.query(
                query_texts=[query],
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
            meta = self._create_base_metadata(user_id, piece.type.value, {}, datetime.now())
            metadatas.append(meta)
        
        # Batch store all meta contents
        memory_ids = self._batch_store_memories(documents, metadatas)
        
        self.logger.debug("Stored %d meta content pieces for user %s", len(memory_ids), user_id)
        return memory_ids


    def _create_base_metadata(self, user_id: str, memory_type: str, 
                             additional_metadata: Optional[Dict[str, Any]] = None,
                             timestamp: Optional[datetime] = None) -> Dict[str, Any]:
        """Create base metadata for memory storage."""
        now = timestamp or datetime.now()
        meta = {
            "user_id": user_id,
            "created_at": now.isoformat(),
            "created_timestamp": now.timestamp(),
            "memory_type": memory_type,
        }
        if additional_metadata:
            meta.update(additional_metadata)
        return meta
    
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
            for doc_id, content, metadata in zip(
                results["ids"], 
                results["documents"], 
                results["metadatas"]
            ):
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
        
        # Calculate time range
        start_date = datetime.now() - timedelta(days=days-1)
        start_timestamp = start_date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        end_timestamp = datetime.now().timestamp()
        
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
