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
        
        system_prompt = """You are an expert memory extraction system. Your task is to analyze conversation content and extract ONLY the truly important memory pieces from the LAST user message that should be remembered long-term.

KEY INSTRUCTION: When given conversation format with multiple exchanges, ONLY extract memories from the FINAL user message. Previous messages provide context to help understand the final message better.

FOCUS ON THE LAST USER MESSAGE - EXTRACT ONLY WHAT'S TRULY WORTH REMEMBERING:

**CRITICAL PRINCIPLE**: Be HIGHLY SELECTIVE. Only extract information from the final user message that is genuinely important for future interactions with this specific user.

**CONTEXT USAGE**: Use previous messages to understand the context and timing of the final user message, but do NOT extract memories from previous messages.

1. **PROFILE**: Only extract NEW or UPDATED personal information from the last user message
   - Personal habits, routines, or lifestyle patterns mentioned in the final message
   - Preferences about activities, food, exercise, or daily life from the final message
   - Health-related activities or constraints mentioned in the final message
   - Personal goals or commitments expressed in the final message
   - Regular activities or schedules revealed in the final message

2. **EPISODIC**: Only extract significant activities or plans from the last user message
   - Specific plans or activities mentioned for today/tonight/specific times
   - Important events or commitments the user shared in the final message
   - Routine activities that show patterns (like regular exercise)
   - Meal plans or food choices that might indicate preferences
   - Exercise routines or fitness activities mentioned

**CONTEXT INTEGRATION**: 
- Use previous messages to understand WHEN the final user message refers to (e.g., if previous message mentioned "tonight" and final message mentions exercise, understand it's for tonight)
- Use previous messages to understand the SETTING or SITUATION of the final user message
- Extract temporal context from the conversation flow to make the final message more meaningful

**STRICT EXTRACTION CRITERIA**:
- IGNORE: Previous user messages and ALL assistant responses (they are context only)
- IGNORE: Casual mentions that don't reveal patterns or preferences
- EXTRACT: Activities from the final message that show user's lifestyle patterns
- EXTRACT: Plans from the final message that indicate user's priorities or routines
- EXTRACT: Information from the final message that would help personalize future interactions

**EXAMPLES**:
- If conversation shows "I want fried chicken tonight" then "I also need to use the elliptical machine", extract: "User plans to exercise on elliptical machine tonight" (EPISODIC) and "User does elliptical machine exercise" (PROFILE if it suggests a routine)
- If user mentions specific meal + exercise combo, this might indicate a lifestyle pattern worth remembering

**QUALITY OVER QUANTITY**: 
- Better to extract NOTHING than to extract trivial information
- Focus on information from the final message that reveals user's habits, preferences, or important activities
- Consider temporal context from previous messages to make the final message more meaningful"""

        user_prompt = f"""Analyze the conversation below and extract ONLY truly important information from the LAST user message. Use previous messages as context to understand timing and situation, but extract memories ONLY from the final user message.

Conversation:
{content}

Extract meaningful memories from the LAST user message only. Use previous messages to understand context (like timing, situation) but do not extract from them."""

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
        
        system_prompt = """You are an expert meta-memory analyst. Your task is to analyze a collection of memories and extract high-level insights, patterns, and meta-information focusing on the user's PROFILE and EPISODIC experiences.

ANALYZE PATTERNS ACROSS TWO CORE MEMORY TYPES:

**PROFILE PATTERNS**:
- Consistent preferences, traits, and characteristics emerging across interactions
- Evolution of user's personal information, preferences, or habits over time
- Behavioral patterns and communication style preferences
- Personal context patterns that affect interaction quality
- Lifestyle patterns, routines, and personal circumstances
- Relationship patterns and social interaction preferences
- Goals, aspirations, and value patterns expressed over time

**EPISODIC PATTERNS**:
- Recurring themes in user's experiences and interactions
- Temporal patterns in user's activities, needs, and requests
- Emotional states and satisfaction patterns across different interactions
- Notable achievements, challenges, or milestone events
- Problem-solving patterns and help-seeking behaviors
- Feedback patterns and service interaction outcomes
- Seasonal or cyclical patterns in user's activities
- Evolution of user's experiences and interaction success

GENERATE META-INSIGHTS ABOUT:
- Overall themes connecting PROFILE and EPISODIC information
- User's evolving personal context and interaction patterns
- Patterns that would improve future personalization and interaction quality
- Important connections between user's personal characteristics and their experiences
- Notable changes or consistency in user behavior, preferences, and life circumstances
- Key insights about the user's personal journey, growth, and changing needs
- Relationship between user's stated preferences (PROFILE) and actual experiences (EPISODIC)

Each meta-memory piece should be:
1. High-level and synthesized (not just summaries of individual memories)
2. Focused on patterns across PROFILE and EPISODIC domains
3. Useful for understanding the user's comprehensive personal context
4. Written to enhance future personalized interactions based on learned patterns
5. Connecting personal characteristics with actual experiences and outcomes

If the memories don't reveal meaningful patterns across PROFILE and EPISODIC types, create basic insights about the key personal themes present."""

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
- The query uses relative time expressions that need current date context to be specific (e.g., "yesterday", "tomorrow", "last week", "day after tomorrow", "next month")
- The query refers to previous conversations or events that are not self-contained (e.g., "continue that discussion", "what was the outcome?")
- The query is clearly a follow-up that depends on prior context (e.g., "and then what?", "how about the other one?", "what about it?" as a follow-up)
- The query is a fragment or incomplete expression that needs context (e.g., "day after tomorrow?", "that one?", "how about it?")

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
3. Does it contain relative time expressions that need current date context (like "yesterday", "tomorrow", "day after tomorrow", "last week")?
4. Is it a fragment or follow-up that depends on previous conversation?

- If the query is clear and self-contained (even if simple): Return EMPTY list
- If the query contains relative time expressions or genuinely cannot be understood without context: Generate 2-3 rewritten queries

Examples of queries that NEED rewriting:
- "what did he tell you?" (who is "he"?)
- "continue from where we left off" (what previous discussion?)
- "how did that turn out?" (what specific event?)
- "tomorrow's weather" (which specific date?)
- "day after tomorrow?" (what about the day after tomorrow - which date?)
- "yesterday's meeting" (which specific date?)

Examples of queries that DON'T NEED rewriting:
- "from now on, your name is X" (clear instruction)
- "Python programming tips" (clear topic)  
- "what is machine learning" (complete question)
- "start the process" (clear command)
- "thanks" (simple expression)
- "weather on 2025-08-27" (specific date given)"""

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