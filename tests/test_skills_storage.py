import asyncio
import tempfile
import unittest
from pathlib import Path

from xagent.components.skills import SkillsStorageLocal
from xagent.tools import create_read_skill_tool


class SkillsStorageLocalTests(unittest.TestCase):
    def test_create_list_catalog_and_read_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = SkillsStorageLocal(Path(tmpdir) / "skills", seed_builtins=False)

            skill = storage.create_skill(
                name="code-review",
                description="Reviews code changes for correctness. Use when reviewing diffs or PRs.",
                body="# Code Review\n\nSecret body text that should not appear in the catalog.\n",
            )

            self.assertTrue(skill.valid)
            self.assertEqual(skill.name, "code-review")
            self.assertEqual(storage.list_skills()[0].skill_file, "code-review/SKILL.md")

            catalog = storage.catalog_text(max_chars=2000)
            self.assertIn("Available Skills", catalog)
            self.assertIn("description:", catalog)
            self.assertIn("code-review", catalog)
            self.assertIn("Reviews code changes", catalog)
            self.assertNotIn("Secret body text", catalog)

            read_result = storage.read_skill_file("code-review")
            self.assertEqual(read_result["path"], "code-review/SKILL.md")
            self.assertTrue(read_result["text"])
            self.assertIn("# Code Review", read_result["content"])
            self.assertEqual(read_result["skill"]["name"], "code-review")
            self.assertIn("files", read_result)
            self.assertIn("code-review/SKILL.md", [item["path"] for item in read_result["files"]])

    def test_disable_hides_skill_from_catalog_and_tool_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = SkillsStorageLocal(Path(tmpdir) / "skills", seed_builtins=False)
            storage.create_skill(
                name="writing-docs",
                description="Writes project documentation. Use when drafting docs.",
            )

            disabled = storage.set_enabled("writing-docs", False)

            self.assertFalse(disabled.enabled)
            self.assertEqual(storage.catalog_text(max_chars=2000), "")
            self.assertIsNone(storage.get_skill("writing-docs"))
            with self.assertRaises(FileNotFoundError):
                storage.read_skill_file("writing-docs")

    def test_read_skill_file_cannot_escape_selected_skill_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            storage = SkillsStorageLocal(root / "skills", seed_builtins=False)
            storage.create_skill(
                name="alpha-skill",
                description="Handles alpha tasks. Use when alpha is mentioned.",
            )
            storage.create_skill(
                name="beta-skill",
                description="Handles beta tasks. Use when beta is mentioned.",
            )
            outside = root / "secret.txt"
            outside.write_text("secret", encoding="utf-8")
            (storage.root / "alpha-skill" / "secret-link.txt").symlink_to(outside)

            with self.assertRaises(PermissionError):
                storage.read_skill_file("alpha-skill", "../beta-skill/SKILL.md")
            with self.assertRaises(PermissionError):
                storage.read_skill_file("alpha-skill", "secret-link.txt")

    def test_invalid_skill_is_listed_with_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = SkillsStorageLocal(Path(tmpdir) / "skills", seed_builtins=False)
            invalid_dir = storage.root / "bad-skill"
            invalid_dir.mkdir()
            (invalid_dir / "SKILL.md").write_text(
                "---\nname: other-name\ndescription: ''\n---\n\n# Bad\n",
                encoding="utf-8",
            )

            skills = storage.list_skills(include_invalid=True)
            validation = storage.validate_all()

            self.assertEqual(len(skills), 1)
            self.assertFalse(skills[0].valid)
            self.assertFalse(validation["valid"])
            self.assertGreaterEqual(len(validation["skills"][0]["errors"]), 1)

    def test_read_skill_tool_loads_main_and_referenced_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = SkillsStorageLocal(Path(tmpdir) / "skills", seed_builtins=False)
            storage.create_skill(
                name="testing-code",
                description="Generates tests. Use when adding test coverage.",
                body="# Testing Code\n",
            )
            references_dir = storage.root / "testing-code" / "references"
            references_dir.mkdir()
            (references_dir / "patterns.md").write_text("# Patterns\n", encoding="utf-8")

            read_tool = create_read_skill_tool(storage)

            read = asyncio.run(read_tool("testing-code"))
            referenced = asyncio.run(read_tool("testing-code", "references/patterns.md"))

            self.assertIn("# Testing Code", read["content"])
            self.assertIn("testing-code/references", [item["path"] for item in read["files"]])
            self.assertIn("# Patterns", referenced["content"])

    def test_builtin_skill_creator_is_seeded_and_readable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = SkillsStorageLocal(Path(tmpdir) / "skills")

            skill = storage.get_skill("skill-creator")
            self.assertIsNotNone(skill)
            self.assertTrue(skill.valid)

            catalog = storage.catalog_text(max_chars=4000)
            self.assertIn("skill-creator", catalog)
            self.assertIn("create", catalog.lower())

            read_result = storage.read_skill_file("skill-creator")
            self.assertEqual(read_result["path"], "skill-creator/SKILL.md")
            self.assertIn("# Skill Creator", read_result["content"])
            self.assertEqual(read_result["skill"]["name"], "skill-creator")

    def test_builtin_skill_seed_does_not_overwrite_existing_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_root = Path(tmpdir) / "skills"
            storage = SkillsStorageLocal(skills_root)
            skill_file = storage.root / "skill-creator" / "SKILL.md"
            original = skill_file.read_text(encoding="utf-8")
            skill_file.write_text(original + "\nCUSTOM LOCAL EDIT\n", encoding="utf-8")

            SkillsStorageLocal(skills_root)

            self.assertIn("CUSTOM LOCAL EDIT", skill_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
