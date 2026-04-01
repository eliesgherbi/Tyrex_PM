"""Architecture guards (v1.04): keep execution imports out of `data/`."""

from __future__ import annotations

import re
from pathlib import Path

FORBIDDEN = re.compile(r"\b(submit_order|order_factory|ExecutionClient)\b")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "src" / "tyrex_pm" / "data"


def test_data_package_avoids_execution_keywords() -> None:
    assert DATA_DIR.is_dir()
    for path in sorted(DATA_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if FORBIDDEN.search(line):
                raise AssertionError(f"{path}:{i}: forbidden execution reference:\n{line}")
