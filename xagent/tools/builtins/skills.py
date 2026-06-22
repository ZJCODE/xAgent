"""Built-in tools for on-demand Agent Skills access."""

from __future__ import annotations

from typing import Optional

from xagent.ports import SkillStore
from xagent.tools.protocol import function_tool


def create_read_skill_tool(skills_storage: SkillStore):
    """Create a tool that reads SKILL.md or another referenced skill file."""

    @function_tool(
        name="read_skill",
        description=(
            "Read SKILL.md or a referenced UTF-8 file from an enabled Agent Skill package."
        ),
        param_descriptions={
            "skill_name": "Exact skill name from Available Skills.",
            "file_path": "Relative path inside the skill directory; defaults to SKILL.md.",
        },
    )
    async def read_skill(skill_name: str, file_path: Optional[str] = "SKILL.md") -> dict:
        return skills_storage.read_skill_file(skill_name, file_path or "SKILL.md")

    return read_skill
