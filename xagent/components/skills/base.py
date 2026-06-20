"""Filesystem skill storage data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SkillValidationIssue:
    """A structured validation issue for a skill package."""

    path: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class SkillMetadata:
    """Parsed metadata for one skill package."""

    name: str
    description: str
    path: str
    skill_file: str
    enabled: bool = True
    valid: bool = True
    modified: Optional[float] = None
    license: Optional[str] = None
    compatibility: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    allowed_tools: Optional[str] = None
    errors: List[SkillValidationIssue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "skill_file": self.skill_file,
            "enabled": self.enabled,
            "valid": self.valid,
            "modified": self.modified,
            "license": self.license,
            "compatibility": self.compatibility,
            "metadata": self.metadata,
            "allowed_tools": self.allowed_tools,
            "errors": [issue.to_dict() for issue in self.errors],
        }
