import logging
from typing import List, Dict, Any, Optional
from langfuse.openai import AsyncOpenAI
from langfuse import observe

from ....schemas.memory import MemoryExtraction, MetaMemory, MetaMemoryPiece, MetaMemoryType, QueryPreprocessResult


class MemoryLLMService:
    """LLM service for memory-related operations including extraction, meta-memory generation, and query preprocessing."""

    def __init__(self, model: str = "gpt-4.1-mini", mini_model: str = "gpt-4.1-nano"):
        """Initialize the LLM service.
        
        Args:
            model: OpenAI model to use for LLM operations
        """
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.openai_client = AsyncOpenAI()
        self.model = model
        self.mini_model = mini_model
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
        
        # Get current date for context
        from datetime import datetime
        current_date = datetime.now().strftime("%Y-%m-%d")
        

        system_prompt = f"""You are an expert memory extraction system. Your task is to analyze conversation content and extract truly important memory pieces that should be remembered long-term.

CURRENT DATE: {current_date}

Use this date to properly contextualize time references in the conversation (e.g., "tonight", "tomorrow", "yesterday").

**CRITICAL PRINCIPLE**: Be HIGHLY SELECTIVE. Only extract information that is genuinely important for future interactions with this specific user.

**LANGUAGE REQUIREMENT**: Extract and store memories in the user's original language.

EXTRACT IMPORTANT INFORMATION FROM THE CONVERSATION:

1. **PROFILE**: Extract personal information, preferences, and patterns revealed throughout the conversation
   - Personal habits, routines, or lifestyle patterns mentioned
   - Preferences about activities, food, exercise, or daily life
   - Health-related activities or constraints mentioned
   - Personal goals or commitments expressed
   - Regular activities or schedules revealed
   - Skills, expertise, or professional information shared
   - Personal relationships or family information mentioned

2. **EPISODIC**: Extract significant activities, plans, or events from the conversation
   - Specific plans or activities mentioned for today/tonight/specific times
   - Important events or commitments the user shared
   - Routine activities that show patterns (like regular exercise)
   - Meal plans or food choices that might indicate preferences
   - Exercise routines or fitness activities mentioned
   - Meetings, appointments, or scheduled activities
   - **DATE & TIME FORMAT**: If the activity/plan involves a specific date or time, include both in the content:
     * Date: YYYY-MM-DD format
     * Time: Include specific times like "8:00 PM", "tonight", "morning", etc.
     * Example: "User plans to exercise at 8:00 PM on 2025-08-25" or "User has dinner tonight on 2025-08-25"

**CONTEXT INTEGRATION**: 
- Use the full conversation to understand context, timing, and relationships between different pieces of information
- Extract temporal context from the conversation flow to make information more meaningful
- Connect related information mentioned across different parts of the conversation
- **IMPORTANT**: For EPISODIC memories involving dates or times, always include the specific date in YYYY-MM-DD format and preserve any time information in the content

**EXTRACTION CRITERIA**:
- FOCUS: User messages that reveal personal information, preferences, plans, or important activities
- IGNORE: Assistant responses unless they contain user-confirmed information
- IGNORE: Casual mentions that don't reveal patterns or preferences
- EXTRACT: Information that would help personalize future interactions
- EXTRACT: Plans, activities, and commitments that show user's lifestyle and priorities
- EXTRACT: Personal context that affects how the user prefers to interact

**EXAMPLES**:
- If user mentions "I exercise every morning at 7 AM", extract: "User exercises every morning at 7:00 AM" (PROFILE)
- If user says "I have a meeting tomorrow at 3:00 PM", extract: "User has meeting at 3:00 PM on 2025-08-26" (EPISODIC)
- If user mentions "I'm vegetarian and prefer Italian food", extract: "User is vegetarian and prefers Italian food" (PROFILE)
- If user says "I work as a software engineer", extract: "User works as a software engineer" (PROFILE)
- For date and time-specific activities, always include both: "User has appointment at 10:00 AM on 2025-08-30" not "User has appointment next week"

**QUALITY OVER QUANTITY**: 
- Better to extract NOTHING than to extract trivial information
- Focus on information that reveals user's habits, preferences, important activities, or personal context
- Prioritize information that would improve future personalized interactions"""

        user_prompt = f"""Analyze the conversation below and extract truly important information that should be remembered for future interactions.

Conversation:
{content}

Extract meaningful memories from the conversation that reveal the user's preferences, habits, plans, and important personal context. All extracted memories must be in the user's original language as used in the conversation."""

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
    async def preprocess_query(self, query: str, query_context: Optional[str] = None, enable: bool = False) -> QueryPreprocessResult:
        """Preprocess query using LLM to generate variations and extract keywords for better memory retrieval.
        
        Args:
            query: Original query string
            query_context: Additional context about the query (conversation history, current task, etc.)
            enable: Whether to enable query preprocessing
            
        Returns:
            QueryPreprocessResult containing original query, variations, and keywords
        """
        
        if not enable:
            return QueryPreprocessResult(
                original_query=query,
                rewritten_queries=[],
                keywords=[]
            )

        self.logger.debug("Preprocessing query for memory search: %s", query[:100] + "..." if len(query) > 100 else query)
        
        from datetime import datetime
        
        current_date = datetime.now().strftime("%Y-%m-%d (%A)")
        
        # Prepare context
        context_info = f"Current date: {current_date}"
        if query_context:
            context_info = f"{context_info}\nContext: {query_context}"
        
        system_prompt = f"""You are a query preprocessor for memory search optimization. Current date: {current_date}

TASKS:
1. Extract 2-4 key searchable terms (entities, concepts, dates)
2. Rewrite queries that need clarification or completion based on context

KEYWORD RULES:
- Convert relative time → specific dates ("tomorrow" → "2025-08-27")
- Extract meaningful content words, not stop words
- Support multilingual queries (Chinese, English, etc.)

REWRITE RULES - Rewrite when query has:
- Incomplete questions needing context inference ("this morning?" + previous "what did I do yesterday" → "what did I do this morning")
- Follow-up questions ("what about today" after "what did I do yesterday" → "what did I do today")
- Pronouns with known referents ("he said" → "John said")
- Relative time ("yesterday" → "2025-08-26")
- Vague references ("that meeting" → specific meeting name)
- If query is complete and clear return empty list for rewritten_queries
- Never answer the query, only rewrite if necessary

NEVER ANSWER THE QUERY OR RAISE QUESTIONS. ONLY PREPROCESS FOR SEARCH.
"""

        user_prompt = f"""Context: {context_info}

Query to preprocess: "{query}"

Task: Extract keywords and rewrite query if it needs completion based on conversation context. If this is a follow-up question (like "this morning?" after "what did I do yesterday"), infer the complete intent from the conversation pattern. Do not answer the question."""

        try:
            response = await self.openai_client.responses.parse(
                model=self.mini_model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                text_format=QueryPreprocessResult,
                temperature=0.3
            )
            
            result = response.output_parsed
            if result:
                result.original_query = query
                self.logger.debug("Query preprocessing successful: %d rewritten queries, %d keywords", 
                                len(result.rewritten_queries), len(result.keywords))
                return result
            else:
                # Fallback
                self.logger.warning("Query preprocessing failed, using fallback")
                return QueryPreprocessResult(
                    original_query=query,
                    rewritten_queries=[],
                    keywords=[]
                )
                
        except Exception as e:
            self.logger.error("Error in query preprocessing: %s", str(e))
            return QueryPreprocessResult(
                original_query=query,
                rewritten_queries=[],
                keywords=[]
            )

    @observe()
    async def merge_memories(self, extracted_memories: 'MemoryExtraction', related_memories: List[Dict[str, Any]]) -> MemoryExtraction:
        """Merge extracted memories with related existing memories using LLM.
        
        Args:
            extracted_memories: MemoryExtraction object containing new memories to merge
            related_memories: List of related existing memories for all extracted memories
            
        Returns:
            MemoryExtraction object containing the merged/updated memories
        """
        self.logger.debug("Merging %d extracted memories with %d related memories", 
                         len(extracted_memories.memories), len(related_memories))
        
        if not extracted_memories.memories:
            # No extracted memories, return empty
            return MemoryExtraction(memories=[])
        
        if not related_memories:
            # No related memories, return extracted memories as-is
            return extracted_memories
        
        # Get current date for context
        from datetime import datetime
        current_date = datetime.now().strftime("%Y-%m-%d")
        
        # Format extracted memories for LLM
        extracted_memory_list = []
        for i, memory_piece in enumerate(extracted_memories.memories):
            extracted_memory_list.append(f"Memory {i+1}:\nType: {memory_piece.type.value}\nContent: {memory_piece.content}")
        
        extracted_memories_text = "\n\n".join(extracted_memory_list)
        
        # Format related memories for LLM
        related_memory_list = []
        for i, memory in enumerate(related_memories):
            content = memory.get("content", "")
            metadata = memory.get("metadata", {})
            memory_type = metadata.get("memory_type", "unknown")
            created_at = metadata.get("created_at", "unknown")
            related_memory_list.append(f"Type: {memory_type}\nCreated: {created_at}\nContent: {content}")
        
        related_memories_text = "\n\n".join(related_memory_list)
        
        system_prompt = f"""You are an expert memory fusion system. Your task is to intelligently merge a new memory with related existing memories.

CURRENT DATE: {current_date}

**CRITICAL PRINCIPLE**: Be HIGHLY SELECTIVE and maintain FOCUSED, SPECIFIC memories. Avoid creating overly long or mixed-topic memories.

**CORE PRINCIPLES**:
1. **PRESERVE VALUABLE INFORMATION**: Never lose important details from either new or existing memories
2. **ELIMINATE REDUNDANCY**: Merge similar or overlapping information efficiently
3. **MAINTAIN ACCURACY**: Ensure temporal accuracy and factual consistency
4. **RESOLVE CONFLICTS**: When contradictory information exists, prioritize newer information
5. **KEEP MEMORIES FOCUSED**: Each memory should focus on ONE specific topic or closely related preferences
6. **AVOID INFORMATION OVERLOAD**: Don't combine unrelated preferences into one memory piece

**MEMORY QUALITY STANDARDS**:
- **FOCUSED**: Each memory should cover ONE specific aspect (e.g., food preferences, exercise habits, communication style)
- **CONCISE**: Keep memories brief and to the point, not paragraph-long combinations
- **SPECIFIC**: Focus on specific preferences, habits, or activities rather than general statements
- **SEPARABLE**: If information can be logically separated into different topics, create separate memories

**MERGE STRATEGIES**:

**FOR PROFILE MEMORIES**:
- **TOPIC-SPECIFIC MERGING**: Only merge memories about the SAME specific topic
  - Food preferences: "User is vegetarian" + "User likes Italian food" → "User is vegetarian and prefers Italian food"
  - Exercise: "User exercises daily" + "User prefers morning workouts" → "User exercises daily, preferring morning workouts"
  - Communication: "User likes warm conversations" + "User prefers gentle communication" → "User prefers warm and gentle communication"
- **SEPARATE DIFFERENT TOPICS**: Keep different preference categories as separate memories
  - DON'T merge: "User likes reading" + "User doesn't like fishing" + "User enjoys sunny weather" → Create 3 separate memories
- **RESOLVE CONFLICTS**: When memories contradict each other on the SAME topic, use newer information
- **AVOID MIXING**: Don't create memories that mix communication preferences, activity preferences, and weather preferences

**FOR EPISODIC MEMORIES**:
- **SAME EVENT/TIME**: Only merge activities that happened at the same time or are part of the same event
- **SEPARATE EVENTS**: Keep different activities/events as separate memories even if they happened on the same day
- **TEMPORAL ACCURACY**: Preserve specific dates and times

**SEPARATION CRITERIA** - Create SEPARATE memories when topics are:
- Different activity categories (reading vs hiking vs fishing)
- Different preference types (food vs weather vs communication)
- Different time periods or events
- Unrelated personal characteristics

**CONFLICT RESOLUTION RULES**:
1. **DIRECT CONTRADICTIONS**: When memories directly contradict on the SAME topic, ONLY keep the newer information
2. **PREFERENCE UPDATES**: Treat newer preferences as updates that replace older conflicting ones
3. **TEMPORAL PRIORITY**: More recent information takes precedence over older information
4. **NO CONTRADICTION PRESERVATION**: Never output memories that contain contradictory statements about the same topic

**OUTPUT REQUIREMENTS**:
- Each memory must be focused on ONE specific topic or closely related aspects
- Keep memories concise and specific
- Preserve temporal information (dates, times) accurately
- Maintain the user's original language
- Use appropriate memory type (PROFILE/EPISODIC)
- **NEVER mix unrelated topics in the same memory piece**
- **PREFER multiple focused memories over one comprehensive memory**

**LANGUAGE**: Maintain the user's original language throughout the merge process."""

        user_prompt = f"""Merge these extracted memories with the related existing memories:

EXTRACTED MEMORIES TO PROCESS:
{extracted_memories_text}

RELATED EXISTING MEMORIES:
{related_memories_text}

**CRITICAL INSTRUCTIONS**:
1. If there are CONTRADICTIONS (like "user likes X" vs "user doesn't like X"), prioritize the EXTRACTED memories and discard conflicting information from existing memories
2. **KEEP MEMORIES FOCUSED**: Create separate, topic-specific memories rather than mixing unrelated information
3. **AVOID LONG COMBINATIONS**: Don't create paragraph-long memories that mix communication style + activity preferences + weather preferences
4. **SEPARATE BY TOPIC**: If information covers different topics (e.g., reading, fishing, weather, communication), create separate memories for each topic
5. **MERGE SIMILAR TOPICS**: If extracted memories and existing memories cover the same topic, merge them intelligently
6. **PRESERVE ALL VALUE**: Don't lose any valuable information from either extracted or existing memories

**EXAMPLES OF GOOD SEPARATION**:
- Instead of: "User is willing to share feelings and favorite things with assistant, likes warm and kind communication. User doesn't like fishing, likes to feel warm and relaxed on sunny days, tends to enjoy nature and good times, especially sunny weather. User likes reading and hiking."
- Create: 
  * "User is willing to share feelings with assistant and likes warm, kind communication"
  * "User doesn't like fishing"
  * "User likes reading and hiking"
  * "User enjoys sunny weather and tends to appreciate nature and good times"

Analyze relationships between extracted and existing memories, then create the optimal set of FOCUSED, SPECIFIC memories that preserve all valuable information while keeping each memory topic-specific."""

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
            
            merged = response.output_parsed
            self.logger.debug("Successfully merged memories into %d final memories", len(merged.memories))
            return merged
        except Exception as e:
            self.logger.error("Error merging memories: %s", str(e))
            # Fallback: return extracted memories as-is
            return extracted_memories