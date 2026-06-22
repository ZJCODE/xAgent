import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from xagent.channels.weixin.media import (
    ITEM_IMAGE,
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    download_inbound_media,
    parse_aes_key,
    upload_outbound_media,
)


class _FakeMediaClient:
    cdn_base_url = "https://cdn.example/c2c"

    def __init__(self, encrypted=b""):
        self.encrypted = encrypted
        self.upload_requests = []
        self.uploaded = []

    async def download_cdn(self, *, encrypted_query_param, timeout_ms=120_000):
        self.last_download_param = encrypted_query_param
        return self.encrypted

    async def get_upload_url(self, **kwargs):
        self.upload_requests.append(kwargs)
        return {"upload_param": "upload-param"}

    async def upload_cdn(self, *, upload_url, ciphertext, timeout_ms=120_000):
        self.uploaded.append((upload_url, ciphertext))
        return "download-param"


class WeixinMediaTests(unittest.TestCase):
    def test_aes_round_trip(self):
        key = b"0123456789abcdef"
        plaintext = b"hello weixin media"

        ciphertext = aes128_ecb_encrypt(plaintext, key)
        decrypted = aes128_ecb_decrypt(ciphertext, key)

        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(decrypted, plaintext)

    def test_parse_aes_key_supports_raw_and_hex_string_base64(self):
        raw_key = b"0123456789abcdef"
        raw_b64 = base64.b64encode(raw_key).decode("ascii")
        hex_string_b64 = base64.b64encode(raw_key.hex().encode("ascii")).decode("ascii")

        self.assertEqual(parse_aes_key({"aes_key": raw_b64}), raw_key)
        self.assertEqual(parse_aes_key({"aes_key": hex_string_b64}), raw_key)
        self.assertEqual(parse_aes_key({}, image_aeskey=raw_key.hex()), raw_key)

    def test_download_inbound_image_decrypts_cdn_payload(self):
        key = b"0123456789abcdef"
        encrypted = aes128_ecb_encrypt(b"\xff\xd8\xffjpeg", key)
        client = _FakeMediaClient(encrypted)
        item = {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": {
                    "encrypt_query_param": "download-param",
                    "aes_key": base64.b64encode(key).decode("ascii"),
                }
            },
        }

        result = asyncio.run(download_inbound_media(client, item))

        self.assertIsNotNone(result)
        self.assertEqual(result.data, b"\xff\xd8\xffjpeg")
        self.assertEqual(result.mime_type, "image/jpeg")
        self.assertEqual(client.last_download_param, "download-param")

    def test_upload_outbound_file_builds_file_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "report.txt"
            path.write_text("hello", encoding="utf-8")
            client = _FakeMediaClient()

            outbound = asyncio.run(upload_outbound_media(client, to_user_id="user", path=path))

        self.assertEqual(client.upload_requests[0]["to_user_id"], "user")
        self.assertEqual(client.upload_requests[0]["media_type"], 3)
        self.assertEqual(client.uploaded[0][0], "https://cdn.example/c2c/upload?encrypted_query_param=upload-param&filekey=" + client.upload_requests[0]["filekey"])
        self.assertEqual(outbound.item["type"], 4)
        self.assertEqual(outbound.item["file_item"]["file_name"], "report.txt")
        self.assertEqual(outbound.item["file_item"]["media"]["encrypt_query_param"], "download-param")


if __name__ == "__main__":
    unittest.main()
