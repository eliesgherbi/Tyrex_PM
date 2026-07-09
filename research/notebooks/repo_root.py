"""Ensure Tyrex_PM repo root is on sys.path for notebook imports."""

from __future__ import annotations

import sys
from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").is_file() and (candidate / "research" / "lib").is_dir():
            return candidate
    raise RuntimeError(
        "Cannot locate Tyrex_PM repo root (expected pyproject.toml + research/lib/). "
        "Open the notebook from the repo root or research/notebooks/."
    )


def ensure_importable(start: Path | None = None) -> Path:
    root = find_repo_root(start)
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    return root


if __name__ == "__main__":
    print(ensure_importable())
