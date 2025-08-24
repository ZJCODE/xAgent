import chromadb
import chromadb.utils.embedding_functions as embedding_functions
import logging
import os
import uuid
from typing import List, Optional, Dict, Any
from datetime import datetime
import dotenv
from openai import AsyncOpenAI

from ...schemas.memory import MemoryType, MemoryExtraction,QueryPreprocessResult
from .base_memory import MemoryStore

dotenv.load_dotenv(override=True)

class LocalMemory(MemoryStore):
    """Local memory implementation using ChromaDB with LLM-powered memory extraction."""
    
    def __init__(self, collection_name: str = "xagent_memory"):
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Initialize OpenAI client
        self.openai_client = AsyncOpenAI()
        self.model = "gpt-4.1-mini"

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
        extracted_memories = await self.extract_memories_from_content(content)
        
        # Store each extracted memory piece
        memory_ids = []
        documents = []
        metadatas = []
        for memory_piece in extracted_memories.memories:
            memory_id = str(uuid.uuid4())
            
            # Prepare metadata
            meta = {
                "user_id": user_id, 
                "created_at": datetime.now().isoformat(),
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
            meta = {
                "user_id": user_id, 
                "created_at": datetime.now().isoformat(),
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
        preprocessed = await self.preprocess_query(query)
        
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

    async def extract_memories_from_content(self, content: str) -> MemoryExtraction:
        """Extract structured memories from raw content using LLM."""
        self.logger.debug("Extracting memories from content, length: %d", len(content))
        
        system_prompt = """You are an expert memory extraction system. Your task is to analyze the given content and extract meaningful memory pieces that can be stored for future reference.

For each piece of extracted memory, classify it into one of these types:
- WORKING: Short-term, task or session-specific memory
- PROFILE: Knowledge about users, preferences, personal information
- EPISODIC: Past interactions and experiences with timestamps
- SEMANTIC: General world knowledge, facts, concepts
- PROCEDURAL: How-to instructions, tool usage patterns
- META: Memory about memory itself, patterns about the user's behavior

Guidelines:
1. Extract multiple memory pieces if the content contains diverse information
2. Each memory piece should be self-contained and meaningfull
3. Don't extract memories for trivial or temporary information
4. Focus on information that would be useful for future interactions

If the content doesn't contain any meaningful information worth storing as memory, return an empty list."""

        user_prompt = f"Analyze this content and extract meaningful memories:\n\n{content}"

        try:
            response = await self.openai_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                text_format=MemoryExtraction,
                temperature=0.3
            )
            
            extracted = response.output_parsed
            self.logger.debug("Successfully extracted %d memory pieces", len(extracted.memories))
            return extracted
        except Exception as e:
            self.logger.error("Error extracting memories: %s", str(e))
            # Fallback: return empty extraction
            return MemoryExtraction(memories=[])
        

    async def preprocess_query(self, query: str) -> QueryPreprocessResult:
        """Preprocess query to generate variations and extract keywords for better memory retrieval."""
        self.logger.debug("Preprocessing query: %s", query[:100] + "..." if len(query) > 100 else query)
        
        system_prompt = """You are an expert query preprocessing system. Your task is to analyze the given query and generate:

**Rewritten Queries**: Create 2-4 alternative formulations of the query that capture the same intent but use different words or phrasing. This helps retrieve semantically similar memories even when exact wording doesn't match.

Guidelines:
- Rewritten queries should maintain the original intent while using different vocabulary
- Include both more specific and more general variations
"""

        user_prompt = f"Preprocess this query for memory retrieval:\n\nQuery: {query}"

        try:
            response = await self.openai_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                text_format=QueryPreprocessResult,
                temperature=0.3
            )
            
            preprocessed = response.output_parsed
            if preprocessed:
                # Ensure original_query is set
                preprocessed.original_query = query
                self.logger.debug("Successfully preprocessed query: %d rewritten queries", 
                                len(preprocessed.rewritten_queries))
                return preprocessed
            else:
                # Fallback if parsing failed
                self.logger.warning("Query preprocessing returned no result, using fallback")
                return QueryPreprocessResult(
                    original_query=query,
                    rewritten_queries=[]
                )
                
        except Exception as e:
            self.logger.error("Error preprocessing query: %s", str(e))
            # Fallback: return basic preprocessing
            return QueryPreprocessResult(
                original_query=query,
                rewritten_queries=[]
            )
