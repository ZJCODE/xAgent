"""Shared helpers for verbatim term search over memory text."""

from __future__ import annotations


def normalize_terms(query: list[str] | None) -> list[str]:
    """Strip and drop empty entries; do not split on whitespace."""
    if not query:
        return []
    return [term.strip() for term in query if term and term.strip()]


def score_text(text: str, terms: list[str]) -> int:
    """Count how many distinct terms appear as case-insensitive substrings."""
    if not text or not terms:
        return 0
    haystack = text.casefold()
    return sum(1 for term in terms if term.casefold() in haystack)
