"""Tests of the homework checker, not of the future meal-planning product."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_repo import REQUIRED, validate


class RepositoryCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in REQUIRED:
            self.write(name, "present\n")
        self.write(
            "pyproject.toml",
            '[tool.ruff]\nline-length = 100\n[dependency-groups]\ndev = ["ruff==0.15.1"]\n',
        )
        self.design = "> ## Термины и пояснения\n> - Term: explanation.\n"
        for title, sections in (
            ("1. Цели и предпосылки", ("1.1", "1.2", "1.3", "1.4")),
            ("2. Методология `Data Scientist`", ("2.1", "2.2")),
        ):
            self.design += f"\n### {title}\n"
            self.design += "\n".join(f"#### {section} Heading\nText.\n" for section in sections)
        self.design += "\n```mermaid\nflowchart LR\n    A --> B\n```\n"
        self.write("docs/ml_system_design_doc.md", self.design)

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_complete_fixture_passes(self) -> None:
        self.assertEqual(validate(self.root), [])

    def test_missing_required_file_fails(self) -> None:
        (self.root / "uv.lock").unlink()
        self.assertIn("Missing or empty required file: uv.lock", validate(self.root))

    def test_missing_design_section_fails(self) -> None:
        self.write("docs/ml_system_design_doc.md", self.design.replace("#### 1.2", "#### Other"))
        self.assertIn("Missing required design section: 1.2", validate(self.root))

    def test_document_without_terms_block_passes(self) -> None:
        self.write(
            "docs/ml_system_design_doc.md",
            self.design.replace("> ## Термины и пояснения\n> - Term: explanation.\n", ""),
        )
        self.assertEqual(validate(self.root), [])

    def test_incorrect_design_heading_levels_fail(self) -> None:
        for before, after, error in (
            (
                "### 1. Цели и предпосылки",
                "## 1. Цели и предпосылки",
                "Missing required design heading: ### 1. Цели и предпосылки",
            ),
            (
                "### 2. Методология `Data Scientist`",
                "## 2. Методология `Data Scientist`",
                "Missing required design heading: ### 2. Методология `Data Scientist`",
            ),
            (
                "#### 1.2",
                "### 1.2",
                "Incorrect heading level for design section 1.2: expected ####",
            ),
        ):
            with self.subTest(heading=before):
                self.write("docs/ml_system_design_doc.md", self.design.replace(before, after))
                self.assertIn(error, validate(self.root))

    def test_text_hygiene_fails(self) -> None:
        self.write("README.md", "Bad text  \nMissing final newline")
        errors = validate(self.root)
        self.assertIn("Trailing whitespace: README.md:1", errors)
        self.assertIn("Missing final newline: README.md", errors)

    def test_broken_relative_link_fails(self) -> None:
        self.write("README.md", "[Missing](docs/missing.md)\n")
        self.assertIn("Broken local link in README.md: docs/missing.md", validate(self.root))

    def test_link_outside_repository_fails(self) -> None:
        self.write("README.md", "[Outside](../other.md)\n")
        self.assertIn("Non-portable local link in README.md: ../other.md", validate(self.root))

    def test_links_inside_fenced_examples_are_ignored(self) -> None:
        self.write("README.md", "```markdown\n[Example](not-a-file.md)\n```\n")
        self.assertEqual(validate(self.root), [])

    def test_external_and_anchor_links_need_no_network(self) -> None:
        self.write("README.md", "[External](https://example.invalid) [Section](#heading)\n")
        self.assertEqual(validate(self.root), [])

    def test_unclosed_code_fence_fails(self) -> None:
        self.write("README.md", "```text\nNot closed\n")
        self.assertIn("Unclosed fenced code block: README.md", validate(self.root))

    def test_template_placeholder_fails(self) -> None:
        self.write("docs/ml_system_design_doc.md", self.design + "<Продукт>\n")
        self.assertIn("Unfilled template placeholder: <Продукт>", validate(self.root))

    def test_invalid_utf8_is_an_error_not_a_crash(self) -> None:
        (self.root / "docs/ml_system_design_doc.md").write_bytes(b"\xff")
        errors = validate(self.root)
        self.assertTrue(any(error.startswith("Cannot read UTF-8 text") for error in errors))

    def test_invalid_toml_fails(self) -> None:
        self.write("pyproject.toml", "[invalid\n")
        errors = validate(self.root)
        self.assertTrue(any(error.startswith("Invalid pyproject.toml:") for error in errors))

    def test_cache_files_are_ignored(self) -> None:
        self.write(".venv/invalid.py", "trailing whitespace  ")
        self.assertEqual(validate(self.root), [])


if __name__ == "__main__":
    unittest.main()
