from ..core.agent import Agent
from ..schemas import Message

class SharedContext:
    """A shared context for managing data and chat history bewteen agents in a swarm."""
    def __init__(self):
        self.histories = {}

class Swarm:
    """
    A Swarm is a system for orchestrating multiple collaborative agents, enabling them to work together as a coordinated team to tackle complex tasks.
    """
    def __init__(self, agents: list[Agent]):
        self.agents = agents

    async def __call__(self, task: Message | str, *args, **kwds):
        return await self.invoke(task, *args, **kwds)

    async def invoke(self, task: Message | str):
        pass

