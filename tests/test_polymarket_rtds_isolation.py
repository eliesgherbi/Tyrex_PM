"""Polymarket RTDS import isolation (M2B.3-A)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _src_text(relative: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / relative).read_text(encoding="utf-8")


def test_polymarket_rtds_does_not_import_trading_modules() -> None:
    forbidden = (
        "tyrex_pm.runtime.paired_binary_run",
        "tyrex_pm.runtime.pipeline",
        "tyrex_pm.execution",
        "tyrex_pm.risk",
        "tyrex_pm.state.wallet_store",
        "tyrex_pm.strategies",
    )
    for rel in (
        "src/tyrex_pm/venue/polymarket_rtds/ws_client.py",
        "src/tyrex_pm/venue/polymarket_rtds/normalize.py",
        "src/tyrex_pm/ingestion/reference_prices.py",
        "src/tyrex_pm/ingestion/price_to_beat_tracker.py",
    ):
        text = _src_text(rel)
        for snippet in forbidden:
            assert snippet not in text, f"{snippet} in {rel}"


def test_reference_prices_import_does_not_load_trading_runtime() -> None:
    before = set(sys.modules)
    for name in list(sys.modules):
        if name.startswith(("research", "tyrex_pm.ingestion.reference_prices")):
            del sys.modules[name]
    importlib.import_module("tyrex_pm.ingestion.reference_prices")
    loaded = set(sys.modules) - before
    offenders = [m for m in loaded if any(x in m for x in ("oms", "wallet_store", "paired_binary_run", "pipeline"))]
    assert not offenders, offenders


def test_reference_prices_disabled_by_default_in_config() -> None:
    text = _src_text("src/tyrex_pm/runtime/config.py")
    assert "class ReferencePricesConfig" in text
    assert "enabled: bool = False" in text
