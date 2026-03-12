"""
Centralized image detection, classification and extraction utilities.

All image-related heuristics live here so that agent.py, message.py and
web.py share a single source of truth instead of scattering regex / prefix
checks across the codebase.

Three categories of image source at the boundary:
  1. URL        — ``https://example.com/photo.png``
  2. Data URI   — ``data:image/png;base64,iVBOR...``
  3. File path  — ``/tmp/photo.png``  (converted to Data URI before use)

Additionally, tool outputs may wrap any of the above in Markdown image
syntax:  ``![alt text](source)``
"""

import base64
import mimetypes
import os
import re
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Regex patterns (compiled once)
# ---------------------------------------------------------------------------

_DATA_URI_RE = re.compile(r"^data:image/[^;]+;base64,", re.IGNORECASE)

_MARKDOWN_IMG_RE = re.compile(r"^!\[([^\]]*)\]\((.+)\)$", re.DOTALL)

# URL ending with a known image extension (allows query / fragment)
_IMAGE_URL_EXT_RE = re.compile(
    r"^https?://.+\.(?:png|jpe?g|gif|webp|bmp|tiff|svg)(?:[?#].*)?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Source classification (used at *input* boundaries, e.g. Message.create)
# ---------------------------------------------------------------------------

class ImageSourceType(Enum):
    """How an image source string should be interpreted."""
    URL = "url"            # Remote URL  — usable by OpenAI API directly
    DATA_URI = "data_uri"  # Base64 data URI — usable by OpenAI API directly
    FILE = "file"          # Local file path — must be converted to data URI


def classify_source(source: str) -> ImageSourceType:
    """Classify a raw image source string into URL, data URI or file path.

    >>> classify_source("https://img.example.com/a.png")
    <ImageSourceType.URL: 'url'>
    >>> classify_source("data:image/png;base64,abc")
    <ImageSourceType.DATA_URI: 'data_uri'>
    >>> classify_source("/tmp/photo.png")
    <ImageSourceType.FILE: 'file'>
    """
    if _DATA_URI_RE.match(source):
        return ImageSourceType.DATA_URI
    if source.startswith(("http://", "https://")):
        return ImageSourceType.URL
    return ImageSourceType.FILE


# ---------------------------------------------------------------------------
# Output detection (used after tool execution in Agent._act)
# ---------------------------------------------------------------------------

def is_image_output(text: str) -> bool:
    """Return *True* if ``text`` represents an image that should be delivered
    to the user rather than fed back to the model as plain text.

    Recognised forms:
    - ``data:image/...;base64,...``
    - ``![...](data:image/...)``  or  ``![...](https://...image.png)``
    - ``https://example.com/result.png``
    """
    if not isinstance(text, str) or not text.strip():
        return False
    text = text.strip()

    # Direct data URI
    if _DATA_URI_RE.match(text):
        return True

    # Markdown image wrapping — accept if inner source is data URI or image URL
    md = _MARKDOWN_IMG_RE.match(text)
    if md:
        inner = md.group(2)
        return bool(_DATA_URI_RE.match(inner)) or _is_image_url(inner)

    # Direct image URL
    return _is_image_url(text)


def _is_image_url(url: str) -> bool:
    """Heuristic: does this URL point to an image?"""
    if not url.startswith(("http://", "https://")):
        return False
    return bool(_IMAGE_URL_EXT_RE.match(url))


# ---------------------------------------------------------------------------
# Extract image URLs/URIs from free-form text
# ---------------------------------------------------------------------------

# Image URL embedded in arbitrary text (terminates at whitespace / quotes / angle brackets)
_IMAGE_URL_IN_TEXT_RE = re.compile(
    r'https?://[^\s<>"\')\]]+\.(?:png|jpe?g|gif|webp|bmp|tiff|svg)(?:[?#][^\s<>"\')\]]*)?',
    re.IGNORECASE,
)

# Data URI embedded in arbitrary text
_DATA_URI_IN_TEXT_RE = re.compile(
    r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+',
    re.IGNORECASE,
)

# Markdown image anywhere in text  —  ``![alt](src)``
_MARKDOWN_IMG_IN_TEXT_RE = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')


def extract_image_urls_from_text(text: str) -> list:
    """Extract all image sources (URLs / data URIs) from free-form text.

    Detects:
    - Direct image URLs  — ``https://example.com/photo.png``
    - Markdown images    — ``![alt](https://example.com/photo.png)``
    - Data URIs          — ``data:image/png;base64,iVBOR…``

    Returns:
        De-duplicated list of image source strings in the order they were
        first encountered.  Empty list if nothing was found.

    >>> extract_image_urls_from_text("Look at https://img.co/a.png please")
    ['https://img.co/a.png']
    >>> extract_image_urls_from_text("No images here")
    []
    """
    if not isinstance(text, str) or not text.strip():
        return []

    seen: set = set()
    result: list = []

    def _add(url: str) -> None:
        if url not in seen:
            seen.add(url)
            result.append(url)

    # 1. Markdown images  — highest priority (extract inner src)
    for m in _MARKDOWN_IMG_IN_TEXT_RE.finditer(text):
        inner = m.group(1).strip()
        if _DATA_URI_RE.match(inner) or _is_image_url(inner):
            _add(inner)

    # 2. Data URIs
    for m in _DATA_URI_IN_TEXT_RE.finditer(text):
        _add(m.group(0))

    # 3. Direct image URLs
    for m in _IMAGE_URL_IN_TEXT_RE.finditer(text):
        _add(m.group(0))

    return result


# ---------------------------------------------------------------------------
# Source extraction (unwrap Markdown if present)
# ---------------------------------------------------------------------------

def extract_source(text: str) -> str:
    """Extract the raw image URL or data URI from *text*.

    If the text is wrapped in ``![alt](src)`` Markdown, return ``src``.
    Otherwise return the text as-is.

    >>> extract_source("![cat](https://img.example.com/cat.png)")
    'https://img.example.com/cat.png'
    >>> extract_source("data:image/png;base64,abc")
    'data:image/png;base64,abc'
    """
    md = _MARKDOWN_IMG_RE.match(text.strip())
    if md:
        return md.group(2)
    return text.strip()


# ---------------------------------------------------------------------------
# Format inference
# ---------------------------------------------------------------------------

_FORMAT_MAP = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/png": "png",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/svg+xml": "svg",
}

_EXT_FORMAT_MAP = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
    ".png": "png",
    ".bmp": "bmp",
    ".tiff": "tiff",
    ".svg": "svg",
}


def infer_format(source: str) -> str:
    """Best-effort image format from a source string. Falls back to ``'png'``.

    Works with data URIs (``data:image/jpeg;base64,...``), URLs and file paths.
    """
    lower = source.lower()

    # Try MIME type inside data URI
    for mime, fmt in _FORMAT_MAP.items():
        if mime in lower:
            return fmt

    # Try file extension
    for ext, fmt in _EXT_FORMAT_MAP.items():
        if lower.endswith(ext) or f"{ext}?" in lower or f"{ext}#" in lower:
            return fmt

    return "png"


# ---------------------------------------------------------------------------
# File-path → data URI conversion
# ---------------------------------------------------------------------------

def file_to_data_uri(file_path: str) -> Optional[str]:
    """Convert a local image file to a base64 data URI.

    Args:
        file_path: Path to the image file.

    Returns:
        A ``data:<mime>;base64,…`` string ready to be used with OpenAI APIs,
        or ``None`` if the file cannot be read.
    """
    if not os.path.isfile(file_path):
        return None

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/png"  # safe default for unknown image types

    with open(file_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime_type};base64,{encoded}"
