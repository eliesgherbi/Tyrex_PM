"""Record mode must not import trading-path modules (M2B.1-A)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


FORBIDDEN_IMPORT_SNIPPETS = (
    "tyrex_pm.runtime.paired_binary_run",
    "tyrex_pm.runtime.pipeline",
    "tyrex_pm.execution.oms",
    "tyrex_pm.risk.engine",
    "tyrex_pm.state.wallet_store",
    "process_intent_work_unit",
    "SingleWriterOMS",
    "LiveOMS",
    "WalletStore",
    "RiskEngine",
)

RECORD_MODULES = (
    "tyrex_pm.runtime.record_run",
    "tyrex_pm.reporting.event_sink",
)


def _src_text(relative: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / relative).read_text(encoding="utf-8")


def test_record_run_source_has_no_forbidden_imports() -> None:
    text = _src_text("src/tyrex_pm/runtime/record_run.py")
    for snippet in FORBIDDEN_IMPORT_SNIPPETS:
        assert snippet not in text, f"forbidden reference {snippet!r} in record_run.py"


def test_event_sink_source_has_no_forbidden_imports() -> None:
    text = _src_text("src/tyrex_pm/reporting/event_sink.py")
    for snippet in FORBIDDEN_IMPORT_SNIPPETS:
        assert snippet not in text, f"forbidden reference {snippet!r} in event_sink.py"


def test_record_module_import_does_not_load_trading_modules() -> None:
    before = set(sys.modules)
    for name in RECORD_MODULES:
        if name in sys.modules:
            del sys.modules[name]
    importlib.import_module("tyrex_pm.runtime.record_run")
    loaded = set(sys.modules) - before
    offenders = [m for m in loaded if any(x in m for x in ("oms", "wallet_store", "paired_binary_run", "pipeline", "risk.engine"))]
    assert not offenders, f"record import pulled trading modules: {offenders}"
