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
import binascii
import io
import mimetypes
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse


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

_DATA_URI_FULL_RE = re.compile(r"^data:(image/[^;]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)

MAX_IMAGES_PER_MESSAGE = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
SUPPORTED_UPLOAD_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
WORKSPACE_IMAGE_OUTPUT_DIR = "temp/images/inbound"
DEFAULT_IMAGE_TRANSPORT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_IMAGE_TRANSPORT_MAX_EDGE = 2048
DEFAULT_IMAGE_TRANSPORT_JPEG_QUALITY = 85
DEFAULT_IMAGE_TRANSPORT_MIN_JPEG_QUALITY = 55


@dataclass(frozen=True)
class ImageCompressionResult:
    data: bytes
    mime_type: str
    file_name: str
    compressed: bool
    original_size: int
    width: Optional[int] = None
    height: Optional[int] = None


# ---------------------------------------------------------------------------
# Source classification (used at *input* boundaries, e.g. Message.create)
# ---------------------------------------------------------------------------

class ImageSourceType(Enum):
    """How an image source string should be interpreted."""
    URL = "url"            # Remote URL  — usable by OpenAI API directly
    DATA_URI = "data_uri"  # Base64 data URI — usable by OpenAI API directly
    WORKSPACE_BLOB = "workspace_blob"  # /api/workspace/blob?path=...
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
    if is_workspace_blob_source(source):
        return ImageSourceType.WORKSPACE_BLOB
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
        return bool(_DATA_URI_RE.match(inner)) or _is_image_url(inner) or _is_workspace_blob_image_source(inner)

    # Direct image URL
    return _is_image_url(text) or _is_workspace_blob_image_source(text)


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

_WORKSPACE_BLOB_IN_TEXT_RE = re.compile(
    r'(?:https?://[^\s<>"\')\]]+)?/api/workspace/blob\?path=[^\s<>"\')\]]+',
    re.IGNORECASE,
)


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
        if _DATA_URI_RE.match(inner) or _is_image_url(inner) or _is_workspace_blob_image_source(inner):
            _add(inner)

    # 2. Data URIs
    for m in _DATA_URI_IN_TEXT_RE.finditer(text):
        _add(m.group(0))

    # 3. Workspace blob URLs
    for m in _WORKSPACE_BLOB_IN_TEXT_RE.finditer(text):
        source = m.group(0)
        if _is_workspace_blob_image_source(source):
            _add(source)

    # 4. Direct image URLs
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
# Workspace blob URLs and image byte handling
# ---------------------------------------------------------------------------

def workspace_blob_url(relative_path: str) -> str:
    """Return a stable API URL for a workspace-relative binary path."""
    normalized = str(relative_path or "").strip().strip("/")
    return f"/api/workspace/blob?path={quote(normalized, safe='')}"


def workspace_blob_relative_path(source: str) -> str:
    """Extract the workspace-relative path from a workspace blob URL."""
    source = extract_source(str(source or "")).strip().strip("<>")
    if not source:
        return ""
    parsed = urlparse(source)
    if parsed.path != "/api/workspace/blob":
        return ""
    values = parse_qs(parsed.query).get("path") or []
    return unquote(values[0]).strip("/") if values else ""


def is_workspace_blob_source(source: str) -> bool:
    """Return True when the source is a local workspace blob API URL."""
    return bool(workspace_blob_relative_path(source))


def _is_workspace_blob_image_source(source: str) -> bool:
    relative_path = workspace_blob_relative_path(source)
    if not relative_path:
        return False
    mime_type, _ = mimetypes.guess_type(relative_path)
    return bool(str(mime_type or "").lower().startswith("image/"))


def resolve_workspace_blob_path(source: str, workspace_dir: str | Path) -> Optional[Path]:
    """Resolve a workspace blob URL to a file path inside workspace_dir."""
    relative_path = workspace_blob_relative_path(source)
    if not relative_path:
        return None
    root = Path(workspace_dir).expanduser().resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def detect_image_mime(image_bytes: bytes) -> Optional[str]:
    """Detect common raster image MIME types from magic bytes."""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def image_extension_for_mime(mime_type: str) -> str:
    normalized = str(mime_type or "").strip().lower()
    if normalized == "image/jpeg":
        return "jpg"
    if normalized == "image/webp":
        return "webp"
    if normalized == "image/gif":
        return "gif"
    return "png"


def compressed_image_file_name(file_name: str, *, extension: str = "jpg") -> str:
    """Return a conservative file name for a compressed image derivative."""
    source_name = Path(str(file_name or "image")).name or "image"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(source_name).stem).strip(".-_")[:80] or "image"
    clean_extension = re.sub(r"[^A-Za-z0-9]+", "", str(extension or "jpg").lower()) or "jpg"
    return f"{stem}.{clean_extension}"


def compress_image_bytes_for_transport(
    image_bytes: bytes,
    *,
    mime_type: str = "",
    file_name: str = "",
    max_bytes: int = DEFAULT_IMAGE_TRANSPORT_MAX_BYTES,
    max_edge: int = DEFAULT_IMAGE_TRANSPORT_MAX_EDGE,
    jpeg_quality: int = DEFAULT_IMAGE_TRANSPORT_JPEG_QUALITY,
    min_jpeg_quality: int = DEFAULT_IMAGE_TRANSPORT_MIN_JPEG_QUALITY,
) -> ImageCompressionResult:
    """Resize and re-encode large images for chat/channel transport.

    The helper keeps small images untouched. Large or very high-resolution images
    are transposed according to EXIF orientation, stripped of metadata, resized
    proportionally, flattened onto white if they contain transparency, and encoded
    as progressive JPEG until they fit the target budget as closely as possible.
    """
    original_data = bytes(image_bytes or b"")
    original_size = len(original_data)
    detected_mime_type = detect_image_mime(original_data) or ""
    normalized_mime_type = (detected_mime_type or str(mime_type or "").split(";", 1)[0].strip().lower() or "image/jpeg")
    original_file_name = Path(str(file_name or "image")).name or "image"

    def original_result(width: Optional[int] = None, height: Optional[int] = None) -> ImageCompressionResult:
        return ImageCompressionResult(
            data=original_data,
            mime_type=normalized_mime_type,
            file_name=original_file_name,
            compressed=False,
            original_size=original_size,
            width=width,
            height=height,
        )

    if not original_data:
        return original_result()

    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        return original_result()

    try:
        with Image.open(io.BytesIO(original_data)) as opened:
            image = ImageOps.exif_transpose(opened)
            if getattr(image, "is_animated", False):
                try:
                    image.seek(0)
                except Exception:
                    pass
            image.load()
            image = image.copy()
    except Exception:
        return original_result()

    width, height = image.size
    needs_resize = max(width, height) > max_edge if max_edge > 0 else False
    if original_size <= max_bytes and not needs_resize:
        return original_result(width=width, height=height)

    image = _image_to_transport_rgb(image)
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
    if needs_resize:
        image.thumbnail((max_edge, max_edge), resampling)

    best_data = b""
    best_size = 0
    working = image
    quality_values = list(range(
        max(min(jpeg_quality, 95), min_jpeg_quality),
        max(min_jpeg_quality, 1) - 1,
        -5,
    )) or [max(min_jpeg_quality, 1)]

    for _resize_attempt in range(7):
        for quality in quality_values:
            candidate = _encode_transport_jpeg(working, quality=quality)
            candidate_size = len(candidate)
            if not best_data or candidate_size < best_size:
                best_data = candidate
                best_size = candidate_size
            if candidate_size <= max_bytes:
                return ImageCompressionResult(
                    data=candidate,
                    mime_type="image/jpeg",
                    file_name=compressed_image_file_name(original_file_name, extension="jpg"),
                    compressed=candidate != original_data,
                    original_size=original_size,
                    width=working.size[0],
                    height=working.size[1],
                )

        if not best_data:
            break
        current_width, current_height = working.size
        if current_width <= 320 and current_height <= 320:
            break
        ratio = (max_bytes / max(best_size, 1)) ** 0.5 * 0.92
        ratio = max(0.5, min(0.85, ratio))
        next_size = (
            max(1, int(current_width * ratio)),
            max(1, int(current_height * ratio)),
        )
        if next_size == working.size:
            break
        working = working.resize(next_size, resampling)

    if best_data and (best_size < original_size or original_size > max_bytes):
        return ImageCompressionResult(
            data=best_data,
            mime_type="image/jpeg",
            file_name=compressed_image_file_name(original_file_name, extension="jpg"),
            compressed=best_data != original_data,
            original_size=original_size,
            width=working.size[0],
            height=working.size[1],
        )
    return original_result(width=width, height=height)


def _image_to_transport_rgb(image: Any) -> Any:
    if image.mode in {"RGBA", "LA"} or "transparency" in getattr(image, "info", {}):
        from PIL import Image as PILImage  # type: ignore

        rgba = image.convert("RGBA")
        background = PILImage.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _encode_transport_jpeg(image: Any, *, quality: int) -> bytes:
    output = io.BytesIO()
    try:
        image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    except OSError:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=False, progressive=True)
    return output.getvalue()


def bytes_to_data_uri(image_bytes: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def data_uri_to_bytes(
    source: str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    allowed_mime_types: Optional[set[str] | frozenset[str]] = SUPPORTED_UPLOAD_IMAGE_MIME_TYPES,
) -> tuple[bytes, str]:
    """Decode and validate an image data URI."""
    match = _DATA_URI_FULL_RE.match(str(source or "").strip())
    if not match:
        raise ValueError("Image data URI must use data:image/...;base64 format")
    declared_mime_type = match.group(1).lower()
    if allowed_mime_types is not None and declared_mime_type not in allowed_mime_types:
        allowed = ", ".join(sorted(allowed_mime_types))
        raise ValueError(f"Unsupported image MIME type: {declared_mime_type}; allowed: {allowed}")
    try:
        image_bytes = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid image data URI: {exc}") from exc
    if len(image_bytes) > max_bytes:
        raise ValueError(f"Image is too large; maximum size is {max_bytes // (1024 * 1024)}MB")
    detected_mime_type = detect_image_mime(image_bytes)
    if detected_mime_type and detected_mime_type != declared_mime_type:
        if not (declared_mime_type == "image/jpg" and detected_mime_type == "image/jpeg"):
            raise ValueError(
                f"Image MIME type mismatch: declared {declared_mime_type}, detected {detected_mime_type}"
            )
    return image_bytes, detected_mime_type or declared_mime_type


def read_image_file_bytes(
    file_path: str | Path,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    allowed_mime_types: Optional[set[str] | frozenset[str]] = SUPPORTED_UPLOAD_IMAGE_MIME_TYPES,
) -> tuple[bytes, str]:
    """Read and validate a local image file."""
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValueError(f"Image file not found: {path}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"Image is too large; maximum size is {max_bytes // (1024 * 1024)}MB")
    image_bytes = path.read_bytes()
    detected_mime_type = detect_image_mime(image_bytes)
    guessed_mime_type, _ = mimetypes.guess_type(path.name)
    mime_type = detected_mime_type or guessed_mime_type or "image/png"
    if allowed_mime_types is not None and mime_type not in allowed_mime_types:
        allowed = ", ".join(sorted(allowed_mime_types))
        raise ValueError(f"Unsupported image MIME type: {mime_type}; allowed: {allowed}")
    return image_bytes, mime_type


def save_image_bytes_to_workspace(
    image_bytes: bytes,
    mime_type: str,
    workspace_dir: str | Path,
    *,
    output_dir: str = WORKSPACE_IMAGE_OUTPUT_DIR,
    original_name: str = "",
) -> dict:
    """Write image bytes under workspace and return public asset metadata."""
    root = Path(workspace_dir).expanduser().resolve()
    target_dir = (root / output_dir).resolve()
    if not target_dir.is_relative_to(root):
        raise ValueError("Image output directory must stay inside workspace")
    target_dir.mkdir(parents=True, exist_ok=True)
    extension = image_extension_for_mime(mime_type)
    stem = Path(original_name).stem if original_name else "image"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_")[:48] or "image"
    filename = f"{safe_stem}-{os.urandom(4).hex()}.{extension}"
    output_path = (target_dir / filename).resolve()
    if not output_path.is_relative_to(root):
        raise ValueError("Image output path must stay inside workspace")
    output_path.write_bytes(image_bytes)
    relative_path = output_path.relative_to(root).as_posix()
    return {
        "workspace_path": relative_path,
        "blob_url": workspace_blob_url(relative_path),
        "mime_type": mime_type,
        "size_bytes": len(image_bytes),
        "original_name": original_name or None,
    }


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
