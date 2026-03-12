"""
Swarm — multi-agent collaboration built on top of Agent.as_tool().

Each member agent is converted to a tool and registered on a central
coordinator agent.  The coordinator decides when to call each specialist
and synthesises their results into a final answer.

Example
-------
::

    from xagent import Agent
    from xagent.multi.swarm import Swarm

    researcher = Agent(name="researcher", system_prompt="You research topics.")
    writer = Agent(name="writer", system_prompt="You write clear summaries.")

    swarm = Swarm(agents=[researcher, writer])
    result = await swarm.run(
        task="Research quantum computing and write a summary.",
        user_id="user_1",
        session_id="session_abc",
    )
    print(result)
"""

from __future__ import annotations

import uuid
import logging
from typing import List, Optional

from ..core.agent import Agent


class Swarm:
    """
    Coordinate multiple specialist agents through a single coordinator.

    Every agent in *agents* is converted to an OpenAI tool via
    ``Agent.as_tool()`` and registered on the *coordinator*.  The
    coordinator receives the task and orchestrates the specialists
    automatically, returning a synthesised result.

    Parameters
    ----------
    agents:
        Specialist agents that the coordinator can call as tools.
    coordinator:
        Optional pre-built coordinator agent.  When *None* a default
        coordinator is created automatically.
    """

    def __init__(
        self,
        agents: List[Agent],
        coordinator: Optional[Agent] = None,
    ) -> None:
        if not agents:
            raise ValueError("Swarm requires at least one specialist agent.")

        self.agents = agents
        self.logger = logging.getLogger(self.__class__.__name__)

        # Convert specialist agents to tools
        specialist_tools = [agent.as_tool() for agent in agents]
        agent_names = [a.name for a in agents]

        if coordinator is not None:
            self.coordinator = coordinator
            self.coordinator._register_tools(specialist_tools)
        else:
            self.coordinator = Agent(
                name="swarm_coordinator",
                system_prompt=(
                    "You are a coordinator in a swarm of specialist agents. "
                    f"You have access to the following specialists: {agent_names}. "
                    "Break the task into sub-tasks, delegate to the appropriate "
                    "specialists via their tools, and synthesise their outputs "
                    "into a comprehensive final answer."
                ),
                tools=specialist_tools,
            )

        self.logger.info(
            "Swarm initialised with %d specialist(s): %s — coordinator: %s",
            len(agents),
            agent_names,
            self.coordinator.name,
        )

    async def __call__(self, task: str, user_id: str = "swarm_user", session_id: Optional[str] = None) -> str:
        return await self.run(task=task, user_id=user_id, session_id=session_id)

    async def run(
        self,
        task: str,
        user_id: str = "swarm_user",
        session_id: Optional[str] = None,
    ) -> str:
        """
        Execute the swarm on *task* and return the coordinator's reply.

        Parameters
        ----------
        task:
            The high-level task description.
        user_id:
            User identifier forwarded to the coordinator agent.
        session_id:
            Session identifier; a random UUID is used when *None*.

        Returns
        -------
        str
            The coordinator's final synthesised answer.
        """
        session_id = session_id or str(uuid.uuid4())
        self.logger.info("Swarm.run — task: %.80s…  user=%s", task, user_id)
        return await self.coordinator.chat(
            user_message=task,
            user_id=user_id,
            session_id=session_id,
        )
