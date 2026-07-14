"""Filesystem skill storage interfaces and shared data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SkillValidationIssue:
    """A structured validation issue for a skill package."""

    path: str
    code: str
    message: str
    line: Optional[int] = None
    column: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "path": self.path,
            "code": self.code,
            "message": self.message,
        }
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        return result


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


class SkillsStorageBase(ABC):
    """Abstract interface for local or remote skill storage backends."""

    @abstractmethod
    def list_skills(self, *, include_disabled: bool = True, include_invalid: bool = True) -> List[SkillMetadata]:
        """Return discovered skills."""

    @abstractmethod
    def get_skill(self, name: str, *, include_disabled: bool = False) -> Optional[SkillMetadata]:
        """Return one valid skill by name."""

    @abstractmethod
    def catalog_text(self, *, max_chars: int) -> str:
        """Return a compact model-facing catalog of enabled skills."""

    @abstractmethod
    def read_skill_file(self, skill_name: str, file_path: str = "SKILL.md") -> Dict[str, Any]:
        """Read a UTF-8 text file inside an enabled skill package."""

    @abstractmethod
    def tree(self) -> List[Dict[str, Any]]:
        """Return a safe file tree for the skills root."""

    @abstractmethod
    def read_file(self, relative_path: str) -> Dict[str, Any]:
        """Read a file under the skills root for management UI/API."""

    @abstractmethod
    def search(self, query: str, *, limit: int = 50) -> Dict[str, Any]:
        """Search skill package paths and UTF-8 file contents."""
