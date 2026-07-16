"""Binance data venue must stay isolated from trading paths (M2B.2)."""

from __future__ import annotations

from pathlib import Path

FORBIDDEN_IMPORT_SNIPPETS = (
    "tyrex_pm.runtime.paired_binary_run",
    "tyrex_pm.runtime.pipeline",
    "tyrex_pm.execution",
    "tyrex_pm.risk",
    "tyrex_pm.state.wallet_store",
    "WalletStore",
    "RiskEngine",
    "SingleWriterOMS",
    "LiveOMS",
)

MODULE_SOURCES = (
    "src/tyrex_pm/venue/binance_data/__init__.py",
    "src/tyrex_pm/venue/binance_data/ws_client.py",
    "src/tyrex_pm/venue/binance_data/normalize.py",
    "src/tyrex_pm/ingestion/external_btc.py",
)


def _src_text(relative: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / relative).read_text(encoding="utf-8")


def test_binance_data_sources_have_no_forbidden_imports() -> None:
    for relative in MODULE_SOURCES:
        text = _src_text(relative)
        for snippet in FORBIDDEN_IMPORT_SNIPPETS:
            assert snippet not in text, f"forbidden reference {snippet!r} in {relative}"


def test_external_btc_module_import_does_not_load_trading_modules() -> None:
    import importlib
    import sys

    before = set(sys.modules)
    for name in (
        "tyrex_pm.venue.binance_data",
        "tyrex_pm.venue.binance_data.ws_client",
        "tyrex_pm.venue.binance_data.normalize",
        "tyrex_pm.ingestion.external_btc",
    ):
        if name in sys.modules:
            del sys.modules[name]
    importlib.import_module("tyrex_pm.ingestion.external_btc")
    loaded = set(sys.modules) - before
    offenders = [
        m
        for m in loaded
        if any(x in m for x in ("oms", "wallet_store", "paired_binary_run", "pipeline", "risk.engine"))
    ]
    assert not offenders, f"external_btc import pulled trading modules: {offenders}"
