"""Run a demo server that showcases realtime tools and background Responses jobs.

This example is the recommended companion for:
    - examples/demo/realtime_client.py
    - examples/demo/realtime_audio_sender.py
"""

from __future__ import annotations

import argparse

from xagent.components import MessageStorageLocal
from xagent.core import Agent
from xagent.interfaces.server import AgentHTTPServer
from xagent.tools import web_search
from xagent.utils import function_tool


@function_tool(
    name="pause_music",
    description="Pause local music playback immediately.",
    tier="realtime",
    timeout_seconds=1.0,
)
def pause_music(text: str = "") -> str:
    """Pause music playback on the local device."""
    return "Music playback paused."


@function_tool(
    name="set_status_light",
    description="Set the local status light to a named color.",
    tier="realtime",
    timeout_seconds=1.0,
)
def set_status_light(text: str = "") -> str:
    """Set the local status light color based on the user's text."""
    normalized = text.lower()
    if "green" in normalized:
        color = "green"
    elif "red" in normalized:
        color = "red"
    elif "amber" in normalized or "yellow" in normalized:
        color = "amber"
    elif "blue" in normalized:
        color = "blue"
    else:
        color = "white"
    return f"Status light set to {color}."


def create_showcase_agent() -> Agent:
    return Agent(
        name="realtime_showcase",
        system_prompt=(
            "You are the xAgent realtime showcase assistant. "
            "Use concise answers. For research requests and current events, "
            "use available Responses tools when appropriate."
        ),
        model="gpt-4.1-mini",
        tools=[pause_music, set_status_light, web_search],
        message_storage=MessageStorageLocal(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the xAgent realtime showcase server")
    parser.add_argument("--host", default="localhost", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8010, help="Port to bind to")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    agent = create_showcase_agent()
    server = AgentHTTPServer(agent=agent)

    print(f"Starting realtime showcase server on http://{args.host}:{args.port}")
    print(f"Realtime websocket: ws://{args.host}:{args.port}/realtime/ws")
    print("Companion clients:")
    print(f"  python examples/demo/realtime_client.py --ws-url ws://{args.host}:{args.port}/realtime/ws")
    print(
        "  python examples/demo/realtime_audio_sender.py "
        f"--ws-url ws://{args.host}:{args.port}/realtime/ws --file /path/to/audio.raw"
    )
    print("Suggested commands inside realtime_client.py:")
    print("  /tool pause_music pause the studio speakers")
    print("  /tool set_status_light set the light to amber")
    print("  search the latest AI agent framework news")
    print("  /interrupt")

    server.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
