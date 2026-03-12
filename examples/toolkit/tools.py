import asyncio
from collections import Counter

from xagent.utils import function_tool

@function_tool()
def char_count(text: str) -> dict:
    """Count the frequency of each character in a string."""
    if not text:
        return {}
    return dict(Counter(text))

@function_tool()
async def lookup_ticket_status(ticket_id: str) -> str:
    """Return a simulated support ticket status."""
    await asyncio.sleep(0.1)
    return f"Ticket {ticket_id}: in_progress"
