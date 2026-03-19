from typing import Optional

from xagent.core.handlers.memory import MemoryManager
from xagent.utils.tool_decorator import function_tool


def create_search_journal_memory_tool(
    memory_manager: MemoryManager,
    memory_key: str,
    is_enabled,
):
    @function_tool(
        name="search_journal_memory",
        description=(
            "Search long-term journal memory by keyword or date when the current turn "
            "needs older context. Do not call this on every turn."
        ),
        param_descriptions={
            "query": "Optional keyword query for journal search. Leave empty when only searching by date.",
            "date": "Optional journal date in YYYY-MM-DD format to fetch or narrow the search.",
            "limit": "Maximum number of journal entries to return. Use a small number unless the user requests more.",
        },
    )
    async def search_journal_memory(
        query: str = "",
        date: Optional[str] = None,
        limit: int = 5,
    ) -> dict:
        """Use this only when the user asks about earlier conversations, preferences, plans, or remembered facts."""
        if not is_enabled():
            return {
                "memories": [],
                "enabled": False,
                "message": "Memory retrieval is disabled for this turn.",
            }

        normalized_limit = max(1, min(int(limit), 10))
        results = await memory_manager.search_memories(
            memory_key=memory_key,
            query=query,
            limit=normalized_limit,
            journal_date=date,
        )
        return {
            "memories": results,
            "enabled": True,
        }

    return search_journal_memory
