"""Shadow copy strategy must not construct Nautilus orders."""

from __future__ import annotations

import re
from pathlib import Path

FORBIDDEN = re.compile(r"\b(submit_order|order_factory|MarketOrder)\b")
FORBIDDEN_CACHE = re.compile(
    r"\b(from\s+nautilus_trader\.cache|import\s+Cache)\b",
)

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "tyrex_pm" / "strategy" / "copy_strategy.py"


def test_copy_strategy_avoids_direct_order_apis() -> None:
    text = TARGET.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        if FORBIDDEN.search(line):
            raise AssertionError(f"{TARGET}:{i}: {line}")


def test_copy_strategy_does_not_query_nautilus_cache() -> None:
    """Runtime state lives in injected risk readers — strategy stays thin."""
    text = TARGET.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        if FORBIDDEN_CACHE.search(line):
            raise AssertionError(f"{TARGET}:{i}: {line}")
