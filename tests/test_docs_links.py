"""Lightweight Markdown relative-link validation for Docs/ and root README."""

from __future__ import annotations

import re
from pathlib import Path

import tyrex_pm

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
DOCS = ROOT / "Docs"
README = ROOT / "README.md"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    files = [README]
    files.extend(sorted(DOCS.rglob("*.md")))
    return files


def _local_target(link: str) -> str | None:
    if link.startswith(("http://", "https://", "mailto:", "#")):
        return None
    # strip anchors and query
    path = link.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return None
    return path


def test_no_obsolete_docs_implementation_capital_i() -> None:
    offenders: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        if "Docs/Implementation/" in text or "](Implementation/" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders


def test_relative_markdown_links_resolve() -> None:
    missing: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            raw = match.group(1).strip().strip("<>")
            target = _local_target(raw)
            if target is None:
                continue
            # Windows paths in links should still use forward slashes in md
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{path.relative_to(ROOT)} -> {raw}")
    assert missing == [], "\n".join(missing)


def test_docs_layers_exist() -> None:
    assert (DOCS / "README.md").is_file()
    assert (DOCS / "latest" / "README.md").is_file()
    assert (DOCS / "specifications" / "00_objective.md").is_file()
    assert (DOCS / "implementation" / "r8_framework_acceptance.md").is_file()
    # Canonical listing should use lowercase implementation/ (Windows FS is case-insensitive).
    names = {p.name for p in DOCS.iterdir()}
    assert "implementation" in {n.lower() for n in names}
    assert "specifications" in names
    assert "latest" in names
