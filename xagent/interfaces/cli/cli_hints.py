"""User-facing CLI hints for common mistakes."""

from __future__ import annotations

from typing import Optional, Sequence


def misplaced_agent_flag_hint(argv: Sequence[str]) -> Optional[str]:
    """``xagent --agent NAME <command>`` is invalid; flags belong after the subcommand."""
    if len(argv) < 3 or argv[0] != "--agent":
        return None
    command = argv[2]
    name = argv[1]
    templates = {
        "chat": f"xagent chat --agent {name}",
        "world": "xagent world join WORLD --agent NAME\n  xagent world leave --agent NAME",
        "api": f"xagent api start --agent {name}",
        "web": f"xagent web start --agent {name}",
        "setup": f"xagent setup --agent {name}",
        "memory": f"xagent memory list --agent {name}",
        "config": f"xagent config show --agent {name}",
        "inspect": f"xagent inspect identity show --agent {name}",
        "doctor": f"xagent doctor --agent {name}",
        "observe": f"xagent observe --agent {name}",
        "voice": f"xagent voice start --agent {name}",
        "feishu": f"xagent feishu start --agent {name}",
        "weixin": f"xagent weixin start --agent {name}",
    }
    example = templates.get(command)
    if not example:
        return None
    return (
        "xagent: error: unknown command. Put --agent after the subcommand, for example:\n"
        f"  {example}\n"
    )


CHAT_VS_API_NOTE = (
    "Note: terminal chat runs in-process; Web and World agents need the api channel "
    "(xagent api start)."
)
