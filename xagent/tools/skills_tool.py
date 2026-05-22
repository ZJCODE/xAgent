"""Built-in tools for on-demand Agent Skills access."""

from __future__ import annotations

from typing import Optional

from xagent.components.skills import SkillsStorageBase
from xagent.utils.tool_decorator import function_tool


def create_read_skill_tool(skills_storage: SkillsStorageBase):
    """Create a tool that reads SKILL.md or another referenced skill file."""

    @function_tool(
        name="read_skill",
        description=(
            "Load instructions or a referenced UTF-8 file from an enabled Agent Skill package. "
            "Call without file_path to read SKILL.md and discover the selected skill's package files. "
            "Then read referenced files only as needed."
        ),
        param_descriptions={
            "skill_name": "The exact skill name from Available Skills, such as code-review.",
            "file_path": "Relative file path inside the skill directory. Defaults to SKILL.md.",
        },
    )
    async def read_skill(skill_name: str, file_path: Optional[str] = "SKILL.md") -> dict:
        return skills_storage.read_skill_file(skill_name, file_path or "SKILL.md")

    return read_skill
