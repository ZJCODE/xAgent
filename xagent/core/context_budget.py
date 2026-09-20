"""Global context budget, token estimates, and tool-output folding."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Any

from .config import AgentConfig
from .context_manifest import ManifestEntry, manifest_entry_from_message
from .context_text import (
    cap_message_content,
    content_char_length,
    estimate_tokens,
    truncate_middle,
)

logger = logging.getLogger(__name__)

OPTIONAL_SECTION_ORDER = (
    AgentConfig.NOTEBOOK_CONTEXT_NAME,
    AgentConfig.SUBCONSCIOUS_NOTEBOOK_NAME,
    AgentConfig.SKILLS_CATALOG_NAME,
    AgentConfig.WORKSPACE_CONTEXT_NAME,
)

CONTINUITY_DROP_ORDER = (
    AgentConfig.RELATIONSHIP_CONTEXT_NAME,
    AgentConfig.SUBCONSCIOUS_RELATIONSHIPS_NAME,
    AgentConfig.RECENT_MEMORY_NAME,
    AgentConfig.RECENT_EXPERIENCE_NAME,
    # Room entries may be the only remaining copy after experience dedupe.
    AgentConfig.ROOM_CONTEXT_NAME,
)

_SECTION_CHAR_LIMITS = {
    AgentConfig.CURRENT_INPUT_NAME: AgentConfig.MAX_CURRENT_INPUT_CHARS,
    AgentConfig.SKILLS_CATALOG_NAME: AgentConfig.MAX_SKILLS_CATALOG_CHARS,
    AgentConfig.NOTEBOOK_CONTEXT_NAME: AgentConfig.NOTEBOOK_CONTEXT_MAX_CHARS,
    AgentConfig.SUBCONSCIOUS_NOTEBOOK_NAME: AgentConfig.NOTEBOOK_CONTEXT_MAX_CHARS,
    AgentConfig.RECENT_MEMORY_NAME: AgentConfig.MEMORY_RECENT_MAX_CHARS,
    AgentConfig.RECENT_EXPERIENCE_NAME: AgentConfig.MEMORY_RECENT_MAX_CHARS,
}


def apply_section_char_limit(content: str, section_name: str) -> str:
    limit = _SECTION_CHAR_LIMITS.get(section_name)
    if not limit or not content:
        return content
    return truncate_middle(content, limit)


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def _apply_char_limits_to_messages(messages: list[dict]) -> list[dict]:
    updated: list[dict] = []
    for message in messages:
        name = str(message.get("name") or "")
        content = message.get("content")
        if isinstance(content, str) and name in _SECTION_CHAR_LIMITS:
            limited = apply_section_char_limit(content, name)
            updated.append({**message, "content": limited})
        elif isinstance(content, list) and name == AgentConfig.CURRENT_INPUT_NAME:
            text = _message_text(content)
            limited = apply_section_char_limit(text, name)
            new_blocks: list[dict] = [{"type": "text", "text": limited}]
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    new_blocks.append(block)
            updated.append({**message, "content": new_blocks})
        else:
            updated.append(message)
    return updated


def _entries_total_tokens(
    instruction_entries: list[ManifestEntry],
    turn_entries: list[ManifestEntry],
    tool_specs: list,
) -> int:
    total = sum(entry.est_tokens for entry in instruction_entries)
    total += sum(entry.est_tokens for entry in turn_entries)
    total += estimate_tokens(
        json.dumps(list(tool_specs or []), ensure_ascii=False, separators=(",", ":"))
    )
    return total


def _refresh_entry(entry: ManifestEntry, message: dict) -> ManifestEntry:
    refreshed = manifest_entry_from_message(
        message,
        kind=entry.kind,
        trust=entry.trust,
        priority=entry.priority,
        authority=entry.authority,
        provenance=entry.provenance,
    )
    return replace(refreshed, reason=entry.reason, dropped=entry.dropped)


def _drop_section(
    messages: list[dict],
    entries: list[ManifestEntry],
    section_name: str,
    *,
    reason: str,
) -> tuple[list[dict], list[ManifestEntry]]:
    new_messages = [message for message in messages if message.get("name") != section_name]
    new_entries = []
    for entry in entries:
        if entry.name == section_name:
            new_entries.append(
                replace(
                    entry,
                    chars=0,
                    est_tokens=0,
                    reason=reason,
                    dropped=[section_name],
                )
            )
        else:
            new_entries.append(entry)
    return new_messages, new_entries


def _trim_recent_experience_message(message: dict, *, max_tokens: int) -> dict:
    content = str(message.get("content") or "")
    if estimate_tokens(content) <= max_tokens:
        return message
    blocks = re.split(
        r"(?=\[(?:speaker=|ambient context|scheduled task)\])",
        content,
    )
    blocks = [block for block in blocks if block.strip()]
    while blocks and estimate_tokens("".join(blocks)) > max_tokens:
        if len(blocks) <= 1:
            blocks[0] = truncate_middle(blocks[0], max(200, max_tokens * 3))
            break
        blocks.pop(0)
    trimmed = "".join(blocks).strip()
    return {**message, "content": trimmed}


def _trim_room_context_message(message: dict, *, max_tokens: int) -> dict:
    """Keep recent attributed speech, even when its experience copy was deduped.

    Retain at least two history entries to preserve the immediate exchange.
    If necessary, shorten their bodies rather than removing all room evidence.
    Tiny budgets may still overflow the minimum attributed excerpts; the caller
    reports that through continuity_over_budget.
    """
    content = str(message.get("content") or "")
    if estimate_tokens(content) <= max_tokens:
        return message
    lines = content.splitlines()
    entry_pattern = re.compile(r"^(.+ \d{4}-\d{2}-\d{2} \d{2}:\d{2}: )(.*)$")
    entries = [(index, entry_pattern.match(line)) for index, line in enumerate(lines)]
    entries = [(index, match) for index, match in entries if match is not None]
    if not entries:
        # Legacy free-form room text has no safe message boundaries.
        return {**message, "content": truncate_middle(content, max(64, max_tokens))}

    first, last = entries[0][0], entries[-1][0]
    retained = entries[:]

    def render(body_limit: int | None = None) -> str:
        omitted = len(entries) - len(retained)
        notice = [f"[Earlier room messages omitted: {omitted}]"] if omitted else []
        body = [
            match.group(1) + (
                truncate_middle(match.group(2), body_limit)
                if body_limit is not None else match.group(2)
            )
            for _, match in retained
        ]
        return "\n".join([*lines[:first], *notice, *body, *lines[last + 1:]])

    while len(retained) > 2 and estimate_tokens(render()) > max_tokens:
        retained.pop(0)
    trimmed = render()
    if estimate_tokens(trimmed) > max_tokens:
        low, high = 32, max(32, max(len(match.group(2)) for _, match in retained))
        # Use the same multilingual estimator as the global budget.
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_tokens(render(middle)) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        trimmed = render(low)
    return {**message, "content": trimmed}


def apply_context_budget(
    instructions: list[dict],
    turn_messages: list[dict],
    instruction_entries: list[ManifestEntry],
    turn_entries: list[ManifestEntry],
    tool_specs: list,
    *,
    budget_tokens: int,
    reserve_ratio: float = AgentConfig.CONTEXT_BUDGET_RESERVE_RATIO,
) -> tuple[list[dict], list[dict], list[ManifestEntry], list[ManifestEntry], str]:
    """Trim optional then continuity layers until estimated input fits the budget."""
    instructions = _apply_char_limits_to_messages(list(instructions))
    turn_messages = _apply_char_limits_to_messages(list(turn_messages))

    def sync_entries(messages: list[dict], entries: list[ManifestEntry]) -> list[ManifestEntry]:
        synced: list[ManifestEntry] = []
        live_names = {str(message.get("name") or "") for message in messages}
        for entry in entries:
            if entry.name not in live_names:
                synced.append(
                    replace(
                        entry,
                        chars=0,
                        est_tokens=0,
                        dropped=[entry.name],
                        reason="over_budget:dropped",
                    )
                )
                continue
            message = next(message for message in messages if message.get("name") == entry.name)
            synced.append(_refresh_entry(entry, message))
        return synced

    instruction_entries = sync_entries(instructions, list(instruction_entries))
    turn_entries = sync_entries(turn_messages, list(turn_entries))

    budget = max(256, int(budget_tokens))
    reserve = min(max(float(reserve_ratio), 0.0), 0.5)
    effective = int(budget * (1.0 - reserve))

    required_tokens = sum(
        entry.est_tokens
        for entry in (*instruction_entries, *turn_entries)
        if entry.priority == "required"
    )
    required_tokens += estimate_tokens(
        json.dumps(list(tool_specs or []), ensure_ascii=False, separators=(",", ":"))
    )

    manifest_reason = ""
    if required_tokens > effective:
        manifest_reason = "required_over_budget"
        logger.warning(
            "Required context (~%d est tokens) exceeds budget effective cap (%d); not trimming required sections",
            required_tokens,
            effective,
        )
        return instructions, turn_messages, instruction_entries, turn_entries, manifest_reason

    def total() -> int:
        return _entries_total_tokens(instruction_entries, turn_entries, tool_specs)

    for section_name in OPTIONAL_SECTION_ORDER:
        if total() <= effective:
            break
        if any(message.get("name") == section_name for message in instructions):
            instructions, instruction_entries = _drop_section(
                instructions,
                instruction_entries,
                section_name,
                reason="over_budget:optional",
            )
        elif any(message.get("name") == section_name for message in turn_messages):
            turn_messages, turn_entries = _drop_section(
                turn_messages,
                turn_entries,
                section_name,
                reason="over_budget:optional",
            )

    for section_name in CONTINUITY_DROP_ORDER:
        if total() <= effective:
            break
        if section_name == AgentConfig.ROOM_CONTEXT_NAME:
            message = next((m for m in turn_messages if m.get("name") == section_name), None)
            if message is None:
                continue
            index = next(i for i, entry in enumerate(turn_entries) if entry.name == section_name)
            remaining = effective - (total() - turn_entries[index].est_tokens)
            trimmed = _trim_room_context_message(message, max_tokens=max(0, remaining))
            turn_messages = [trimmed if m.get("name") == section_name else m for m in turn_messages]
            turn_entries[index] = replace(
                _refresh_entry(turn_entries[index], trimmed),
                reason="over_budget:room_trimmed",
            )
            continue
        if section_name == AgentConfig.RECENT_EXPERIENCE_NAME:
            message = next(
                (m for m in turn_messages if m.get("name") == section_name),
                None,
            )
            if message is None:
                continue
            remaining = effective - (
                total() - next(e.est_tokens for e in turn_entries if e.name == section_name)
            )
            if remaining < 64:
                turn_messages, turn_entries = _drop_section(
                    turn_messages,
                    turn_entries,
                    section_name,
                    reason="over_budget:continuity",
                )
                continue
            trimmed = _trim_recent_experience_message(message, max_tokens=max(64, remaining))
            turn_messages = [
                trimmed if m.get("name") == section_name else m for m in turn_messages
            ]
            for index, entry in enumerate(turn_entries):
                if entry.name == section_name:
                    turn_entries[index] = _refresh_entry(entry, trimmed)
            continue

        if any(message.get("name") == section_name for message in turn_messages):
            turn_messages, turn_entries = _drop_section(
                turn_messages,
                turn_entries,
                section_name,
                reason="over_budget:continuity",
            )
        elif any(message.get("name") == section_name for message in instructions):
            instructions, instruction_entries = _drop_section(
                instructions,
                instruction_entries,
                section_name,
                reason="over_budget:continuity",
            )

    if total() > effective:
        manifest_reason = "continuity_over_budget"

    instruction_entries = sync_entries(instructions, instruction_entries)
    turn_entries = sync_entries(turn_messages, turn_entries)

    return instructions, turn_messages, instruction_entries, turn_entries, manifest_reason


def trim_experience_entries(
    entries: list[tuple[str, Any, str]],
    *,
    max_est_tokens: int,
    min_conversation_entries: int = AgentConfig.MIN_HOT_RAW_MESSAGES,
    covers_through_cursor: int = 0,
    storage_cursor_fn: Any = None,
) -> tuple[list[tuple[str, Any, str]], int]:
    """Drop oldest experience rows until under token cap; keep minimum hot raw size."""
    if not entries or max_est_tokens <= 0:
        return entries, 0

    def entry_tokens(item: tuple[str, Any, str]) -> int:
        return estimate_tokens(str(item[2] or ""))

    working = list(entries)
    unsummarized_dropped = 0
    covers = max(0, int(covers_through_cursor or 0))
    cursor_fn = storage_cursor_fn or (lambda _msg: None)

    def conversation_count(items: list) -> int:
        return sum(1 for kind, _, _ in items if kind == "message")

    while working and sum(entry_tokens(item) for item in working) > max_est_tokens:
        if conversation_count(working) <= min_conversation_entries:
            break
        dropped = working.pop(0)
        msg = dropped[1]
        cursor = cursor_fn(msg)
        if cursor is not None and cursor > covers:
            unsummarized_dropped += 1
    return working, unsummarized_dropped


def cap_message_content(text: str, max_chars: int) -> str:
    return truncate_middle(str(text or ""), max(1, int(max_chars)))


def fold_tool_outputs(input_messages: list, *, max_total_chars: int) -> int:
    """Replace earliest tool result bodies with placeholders until under the char cap."""
    limit = max(1, int(max_total_chars))
    folded = 0

    def body_chars(message: dict) -> int:
        content = message.get("content")
        if content is None:
            output = message.get("output")
            return len(str(output or ""))
        return content_char_length(content)

    def is_tool_result(message: dict) -> bool:
        if message.get("role") == "tool":
            return True
        if message.get("type") == "function_call_output":
            return True
        return False

    def is_elided(message: dict) -> bool:
        content = message.get("content")
        if content is None:
            output = str(message.get("output") or "")
            return output.startswith("[earlier tool output elided:")
        return str(content or "").startswith("[earlier tool output elided:")

    def set_placeholder(message: dict, original_len: int) -> None:
        placeholder = (
            f"[earlier tool output elided: {original_len} chars; re-run the tool if needed]"
        )
        if message.get("role") == "tool":
            message["content"] = placeholder
        elif message.get("type") == "function_call_output":
            message["output"] = placeholder
        else:
            message["content"] = placeholder

    while sum(body_chars(message) for message in input_messages if is_tool_result(message)) > limit:
        replaced = False
        for message in input_messages:
            if not is_tool_result(message) or is_elided(message):
                continue
            original = body_chars(message)
            if original <= 64:
                continue
            set_placeholder(message, original)
            folded += 1
            replaced = True
        if not replaced:
            break
    return folded
