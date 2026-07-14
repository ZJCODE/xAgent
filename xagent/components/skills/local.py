"""Local filesystem implementation for Agent Skills packages."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import threading
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .base import SkillMetadata, SkillsStorageBase, SkillValidationIssue


SKILL_FILENAME = "SKILL.md"
SKILLS_STATE_FILENAME = ".xagent-skills.json"
BUILTIN_SKILLS_DIRNAME = "builtin"
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_XML_TAG_RE = re.compile(r"<[^>]+>")
_TEXT_READ_LIMIT = 1_000_000
_SEARCH_TEXT_LIMIT = 2_000_000


class SkillConflictError(Exception):
    """Raised when a file changed after it was loaded by the caller."""

    def __init__(self, message: str, *, current: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.current = current


class SkillValidationError(Exception):
    """Raised when a proposed SKILL.md document is invalid."""

    def __init__(self, issues: List[SkillValidationIssue]):
        super().__init__("SKILL.md validation failed")
        self.issues = issues


class SkillEntryConflictError(Exception):
    """Raised when a create or move destination already exists."""


class SkillsStorageLocal(SkillsStorageBase):
    """Manage open-standard Agent Skills stored under a runtime skills directory."""

    def __init__(self, root: str | Path, *, seed_builtins: bool = True):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._mutation_lock = threading.RLock()
        if seed_builtins:
            self._seed_builtin_skills()

    def list_skills(self, *, include_disabled: bool = True, include_invalid: bool = True) -> List[SkillMetadata]:
        skills: List[SkillMetadata] = []
        for child in self._skill_directories():
            metadata = self._load_skill_metadata(child)
            if not include_invalid and not metadata.valid:
                continue
            if not include_disabled and not metadata.enabled:
                continue
            skills.append(metadata)
        return skills

    def get_skill(self, name: str, *, include_disabled: bool = False) -> Optional[SkillMetadata]:
        requested = str(name or "").strip()
        if not requested:
            return None
        for skill in self.list_skills(include_disabled=include_disabled, include_invalid=False):
            if skill.name == requested:
                return skill
        return None

    def catalog_text(self, *, max_chars: int) -> str:
        skills = self.list_skills(include_disabled=False, include_invalid=False)
        if not skills:
            return ""

        header = (
            "Available Skills\n"
            "Enabled filesystem skills are listed by name and description. "
            "Descriptions are discovery metadata, not full instructions. "
            "When a skill matches the task, load SKILL.md with `read_skill`; read referenced files only when needed.\n\n"
            "<available_skills>"
        )
        footer = "</available_skills>"
        budget = max(0, int(max_chars) - len(header) - len(footer) - 4)
        lines: List[str] = []
        used = 0
        omitted = 0
        for skill in skills:
            line = f"- name: {skill.name}\n  description: {skill.description}\n  skill_file: skills/{skill.skill_file}"
            if used + len(line) + 1 > budget:
                omitted += 1
                continue
            lines.append(line)
            used += len(line) + 1
        if omitted:
            lines.append(f"[Skills omitted from catalog due to budget: {omitted}]")
        return f"{header}\n" + "\n".join(lines) + f"\n{footer}"

    def read_skill_file(self, skill_name: str, file_path: str = SKILL_FILENAME) -> Dict[str, Any]:
        skill = self.get_skill(skill_name, include_disabled=False)
        if skill is None:
            raise FileNotFoundError("Enabled skill not found")
        requested_path = file_path or SKILL_FILENAME
        if requested_path.startswith("/"):
            raise PermissionError("Skill file path must be relative")
        skill_root = (self.root / skill.path).resolve()
        requested_file = (skill_root / requested_path).resolve()
        if not requested_file.is_relative_to(skill_root):
            raise PermissionError("Access denied")
        relative_path = str(requested_file.relative_to(self.root))
        result = self.read_file(relative_path)
        if not result.get("text"):
            raise ValueError("Skill file is not UTF-8 text")
        result["skill"] = skill.to_dict()
        result["skill_root"] = str((self.root / skill.path).resolve())
        result["files"] = self._scan_tree(skill_root, skill_root)
        return result

    def tree(self) -> List[Dict[str, Any]]:
        return self._scan_tree(self.root, self.root)

    def read_file(self, relative_path: str) -> Dict[str, Any]:
        requested = self._resolve_relative_path(relative_path)
        if self._is_reserved_path(requested):
            raise PermissionError("Reserved skills state file cannot be read")
        if not requested.is_file():
            raise FileNotFoundError("File not found")
        metadata = {**self._file_metadata(requested), "revision": self._file_revision(requested)}
        if metadata["binary"]:
            return {**metadata, "content": "", "text": False}
        content = self._read_text_file(requested, _TEXT_READ_LIMIT)
        return {**metadata, "content": content, "text": True}

    def search(self, query: str, *, limit: int = 50) -> Dict[str, Any]:
        needle = query.strip().lower()
        if not needle:
            return {"query": query, "results": []}
        results: List[Dict[str, Any]] = []
        for file_path in sorted(self.root.rglob("*")):
            if len(results) >= limit:
                break
            resolved = self._safe_child(file_path, boundary=self.root)
            if resolved is None or not resolved.is_file() or self._is_reserved_path(resolved):
                continue

            relative_path = str(resolved.relative_to(self.root))
            match_kind: List[str] = []
            snippet = ""
            if needle in resolved.name.lower() or needle in relative_path.lower():
                match_kind.append("filename")

            if not self._is_binary_file(resolved) and resolved.stat().st_size <= _SEARCH_TEXT_LIMIT:
                try:
                    content = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    content = ""
                content_index = content.lower().find(needle)
                if content_index != -1:
                    match_kind.append("content")
                    start = max(0, content_index - 80)
                    end = min(len(content), content_index + len(query) + 120)
                    snippet = content[start:end].replace("\n", " ").strip()

            if match_kind:
                results.append({
                    **self._file_metadata(resolved),
                    "matched_in": match_kind,
                    "snippet": snippet,
                })
        return {"query": query, "results": results}

    def create_skill(
        self,
        *,
        name: str,
        description: str,
        body: str = "",
        license: Optional[str] = None,
        compatibility: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        allowed_tools: Optional[str] = None,
    ) -> SkillMetadata:
        issues = self._validate_name(name, path=name)
        issues.extend(self._validate_description(description, path=f"{name}/{SKILL_FILENAME}"))
        if issues:
            raise ValueError("; ".join(issue.message for issue in issues))
        skill_dir = self._resolve_relative_path(name)
        if skill_dir.exists():
            raise ValueError("Skill already exists")
        skill_dir.mkdir(parents=True)
        frontmatter: Dict[str, Any] = {
            "name": name,
            "description": description,
        }
        if license:
            frontmatter["license"] = license
        if compatibility:
            frontmatter["compatibility"] = compatibility
        if metadata:
            frontmatter["metadata"] = metadata
        if allowed_tools:
            frontmatter["allowed-tools"] = allowed_tools

        skill_body = body.strip() or self._default_skill_body(name)
        content = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + skill_body + "\n"
        (skill_dir / SKILL_FILENAME).write_text(content, encoding="utf-8")
        return self._load_skill_metadata(skill_dir)

    def write_file(
        self,
        relative_path: str,
        content: str,
        *,
        create_parents: bool = True,
        expected_revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        encoded = content.encode("utf-8")
        if len(encoded) > _TEXT_READ_LIMIT:
            raise ValueError("File is too large to write as text")

        with self._mutation_lock:
            self._assert_no_symlink_path(relative_path)
            requested = self._resolve_relative_path(relative_path)
            self._assert_mutable_skill_child(requested, allow_skill_file=True)
            if requested.exists() and requested.is_symlink():
                raise PermissionError("Symbolic links cannot be written")
            if requested.exists() and requested.is_dir():
                raise ValueError("Path is a directory")
            if create_parents:
                requested.parent.mkdir(parents=True, exist_ok=True)
            elif not requested.parent.is_dir():
                raise FileNotFoundError("Parent directory not found")

            current = self.read_file(str(requested.relative_to(self.root))) if requested.is_file() else None
            if expected_revision is not None and (
                current is None or current.get("revision") != expected_revision
            ):
                raise SkillConflictError("File changed since it was opened", current=current)

            if requested.name == SKILL_FILENAME and requested.parent.parent == self.root:
                issues = self._validate_skill_document(
                    requested.parent.name,
                    content,
                    path=str(requested.relative_to(self.root)),
                )
                if issues:
                    raise SkillValidationError(issues)

            self._atomic_write_text(requested, encoded)
            return self.read_file(str(requested.relative_to(self.root)))

    def create_entry(
        self,
        parent_path: str,
        name: str,
        *,
        kind: str,
        content: str = "",
    ) -> Dict[str, Any]:
        if kind not in {"file", "directory"}:
            raise ValueError("Entry kind must be file or directory")
        self._validate_entry_name(name)
        with self._mutation_lock:
            self._assert_no_symlink_path(parent_path)
            parent = self._resolve_relative_path(parent_path)
            self._assert_mutable_skill_child(parent, allow_skill_root=True)
            if not parent.is_dir():
                raise FileNotFoundError("Parent directory not found")
            if parent.is_symlink():
                raise PermissionError("Symbolic links cannot be modified")
            requested = (parent / name).resolve()
            if not requested.is_relative_to(parent.resolve()):
                raise PermissionError("Access denied")
            self._assert_mutable_skill_child(requested, allow_skill_file=True)
            if requested.exists():
                raise SkillEntryConflictError("Entry already exists")
            if kind == "directory":
                requested.mkdir()
                return self._file_metadata(requested)
            return self.write_file(str(requested.relative_to(self.root)), content, create_parents=False)

    def move_entry(
        self,
        relative_path: str,
        new_parent_path: str,
        new_name: str,
        *,
        expected_revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._validate_entry_name(new_name)
        with self._mutation_lock:
            self._assert_no_symlink_path(relative_path)
            self._assert_no_symlink_path(new_parent_path)
            requested = self._resolve_relative_path(relative_path)
            self._assert_mutable_skill_child(requested)
            if not requested.exists():
                raise FileNotFoundError("Entry not found")
            if requested.is_symlink():
                raise PermissionError("Symbolic links cannot be modified")
            destination_parent = self._resolve_relative_path(new_parent_path)
            self._assert_mutable_skill_child(destination_parent, allow_skill_root=True)
            if not destination_parent.is_dir():
                raise FileNotFoundError("Destination directory not found")
            if destination_parent.is_symlink():
                raise PermissionError("Symbolic links cannot be modified")
            if self._skill_root_for(requested) != self._skill_root_for(destination_parent):
                raise PermissionError("Entries cannot be moved between skills")
            if requested.is_dir() and destination_parent.resolve().is_relative_to(requested.resolve()):
                raise ValueError("A directory cannot be moved inside itself")
            destination = (destination_parent / new_name).resolve()
            self._assert_mutable_skill_child(destination, allow_skill_file=True)
            if destination.exists():
                raise SkillEntryConflictError("Destination already exists")
            if requested.is_file() and expected_revision is not None:
                current = self.read_file(str(requested.relative_to(self.root)))
                if current.get("revision") != expected_revision:
                    raise SkillConflictError("File changed since it was opened", current=current)
            requested.rename(destination)
            return self._file_metadata(destination)

    def delete_entry(
        self,
        relative_path: str,
        *,
        recursive: bool = False,
        expected_revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._mutation_lock:
            self._assert_no_symlink_path(relative_path)
            requested = self._resolve_relative_path(relative_path)
            self._assert_mutable_skill_child(requested)
            if not requested.exists():
                raise FileNotFoundError("Entry not found")
            if requested.is_symlink():
                raise PermissionError("Symbolic links cannot be modified")
            metadata = self._file_metadata(requested)
            if requested.is_file() and expected_revision is not None:
                current = self.read_file(str(requested.relative_to(self.root)))
                if current.get("revision") != expected_revision:
                    raise SkillConflictError("File changed since it was opened", current=current)
            if requested.is_dir():
                if recursive:
                    shutil.rmtree(requested)
                else:
                    requested.rmdir()
            else:
                requested.unlink()
            return metadata

    def delete_path(self, relative_path: str, *, recursive: bool = False) -> Dict[str, Any]:
        requested = self._resolve_relative_path(relative_path)
        if requested == self.root:
            raise ValueError("Cannot delete skills root")
        if self._is_reserved_path(requested):
            raise PermissionError("Reserved skills state file cannot be deleted")
        if not requested.exists():
            raise FileNotFoundError("Path not found")
        metadata = self._file_metadata(requested)
        if requested.is_dir():
            if not recursive:
                requested.rmdir()
            else:
                shutil.rmtree(requested)
        else:
            requested.unlink()
        return metadata

    def set_enabled(self, name: str, enabled: bool) -> SkillMetadata:
        skill = self.get_skill(name, include_disabled=True)
        if skill is None:
            raise FileNotFoundError("Skill not found")
        state = self._load_state()
        skills_state = state.setdefault("skills", {})
        skills_state[skill.name] = {"enabled": bool(enabled)}
        self._write_state(state)
        refreshed = self.get_skill(skill.name, include_disabled=True)
        if refreshed is None:
            raise FileNotFoundError("Skill not found")
        return refreshed

    def validate_skill(self, name: str) -> Dict[str, Any]:
        skill = self.get_skill(name, include_disabled=True)
        if skill is None:
            for candidate in self.list_skills(include_disabled=True, include_invalid=True):
                if candidate.path == name:
                    skill = candidate
                    break
        if skill is None:
            raise FileNotFoundError("Skill not found")
        return {
            "name": skill.name,
            "path": skill.path,
            "valid": skill.valid,
            "errors": [issue.to_dict() for issue in skill.errors],
        }

    def validate_all(self) -> Dict[str, Any]:
        items = [
            {
                "name": skill.name,
                "path": skill.path,
                "valid": skill.valid,
                "errors": [issue.to_dict() for issue in skill.errors],
            }
            for skill in self.list_skills(include_disabled=True, include_invalid=True)
        ]
        return {
            "valid": all(item["valid"] for item in items),
            "skills": items,
        }

    def info(self) -> Dict[str, Any]:
        skills = self.list_skills(include_disabled=True, include_invalid=True)
        enabled = [skill for skill in skills if skill.enabled and skill.valid]
        disabled = [skill for skill in skills if not skill.enabled and skill.valid]
        invalid = [skill for skill in skills if not skill.valid]
        return {
            "root": str(self.root),
            "count": len(skills),
            "enabled_count": len(enabled),
            "disabled_count": len(disabled),
            "invalid_count": len(invalid),
            "skills": [skill.to_dict() for skill in skills],
            "validation": self.validate_all(),
        }

    def _seed_builtin_skills(self) -> None:
        try:
            builtin_root = resources.files("xagent.components.skills").joinpath(BUILTIN_SKILLS_DIRNAME)
        except (ModuleNotFoundError, AttributeError):
            return
        if not builtin_root.is_dir():
            return

        for skill_resource in builtin_root.iterdir():
            if not skill_resource.is_dir() or skill_resource.name.startswith((".", "__")):
                continue
            target = self.root / skill_resource.name
            if target.exists():
                continue
            try:
                self._copy_resource_tree(skill_resource, target)
            except OSError:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)

    def _copy_resource_tree(self, source: Any, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=False)
        for child in source.iterdir():
            destination = target / child.name
            if child.is_dir():
                self._copy_resource_tree(child, destination)
            elif child.is_file():
                destination.write_bytes(child.read_bytes())

    def _skill_directories(self) -> List[Path]:
        try:
            children = sorted(self.root.iterdir(), key=lambda path: path.name.lower())
        except (OSError, PermissionError):
            return []
        return [
            child
            for child in children
            if child.is_dir() and not child.name.startswith(".") and (child / SKILL_FILENAME).is_file()
        ]

    def _load_skill_metadata(self, skill_dir: Path) -> SkillMetadata:
        skill_dir = skill_dir.resolve()
        relative_dir = str(skill_dir.relative_to(self.root))
        skill_file = skill_dir / SKILL_FILENAME
        frontmatter: Dict[str, Any] = {}
        issues: List[SkillValidationIssue] = []
        try:
            content = skill_file.read_text(encoding="utf-8")
            frontmatter, _ = self._parse_frontmatter(content, str(Path(relative_dir) / SKILL_FILENAME))
        except Exception as exc:
            issues.append(SkillValidationIssue(str(Path(relative_dir) / SKILL_FILENAME), "frontmatter", str(exc)))

        raw_name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
        name = str(raw_name or skill_dir.name).strip()
        raw_description = frontmatter.get("description") if isinstance(frontmatter, dict) else None
        description = str(raw_description or "").strip()
        issues.extend(self._validate_name(name, path=str(Path(relative_dir) / SKILL_FILENAME)))
        if name != skill_dir.name:
            issues.append(SkillValidationIssue(
                str(Path(relative_dir) / SKILL_FILENAME),
                "name_mismatch",
                "Skill name must match the parent directory name",
            ))
        issues.extend(self._validate_description(description, path=str(Path(relative_dir) / SKILL_FILENAME)))

        license_value = self._optional_string(frontmatter.get("license") if isinstance(frontmatter, dict) else None)
        compatibility = self._optional_string(frontmatter.get("compatibility") if isinstance(frontmatter, dict) else None)
        if compatibility and len(compatibility) > 500:
            issues.append(SkillValidationIssue(str(Path(relative_dir) / SKILL_FILENAME), "compatibility", "compatibility must be at most 500 characters"))
        metadata = frontmatter.get("metadata") if isinstance(frontmatter, dict) else {}
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            issues.append(SkillValidationIssue(str(Path(relative_dir) / SKILL_FILENAME), "metadata", "metadata must be a mapping"))
            metadata = {}
        allowed_tools = self._optional_string(frontmatter.get("allowed-tools") if isinstance(frontmatter, dict) else None)
        enabled = self._is_enabled(name)
        return SkillMetadata(
            name=name,
            description=description,
            path=relative_dir,
            skill_file=str(Path(relative_dir) / SKILL_FILENAME),
            enabled=enabled,
            valid=not issues,
            modified=skill_file.stat().st_mtime if skill_file.exists() else None,
            license=license_value,
            compatibility=compatibility,
            metadata=metadata,
            allowed_tools=allowed_tools,
            errors=issues,
        )

    def _validate_skill_document(self, skill_name: str, content: str, *, path: str) -> List[SkillValidationIssue]:
        issues: List[SkillValidationIssue] = []
        frontmatter: Dict[str, Any] = {}
        try:
            frontmatter, _ = self._parse_frontmatter(content, path)
        except yaml.MarkedYAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            issues.append(SkillValidationIssue(
                path,
                "frontmatter",
                str(getattr(exc, "problem", None) or exc),
                line=(mark.line + 1) if mark is not None else None,
                column=(mark.column + 1) if mark is not None else None,
            ))
            return issues
        except Exception as exc:
            issues.append(SkillValidationIssue(path, "frontmatter", str(exc), line=1, column=1))
            return issues

        name = str(frontmatter.get("name") or "").strip()
        description = str(frontmatter.get("description") or "").strip()
        issues.extend(self._validate_name(name, path=path))
        if name != skill_name:
            issues.append(SkillValidationIssue(path, "name_mismatch", "Skill name must match the parent directory name"))
        issues.extend(self._validate_description(description, path=path))
        compatibility = self._optional_string(frontmatter.get("compatibility"))
        if compatibility and len(compatibility) > 500:
            issues.append(SkillValidationIssue(path, "compatibility", "compatibility must be at most 500 characters"))
        metadata = frontmatter.get("metadata", {})
        if metadata is not None and not isinstance(metadata, dict):
            issues.append(SkillValidationIssue(path, "metadata", "metadata must be a mapping"))
        return issues

    @staticmethod
    def _parse_frontmatter(content: str, path: str) -> Tuple[Dict[str, Any], str]:
        lines = content.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ValueError("SKILL.md must start with YAML frontmatter")
        closing_index = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                closing_index = index
                break
        if closing_index is None:
            raise ValueError("SKILL.md frontmatter is missing closing ---")
        raw_frontmatter = "".join(lines[1:closing_index])
        body = "".join(lines[closing_index + 1:])
        loaded = yaml.safe_load(raw_frontmatter) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"SKILL.md frontmatter must be a mapping: {path}")
        return loaded, body

    @staticmethod
    def _validate_name(name: str, *, path: str) -> List[SkillValidationIssue]:
        issues: List[SkillValidationIssue] = []
        if not name:
            issues.append(SkillValidationIssue(path, "name", "name is required"))
            return issues
        if len(name) > 64:
            issues.append(SkillValidationIssue(path, "name", "name must be at most 64 characters"))
        if not _NAME_RE.fullmatch(name):
            issues.append(SkillValidationIssue(path, "name", "name must contain lowercase letters, numbers, and single hyphens only"))
        return issues

    @staticmethod
    def _validate_description(description: str, *, path: str) -> List[SkillValidationIssue]:
        issues: List[SkillValidationIssue] = []
        if not description:
            issues.append(SkillValidationIssue(path, "description", "description is required"))
        if len(description) > 1024:
            issues.append(SkillValidationIssue(path, "description", "description must be at most 1024 characters"))
        if _XML_TAG_RE.search(description):
            issues.append(SkillValidationIssue(path, "description", "description cannot contain XML tags"))
        return issues

    def _resolve_relative_path(self, relative_path: str = "") -> Path:
        raw_path = str(relative_path or "").strip().strip("/")
        requested = (self.root / raw_path).resolve()
        if not requested.is_relative_to(self.root):
            raise PermissionError("Access denied")
        return requested

    def _assert_no_symlink_path(self, relative_path: str) -> None:
        raw_path = str(relative_path or "").strip().strip("/")
        current = self.root
        for part in Path(raw_path).parts:
            current = current / part
            if current.is_symlink():
                raise PermissionError("Symbolic links cannot be modified")

    def _skill_root_for(self, path: Path) -> Path:
        relative = path.resolve().relative_to(self.root)
        if not relative.parts:
            raise PermissionError("A skill package must be selected")
        skill_root = self.root / relative.parts[0]
        if not skill_root.is_dir() or not (skill_root / SKILL_FILENAME).is_file():
            raise PermissionError("Path must be inside an existing skill package")
        return skill_root.resolve()

    def _assert_mutable_skill_child(
        self,
        path: Path,
        *,
        allow_skill_root: bool = False,
        allow_skill_file: bool = False,
    ) -> None:
        skill_root = self._skill_root_for(path)
        resolved = path.resolve()
        if resolved == skill_root:
            if allow_skill_root:
                return
            raise PermissionError("Skill roots must be managed as packages")
        if not resolved.is_relative_to(skill_root):
            raise PermissionError("Access denied")
        if resolved == skill_root / SKILL_FILENAME and not allow_skill_file:
            raise PermissionError("SKILL.md cannot be renamed or deleted")
        if self._is_reserved_path(resolved):
            raise PermissionError("Reserved skills state file cannot be modified")

    @staticmethod
    def _validate_entry_name(name: str) -> None:
        if not name or name in {".", ".."} or name != Path(name).name:
            raise ValueError("Entry name must be one path segment")
        if "\x00" in name or any(ord(character) < 32 for character in name):
            raise ValueError("Entry name contains invalid characters")

    def _atomic_write_text(self, path: Path, content: bytes) -> None:
        previous_mode: Optional[int] = None
        if path.exists():
            previous_mode = stat.S_IMODE(path.stat().st_mode)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if previous_mode is not None:
                os.chmod(temp_path, previous_mode)
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _file_revision(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _safe_child(self, path: Path, *, boundary: Optional[Path] = None) -> Optional[Path]:
        try:
            resolved = path.resolve()
        except OSError:
            return None
        safe_root = (boundary or self.root).resolve()
        if not resolved.is_relative_to(safe_root):
            return None
        return resolved

    def _scan_tree(self, directory: Path, root: Path) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        try:
            children = sorted(directory.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))
        except (OSError, PermissionError):
            return entries
        for child in children:
            if child.name == SKILLS_STATE_FILENAME:
                continue
            resolved = self._safe_child(child, boundary=root)
            if resolved is None:
                continue
            try:
                item = self._file_metadata(resolved)
            except OSError:
                continue
            if item["type"] == "dir" and not child.is_symlink():
                item["children"] = self._scan_tree(resolved, root)
            entries.append(item)
        return entries

    def _file_metadata(self, path: Path) -> Dict[str, Any]:
        resolved = path.resolve()
        stat = resolved.stat()
        is_dir = resolved.is_dir()
        mime_type, _ = mimetypes.guess_type(resolved.name)
        return {
            "name": resolved.name,
            "path": str(resolved.relative_to(self.root)),
            "type": "dir" if is_dir else "file",
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "mime_type": mime_type or "application/octet-stream",
            "binary": False if is_dir else self._is_binary_file(resolved),
        }

    @staticmethod
    def _is_binary_file(path: Path) -> bool:
        try:
            chunk = path.read_bytes()[:4096]
        except OSError:
            return True
        if b"\0" in chunk:
            return True
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False

    @staticmethod
    def _read_text_file(path: Path, limit: int) -> str:
        if path.stat().st_size > limit:
            raise ValueError("File is too large to read as text")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("File is not UTF-8 text") from exc

    def _load_state(self) -> Dict[str, Any]:
        state_path = self.root / SKILLS_STATE_FILENAME
        if not state_path.is_file():
            return {"version": 1, "skills": {}}
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "skills": {}}
        if not isinstance(data, dict):
            return {"version": 1, "skills": {}}
        skills_state = data.get("skills")
        if not isinstance(skills_state, dict):
            data["skills"] = {}
        data.setdefault("version", 1)
        return data

    def _write_state(self, state: Dict[str, Any]) -> None:
        state_path = self.root / SKILLS_STATE_FILENAME
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _is_enabled(self, name: str) -> bool:
        state = self._load_state()
        skill_state = state.get("skills", {}).get(name, {})
        if not isinstance(skill_state, dict):
            return True
        return bool(skill_state.get("enabled", True))

    def _is_reserved_path(self, path: Path) -> bool:
        return path.resolve() == (self.root / SKILLS_STATE_FILENAME).resolve()

    @staticmethod
    def _optional_string(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _default_skill_body(name: str) -> str:
        title = " ".join(part.capitalize() for part in name.split("-"))
        return (
            f"# {title}\n\n"
            "## Instructions\n\n"
            "Describe the workflow, conventions, and checks the agent should follow when this skill is relevant.\n"
        )
