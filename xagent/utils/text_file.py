"""Helpers for detecting and reading UTF-8 text files."""

from __future__ import annotations

from pathlib import Path

_BINARY_SAMPLE_SIZE = 4096


def is_binary_bytes(chunk: bytes) -> bool:
    """Return True when *chunk* looks like binary rather than UTF-8 text.

    A multi-byte UTF-8 character split by a fixed sample window must not be
    treated as binary: that only means the sample ended mid-character.
    """
    if b"\0" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(chunk):
            return False
        return True
    return False


def is_binary_file(path: Path | str, *, sample_size: int = _BINARY_SAMPLE_SIZE) -> bool:
    """Return True when *path* looks like a binary file."""
    try:
        chunk = Path(path).read_bytes()[:sample_size]
    except OSError:
        return True
    return is_binary_bytes(chunk)
