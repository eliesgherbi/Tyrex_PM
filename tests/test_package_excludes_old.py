"""Packaging isolation: built/installed distribution must not include old/."""

from __future__ import annotations

import pathlib

from importlib import metadata


def test_distribution_files_exclude_old() -> None:
    dist = metadata.distribution("tyrex-pm")
    files = dist.files or []
    for file in files:
        parts = pathlib.PurePosixPath(str(file)).parts
        assert "old" not in parts, f"distribution contains old path: {file}"


def test_top_level_package_is_tyrex_pm_only() -> None:
    dist = metadata.distribution("tyrex-pm")
    # setuptools records top-level.txt in .dist-info
    top = dist.read_text("top_level.txt")
    assert top is not None
    names = {line.strip() for line in top.splitlines() if line.strip()}
    assert "tyrex_pm" in names
    assert "old" not in names
