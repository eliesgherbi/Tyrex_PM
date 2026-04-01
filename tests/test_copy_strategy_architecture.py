"""Shadow copy strategy must not construct Nautilus orders."""

from __future__ import annotations

import re
from pathlib import Path

FORBIDDEN = re.compile(r"\b(submit_order|order_factory|MarketOrder)\b")

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "tyrex_pm" / "strategy" / "copy_strategy.py"


def test_copy_strategy_avoids_direct_order_apis() -> None:
    text = TARGET.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        if FORBIDDEN.search(line):
            raise AssertionError(f"{TARGET}:{i}: {line}")
