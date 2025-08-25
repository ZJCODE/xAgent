import logging
from typing import List, Dict, Any, Optional
from langfuse.openai import AsyncOpenAI
from langfuse import observe

from ...schemas.memory import MemoryExtraction, MetaMemory, MetaMemoryPiece, MetaMemoryType, QueryPreprocessResult


class MemoryLLMService:
    """LLM service for memory-related operations including extraction, meta-memory generation, and query preprocessing."""
    
    def __init__(self, model: str = "gpt-4.1-mini"):
        """Initialize the LLM service.
        
        Args:
            model: OpenAI model to use for LLM operations
        """
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.openai_client = AsyncOpenAI()
        self.model = model
        self.logger.info("MemoryLLMService initialized with model: %s", model)
    
    @observe()
    async def extract_memories_from_content(self, content: str) -> MemoryExtraction:
        """Extract structured memories from raw content using LLM.
        
        Args:
            content: Raw content to extract memories from
            
        Returns:
            MemoryExtraction object containing extracted memory pieces
        """
        self.logger.debug("Extracting memories from content, length: %d", len(content))
        
        system_prompt = """You are an expert memory extraction system. Your task is to analyze the given content and extract meaningful memory pieces that can be stored for future reference.

For each piece of extracted memory, classify it into one of these types:
- PROFILE: Knowledge about users, preferences, personal information
- EPISODIC: Past interactions and experiences with timestamps
- SEMANTIC: General world knowledge, facts, concepts
- PROCEDURAL: How-to instructions, tool usage patterns

Guidelines:
1. Extract multiple memory pieces if the content contains diverse information
2. Each memory piece should be self-contained and meaningful
3. Write memory content in SIMPLE, CLEAR sentences
4. Use straightforward language that is easy to understand
5. Avoid complex or technical jargon unless necessary
6. Keep each memory piece concise but complete
7. Don't extract memories for trivial or temporary information
8. Focus on information that would be useful for future interactions

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

    @observe()
    async def extract_meta_memory_from_recent(self, recent_memories: List[Dict[str, Any]]) -> MetaMemory:
        """Extract meta-level insights from recent memories using LLM.
        
        Args:
            recent_memories: List of recent memory objects to analyze
        
        Returns:
            MetaMemory object containing extracted insights
        """
        self.logger.debug("Extracting meta memory from %d memories", len(recent_memories))
        
        if not recent_memories:
            self.logger.debug("No memories provided for meta extraction")
            return MetaMemory(contents=[])
        
        # Combine all memory contents for analysis
        memory_contents = []
        for memory in recent_memories:
            content = memory["content"]
            memory_type = memory["metadata"].get("memory_type", "unknown")
            created_at = memory["metadata"].get("created_at", "unknown")
            memory_contents.append(f"[{memory_type.upper()}] ({created_at}): {content}")
        
        combined_content = "\n".join(memory_contents)
        
        system_prompt = """You are an expert meta-memory analyst. Your task is to analyze a collection of memories and extract high-level insights, patterns, and meta-information about the user's activities.

Generate a list of specific meta-memory pieces that capture different aspects or insights. Each piece should be classified as META type and focus on creating insights such as:
- Overall themes or patterns in the user's activities
- User's mood, preferences, or behavioral patterns observed
- Important relationships or connections between different memories
- Insights about the user's goals, interests, or decision-making patterns
- Notable changes in behavior or preferences compared to typical patterns
- Summary of key achievements, challenges, or experiences
- Emerging trends or patterns in the user's interactions
- Temporal patterns or evolution across the activities

Each meta-memory piece should be:
1. High-level and synthesized (not just a summary of individual memories)
2. Focused on patterns, insights, and meta-information
3. Useful for understanding the user's overall context and state
4. Written in a way that would be valuable for future interactions

If the memories are too sparse or unrelated to generate meaningful meta-insights, create a few basic content pieces about the key themes."""

        user_prompt = f"""Analyze these memories and extract meta-level insights:

Memories:
{combined_content}

Extract meaningful meta-memory insights about patterns, themes, user state, and high-level observations from the user's activities. Each insight should be classified as META type."""

        try:
            response = await self.openai_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                text_format=MetaMemory,
                temperature=0.3
            )
            
            extracted_meta = response.output_parsed
            self.logger.debug("Successfully extracted meta memory from %d memories", len(recent_memories))
            return extracted_meta
        except Exception as e:
            self.logger.error("Error extracting meta memory: %s", str(e))
            # Fallback: return basic meta content pieces
            memory_types = ', '.join(set(m['metadata'].get('memory_type', 'unknown') for m in recent_memories))
            return MetaMemory(
                contents=[
                    MetaMemoryPiece(
                        content=f"{len(recent_memories)} activities were recorded across various contexts", 
                        type=MetaMemoryType.META
                    ),
                    MetaMemoryPiece(
                        content=f"Memory types included: {memory_types}",
                        type=MetaMemoryType.META
                    )
                ]
            )

    @observe()
    async def preprocess_query(self, query: str, query_context: Optional[str] = None, enable:bool = False) -> QueryPreprocessResult:
        """Preprocess query to generate variations and extract keywords for better memory retrieval.
        
        Args:
            query: Original query string
            query_context: Additional context about the query (conversation history, current task, etc.)
            
        Returns:
            QueryPreprocessResult containing original query and variations
        """

        if not enable:
            return QueryPreprocessResult(
                original_query=query,
                rewritten_queries=[]
            )

        self.logger.debug("Preprocessing query: %s", query[:100] + "..." if len(query) > 100 else query)
        
        # Import datetime to get current date
        from datetime import datetime
        current_date = datetime.now().strftime("%Y-%m-%d (%A)")
        
        # Embed current date in context
        date_context = f"Current date: {current_date}"
        if query_context:
            query_context = f"{date_context}\n{query_context}"
            self.logger.debug("Using query context with current date for preprocessing, length: %d", len(query_context))
        else:
            query_context = date_context
            self.logger.debug("Using current date as query context for preprocessing")
        
        system_prompt = """You are an expert query preprocessing system. Your task is to analyze the given query and determine if it needs context-based rewriting for better memory retrieval.

**CRITICAL PRINCIPLE**: Only generate rewritten queries when the original query is genuinely ambiguous and CANNOT be understood without external context.

**When to Rewrite (generate 2-3 rewritten queries)**:
- The query contains pronouns that refer to someone/something NOT mentioned in the query itself (e.g., "what did he say?" where "he" is not identified)
- The query uses relative time expressions that cannot be resolved without knowing the current context (e.g., "what happened yesterday?" without any date reference)
- The query refers to previous conversations or events that are not self-contained (e.g., "continue that discussion", "what was the outcome?")
- The query is clearly a follow-up that depends on prior context (e.g., "and then what?", "how about the other one?")

**When NOT to Rewrite (return EMPTY list)**:
- Instructions, commands, or statements that are complete (e.g., "your name is X", "start doing Y")
- Questions that are self-contained and clear (e.g., "how to use Python", "what is machine learning")
- Queries with specific topics, names, or concepts that don't need external reference
- Simple expressions, acknowledgments, or standalone statements
- ANY query that makes sense by itself, even if it could theoretically be expanded

**IMPORTANT**: Err on the side of NOT rewriting. Only rewrite if the query is truly incomprehensible without additional context."""

        # Build user prompt with context if available
        user_prompt = f"""Analyze this query and determine if it needs context-based rewriting:

Context: {query_context}

Query: "{query}"

CRITICAL DECISION: Is this query genuinely ambiguous and incomprehensible without external context?

Ask yourself:
1. Can I understand what this query means just by reading it?
2. Does it contain unclear pronouns that refer to unidentified entities?
3. Is it a fragment that depends on previous conversation?

- If the query is clear and self-contained (even if simple): Return EMPTY list
- If the query genuinely cannot be understood without context: Generate 2-3 rewritten queries

Examples of queries that NEED rewriting:
- "what did he tell you?" (who is "he"?)
- "continue from where we left off" (what previous discussion?)
- "how did that turn out?" (what specific event?)

Examples of queries that DON'T NEED rewriting:
- "from now on, your name is X" (clear instruction)
- "Python programming tips" (clear topic)  
- "what is machine learning" (complete question)
- "start the process" (clear command)
- "thanks" (simple expression)"""

        try:
            response = await self.openai_client.responses.parse(
                model="gpt-4.1-nano",
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
