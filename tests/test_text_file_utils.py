import tempfile
import unittest
from pathlib import Path

from xagent.components.skills import SkillsStorageLocal
from xagent.utils.text_file import is_binary_bytes, is_binary_file


class TextFileDetectionTests(unittest.TestCase):
    def test_valid_utf8_is_not_binary(self):
        self.assertFalse(is_binary_bytes("你好，世界\n".encode("utf-8")))

    def test_null_byte_is_binary(self):
        self.assertTrue(is_binary_bytes(b"hello\0world"))

    def test_invalid_utf8_is_binary(self):
        self.assertTrue(is_binary_bytes(b"abc\xffdef"))

    def test_utf8_truncated_at_sample_boundary_is_not_binary(self):
        # "中" is 3 UTF-8 bytes; place it so a 4096-byte sample ends mid-character.
        char = "中".encode("utf-8")
        self.assertEqual(len(char), 3)
        content = (b"x" * (4096 - 2)) + char + b" trailing text\n"
        sample = content[:4096]
        with self.assertRaises(UnicodeDecodeError):
            sample.decode("utf-8")
        self.assertFalse(is_binary_bytes(sample))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "themes.md"
            path.write_bytes(content)
            self.assertFalse(is_binary_file(path))

    def test_skills_read_file_accepts_utf8_split_by_sample_window(self):
        char = "中".encode("utf-8")
        body = ((b"x" * (4096 - 2)) + char + b" more markdown\n").decode("utf-8")
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = SkillsStorageLocal(Path(tmpdir) / "skills", seed_builtins=False)
            storage.create_skill(
                name="theme-docs",
                description="Documents themes. Use when switching themes.",
            )
            references = storage.root / "theme-docs" / "references"
            references.mkdir()
            path = references / "themes.md"
            path.write_text(body, encoding="utf-8")

            result = storage.read_file("theme-docs/references/themes.md")

            self.assertFalse(result["binary"])
            self.assertTrue(result["text"])
            self.assertIn("more markdown", result["content"])
            self.assertIn("中", result["content"])


if __name__ == "__main__":
    unittest.main()
