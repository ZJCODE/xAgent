"""Run AgentHTTPServer with a pre-configured local agent."""

from xagent.components import MessageStorageLocal
from xagent.core import Agent
from xagent.interfaces.server import AgentHTTPServer
from xagent.utils import function_tool


@function_tool()
def release_window(environment: str) -> str:
    """Return the standard release window for an environment."""
    windows = {
        "dev": "Weekdays, any time",
        "staging": "Weekdays before 17:00",
        "production": "Tuesday and Thursday at 10:00 local time",
    }
    return windows.get(environment.lower(), "Unknown environment")


def create_custom_agent() -> Agent:
    return Agent(
        name="release_assistant",
        system_prompt="You help teams answer release-process questions with short, direct responses.",
        model="gpt-5-mini",
        tools=[release_window],
        message_storage=MessageStorageLocal(),
    )


def main():
    agent = create_custom_agent()
    server = AgentHTTPServer(agent=agent)

    print("Starting server on http://localhost:8010")
    print("Example POST body:")
    print(
        '{'
        '"user_id":"demo_user",'
        '"session_id":"release_ops",'
        '"user_message":"When can I deploy to production?"'
        '}'
    )

    server.run(host="localhost", port=8010)


if __name__ == "__main__":
    main()
