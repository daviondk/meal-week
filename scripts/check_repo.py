"""Check homework structure and documentation locally, without network access."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

REQUIRED = (
    "README.md",
    "docs/ml_system_design_doc.md",
    "docs/hw1_checklist.md",
    "pyproject.toml",
    "uv.lock",
    ".pre-commit-config.yaml",
    ".gitignore",
    ".python-version",
    "scripts/check_repo.py",
    "tests/test_check_repo.py",
)
IGNORED = {".git", ".venv", ".ruff_cache", ".pytest_cache", "__pycache__"}
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml", ".lock"}


def validate(root: Path) -> list[str]:
    """Return errors without modifying files or asserting product correctness.

    Markdown checks cover ordinary inline relative file links and triple-backtick
    fences used in this repository, not the entire Markdown specification.
    """
    root = root.resolve()
    errors: list[str] = []
    contents: dict[str, str] = {}
    for name in REQUIRED:
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"Missing or empty required file: {name}")

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED for part in relative.parts) or not path.is_file():
            continue
        if path.suffix not in TEXT_SUFFIXES and relative.as_posix() not in REQUIRED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError) as exc:
            errors.append(f"Cannot read UTF-8 text {relative}: {exc}")
            continue
        contents[relative.as_posix()] = text
        if not text.endswith("\n"):
            errors.append(f"Missing final newline: {relative}")
        for number, line in enumerate(text.splitlines(), 1):
            if line.rstrip(" \t") != line:
                errors.append(f"Trailing whitespace: {relative}:{number}")
        if path.suffix != ".md":
            continue
        if sum(bool(re.match(r"^\s*```", line)) for line in text.splitlines()) % 2:
            errors.append(f"Unclosed fenced code block: {relative}")
        # Ignore fenced examples. Check file paths, not anchors or external URLs.
        prose = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)
        for target in re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", prose):
            target = target.strip().strip("<>")
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            destination = (path.parent / unquote(url.path)).resolve()
            if not destination.is_relative_to(root):
                errors.append(f"Non-portable local link in {relative}: {target}")
            elif not destination.exists():
                errors.append(f"Broken local link in {relative}: {target}")

    if text := contents.get("docs/ml_system_design_doc.md"):
        for heading in ("### 1. Цели и предпосылки", "### 2. Методология `Data Scientist`"):
            if not re.search(rf"^{re.escape(heading)}\s*$", text, re.M):
                errors.append(f"Missing required design heading: {heading}")
        for section in ("1.1", "1.2", "1.3", "1.4", "2.1", "2.2"):
            heading = re.search(rf"^(#{{2,6}})\s+{re.escape(section)}(?:[.\s]|$)", text, re.M)
            if heading is None:
                errors.append(f"Missing required design section: {section}")
            elif heading[1] != "####":
                errors.append(
                    f"Incorrect heading level for design section {section}: expected ####"
                )
        if "```mermaid" not in text:
            errors.append("No Mermaid solution diagram in the design document")
        for placeholder in ("<Продукт>", "\\<Продукт\\>", "<Номер>"):
            if placeholder in text:
                errors.append(f"Unfilled template placeholder: {placeholder}")

    if text := contents.get("pyproject.toml"):
        try:
            config = tomllib.loads(text)
            if not config.get("tool", {}).get("ruff"):
                errors.append("Missing Ruff configuration")
            if not config.get("dependency-groups", {}).get("dev"):
                errors.append("Missing development dependency group")
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"Invalid pyproject.toml: {exc}")
    return errors


def main() -> int:
    errors = validate(Path(__file__).resolve().parents[1])
    if errors:
        print("Repository checks failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Repository structure, local file links, and text hygiene: OK")
    print("Product assumptions, diagram semantics, and external URLs require review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
