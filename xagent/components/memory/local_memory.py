import chromadb
import chromadb.utils.embedding_functions as embedding_functions
import logging
import os
import uuid
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import dotenv

from ...schemas.memory import MemoryType
from .base_memory import MemoryStore
from .llm_service import MemoryLLMService

dotenv.load_dotenv(override=True)

class LocalMemory(MemoryStore):
    """Local memory implementation using ChromaDB with LLM-powered memory extraction."""
    
    def __init__(self, collection_name: str = "xagent_memory"):
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Initialize LLM service
        self.llm_service = MemoryLLMService()

        # Initialize OpenAI embedding function
        self.openai_ef = embedding_functions.OpenAIEmbeddingFunction(
            api_key=os.environ.get("OPENAI_API_KEY"),
            model_name="text-embedding-3-small"
        )
        
        # Initialize ChromaDB client and collection
        self.chroma_client = chromadb.Client()
        self.collection = self.chroma_client.get_or_create_collection(
            name=collection_name,
            embedding_function=self.openai_ef
        )
        
        self.logger.info("LocalMemory initialized with collection: %s", collection_name)
    
    async def store(self, 
              user_id: str, 
              content: str,
              metadata: Optional[Dict[str, Any]] = None) -> str:
        """Store memory content with LLM-based extraction and return memory ID."""
        self.logger.info("Storing memory for user: %s, content length: %d", user_id, len(content))
        
        # Extract structured memories from content
        extracted_memories = await self.llm_service.extract_memories_from_content(content)
        
        # Store each extracted memory piece
        memory_ids = []
        documents = []
        metadatas = []
        for memory_piece in extracted_memories.memories:
            memory_id = str(uuid.uuid4())
            
            # Prepare metadata
            now = datetime.now()
            meta = {
                "user_id": user_id, 
                "created_at": now.isoformat(),
                "created_timestamp": now.timestamp(),
                "memory_type": memory_piece.type.value,
            }
            if metadata:
                meta.update(metadata)

            memory_ids.append(memory_id)
            documents.append(memory_piece.content)
            metadatas.append(meta)

        if memory_ids:
            self.collection.upsert(
                documents=documents,
                metadatas=metadatas,
                ids=memory_ids
            )
            self.logger.debug("Stored %d extracted memory pieces for user %s", len(memory_ids), user_id)

        # If no memories were extracted, store the original content as working memory
        if not memory_ids:
            memory_id = str(uuid.uuid4())
            now = datetime.now()
            meta = {
                "user_id": user_id, 
                "created_at": now.isoformat(),
                "created_timestamp": now.timestamp(),
                "memory_type": MemoryType.WORKING.value,
            }
            if metadata:
                meta.update(metadata)
            
            self.collection.upsert(
                documents=[content.strip()],
                metadatas=[meta],
                ids=[memory_id]
            )
            memory_ids.append(memory_id)
            self.logger.debug("No structured memories extracted, stored as working memory for user %s", user_id)
        
        return memory_ids[0] if len(memory_ids) == 1 else str(memory_ids)
    
    async def retrieve(self, 
                 user_id: str, 
                 query: str,
                 limit: int = 5) -> List[Dict[str, Any]]:
        """Retrieve relevant memories based on query using query preprocessing for better results."""
        self.logger.info("Retrieving memories for user: %s, query: %s, limit: %d", user_id, query[:50] + "..." if len(query) > 50 else query, limit)
        
        # Preprocess the query to get variations and keywords
        preprocessed = await self.llm_service.preprocess_query(query)
        
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
                n_results=min(limit * 2, 20),  # Get more results to account for deduplication
                where={"user_id": user_id},
                include=["documents", "metadatas", "distances"]
            )
            
            # Collect memories with recall count and best distance
            memory_stats = {}
            
            if results["documents"]:
                # Process results from all queries
                for query_idx, (ids, docs, metas, distances) in enumerate(zip(
                    results["ids"],
                    results["documents"], 
                    results["metadatas"], 
                    results["distances"]
                )):
                    matched_query = query_texts[query_idx] if query_idx < len(query_texts) else "unknown"
                    
                    for doc_id, content, metadata, distance in zip(ids, docs, metas, distances):
                        if doc_id not in memory_stats:
                            memory_stats[doc_id] = {
                                "id": doc_id,
                                "content": content,
                                "metadata": metadata,
                                "best_distance": distance,
                                "recall_count": 1,
                                "matched_queries": [matched_query]
                            }
                        else:
                            # Update recall count and best distance
                            memory_stats[doc_id]["recall_count"] += 1
                            memory_stats[doc_id]["matched_queries"].append(matched_query)
                            if distance < memory_stats[doc_id]["best_distance"]:
                                memory_stats[doc_id]["best_distance"] = distance
            
            # Convert to list and sort by recall count (desc) then by best distance (asc)
            memories = list(memory_stats.values())
            memories.sort(key=lambda x: (-x["recall_count"], x["best_distance"]))
            
            # Limit results and remove internal fields
            final_memories = []
            for memory in memories[:limit]:
                final_memories.append({
                    "content": memory["content"],
                    "metadata": memory["metadata"],
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
                
                memories = []
                if results["documents"]:
                    for ids, docs, metas in zip(results["ids"], results["documents"], results["metadatas"]):
                        for doc_id, content, metadata in zip(ids, docs, metas):
                            memories.append({
                                "content": content,
                                "metadata": metadata,
                            })
                
                self.logger.debug("Fallback: Retrieved %d memories for user %s", len(memories), user_id)
                return memories
                
            except Exception as fallback_e:
                self.logger.error("Fallback query also failed: %s", str(fallback_e))
                return []


    async def extract_meta(self, user_id: str, days: int = 1) -> List[str]:
        """Extract meta memory from recent memories and store it. Returns list of memory IDs.
        
        Args:
            user_id: The user ID to extract meta memory for
            days: Number of days to look back for memory extraction (default: 1 for today only)
        
        Returns:
            List of memory IDs for the stored meta memories
        """
        self.logger.info("Extracting and storing meta memory for user: %s (last %d day(s))", user_id, days)
        
        # Get recent memories
        recent_memories = await self._get_recent_memories(user_id, days)
        # Extract meta memory from recent memories
        meta_memory = await self.llm_service.extract_meta_memory_from_recent(recent_memories)
        
        memory_ids = []
        now = datetime.now()
        
        # Store all meta contents separately (each content piece as individual document)
        if meta_memory.contents:
            content_ids = []
            content_documents = []
            content_metadatas = []
            
            for i, piece in enumerate(meta_memory.contents):
                content_id = str(uuid.uuid4())
                
                # Update source description based on days
                source_description = "daily_meta_extraction" if days == 1 else f"{days}_day_meta_extraction"
                
                content_meta = {
                    "user_id": user_id,
                    "created_at": now.isoformat(),
                    "created_timestamp": now.timestamp(),
                    "memory_type": piece.type.value,  # Use the MetaMemoryType value
                    "source": source_description,
                    "days_covered": days,  # Track how many days this meta memory covers
                }
                
                content_ids.append(content_id)
                content_documents.append(piece.content)
                content_metadatas.append(content_meta)
            
            # Batch insert all meta contents
            self.collection.upsert(
                documents=content_documents,
                metadatas=content_metadatas,
                ids=content_ids
            )
            memory_ids.extend(content_ids)
            self.logger.debug("Stored %d meta content pieces for user %s", len(content_ids), user_id)
        
        self.logger.debug("Stored meta memory for user %s: %d total documents", user_id, len(memory_ids))
        return memory_ids

    async def _get_recent_memories(self, user_id: str, days: int = 1) -> List[Dict[str, Any]]:
        """Get all memories for a user created within the last N days.
        
        Args:
            user_id: The user ID to retrieve memories for
            days: Number of days to look back (default: 1 for today only)
        
        Returns:
            List of memories within the specified time range
        """
        self.logger.info("Retrieving last %d day(s) memories for user: %s", days, user_id)
        
        # Calculate start time (N days ago at 00:00:00)
        start_date = datetime.now() - timedelta(days=days-1)
        start_timestamp = start_date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        
        # End time is now
        end_timestamp = datetime.now().timestamp()
        
        try:
            # Query memories for the user created within the specified time range
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
            if results["documents"]:
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
