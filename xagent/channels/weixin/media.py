"""Media helpers for Weixin iLink CDN upload/download."""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.backends import default_backend  # type: ignore[import-not-found]
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore[import-not-found]

from ...infrastructure.media.images import (
    DEFAULT_IMAGE_TRANSPORT_MAX_BYTES,
    DEFAULT_IMAGE_TRANSPORT_MAX_EDGE,
    compress_image_bytes_for_transport,
    detect_image_mime,
    image_extension_for_mime,
)
from .client import WeixinClient, cdn_upload_url


MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

_AUDIO_EXTENSIONS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".silk"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


@dataclass(frozen=True)
class InboundMedia:
    data: bytes
    file_name: str
    mime_type: str
    item_type: int
    resource_id: str
    resource_type: str


@dataclass(frozen=True)
class OutboundMedia:
    item: dict[str, Any]
    client_id_suffix: str


def aes_padded_size(size: int) -> int:
    return ((int(size) + 1 + 15) // 16) * 16


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def parse_aes_key(media: dict[str, Any], *, image_aeskey: Optional[str] = None) -> Optional[bytes]:
    if image_aeskey:
        text = str(image_aeskey).strip()
        if len(text) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)

    raw = str(media.get("aes_key") or "").strip()
    if not raw:
        return None
    decoded = base64.b64decode(raw)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


async def download_inbound_media(client: WeixinClient, item: dict[str, Any]) -> Optional[InboundMedia]:
    item_type = int(item.get("type") or 0)
    if item_type == ITEM_IMAGE:
        image_item = item.get("image_item") or {}
        media = image_item.get("media") or {}
        key = parse_aes_key(media, image_aeskey=image_item.get("aeskey"))
        file_name = "weixin-image"
        mime_type = "image/jpeg"
        resource_type = "image"
    elif item_type == ITEM_VIDEO:
        video_item = item.get("video_item") or {}
        media = video_item.get("media") or {}
        key = parse_aes_key(media)
        file_name = "weixin-video.mp4"
        mime_type = "video/mp4"
        resource_type = "video"
    elif item_type == ITEM_FILE:
        file_item = item.get("file_item") or {}
        media = file_item.get("media") or {}
        key = parse_aes_key(media)
        file_name = str(file_item.get("file_name") or "weixin-file.bin")
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        resource_type = "file"
    elif item_type == ITEM_VOICE:
        voice_item = item.get("voice_item") or {}
        if voice_item.get("text"):
            return None
        media = voice_item.get("media") or {}
        key = parse_aes_key(media)
        file_name = "weixin-voice.silk"
        mime_type = "audio/silk"
        resource_type = "voice"
    else:
        return None

    encrypted_query_param = str(media.get("encrypt_query_param") or "").strip()
    if not encrypted_query_param or key is None:
        return None
    encrypted = await client.download_cdn(encrypted_query_param=encrypted_query_param)
    data = aes128_ecb_decrypt(encrypted, key)
    if item_type == ITEM_IMAGE:
        detected = detect_image_mime(data)
        if detected:
            mime_type = detected
            file_name = f"weixin-image.{image_extension_for_mime(detected)}"
    return InboundMedia(
        data=data,
        file_name=file_name,
        mime_type=mime_type,
        item_type=item_type,
        resource_id=encrypted_query_param,
        resource_type=resource_type,
    )


async def upload_outbound_media(
    client: WeixinClient,
    *,
    to_user_id: str,
    path: Path,
    force_file_attachment: bool = False,
) -> OutboundMedia:
    source_path = Path(path).expanduser().resolve()
    plaintext = source_path.read_bytes()
    media_type, plaintext, file_name, mime_type = _prepare_outbound_payload(
        source_path,
        plaintext,
        force_file_attachment=force_file_attachment,
    )
    filekey = secrets.token_hex(16)
    aes_key = secrets.token_bytes(16)
    ciphertext = aes128_ecb_encrypt(plaintext, aes_key)
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()
    upload_response = await client.get_upload_url(
        to_user_id=to_user_id,
        media_type=media_type,
        filekey=filekey,
        rawsize=len(plaintext),
        rawfilemd5=rawfilemd5,
        filesize=len(ciphertext),
        aeskey_hex=aes_key.hex(),
    )
    upload_full_url = str(upload_response.get("upload_full_url") or "").strip()
    upload_param = str(upload_response.get("upload_param") or "").strip()
    if upload_full_url:
        upload_url = upload_full_url
    elif upload_param:
        upload_url = cdn_upload_url(client.cdn_base_url, upload_param, filekey)
    else:
        raise RuntimeError(f"getuploadurl returned no upload target: {upload_response}")
    encrypted_query_param = await client.upload_cdn(upload_url=upload_url, ciphertext=ciphertext)
    aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
    item = _media_item(
        media_type=media_type,
        encrypted_query_param=encrypted_query_param,
        aes_key_for_api=aes_key_for_api,
        ciphertext_size=len(ciphertext),
        plaintext_size=len(plaintext),
        file_name=file_name,
        rawfilemd5=rawfilemd5,
        mime_type=mime_type,
    )
    return OutboundMedia(item=item, client_id_suffix=_suffix_for_media_type(media_type))


def _prepare_outbound_payload(
    source_path: Path,
    plaintext: bytes,
    *,
    force_file_attachment: bool,
) -> tuple[int, bytes, str, str]:
    mime_type = detect_image_mime(plaintext) or mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    suffix = source_path.suffix.lower()
    if not force_file_attachment and (mime_type.startswith("image/") or suffix in _IMAGE_EXTENSIONS):
        compressed = compress_image_bytes_for_transport(
            plaintext,
            mime_type=mime_type,
            file_name=source_path.name,
            max_bytes=DEFAULT_IMAGE_TRANSPORT_MAX_BYTES,
            max_edge=DEFAULT_IMAGE_TRANSPORT_MAX_EDGE,
        )
        return MEDIA_IMAGE, compressed.data, compressed.file_name, compressed.mime_type
    if not force_file_attachment and (mime_type.startswith("video/") or suffix in _VIDEO_EXTENSIONS):
        return MEDIA_VIDEO, plaintext, source_path.name, mime_type
    return MEDIA_FILE, plaintext, source_path.name, mime_type


def _media_item(
    *,
    media_type: int,
    encrypted_query_param: str,
    aes_key_for_api: str,
    ciphertext_size: int,
    plaintext_size: int,
    file_name: str,
    rawfilemd5: str,
    mime_type: str,
) -> dict[str, Any]:
    media = {
        "encrypt_query_param": encrypted_query_param,
        "aes_key": aes_key_for_api,
        "encrypt_type": 1,
    }
    if media_type == MEDIA_IMAGE:
        return {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": media,
                "mid_size": ciphertext_size,
            },
        }
    if media_type == MEDIA_VIDEO:
        return {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": media,
                "video_size": ciphertext_size,
                "play_length": 0,
                "video_md5": rawfilemd5,
            },
        }
    return {
        "type": ITEM_FILE,
        "file_item": {
            "media": media,
            "file_name": file_name,
            "md5": rawfilemd5,
            "len": str(plaintext_size),
        },
    }


def _suffix_for_media_type(media_type: int) -> str:
    if media_type == MEDIA_IMAGE:
        return "image"
    if media_type == MEDIA_VIDEO:
        return "video"
    if media_type == MEDIA_VOICE:
        return "voice"
    return "file"


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)
