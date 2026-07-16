"""Research package import isolation (M2B.3)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _src_text(relative: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / relative).read_text(encoding="utf-8")


def test_src_does_not_import_research() -> None:
    src_root = Path(__file__).resolve().parents[1] / "src" / "tyrex_pm"
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "research" in text and ("import research" in text or "from research" in text):
            offenders.append(str(path.relative_to(src_root.parent.parent)))
    assert not offenders, f"src imports research: {offenders}"


def test_research_normalize_does_not_import_trading_modules() -> None:
    forbidden = (
        "tyrex_pm.runtime.paired_binary_run",
        "tyrex_pm.runtime.pipeline",
        "tyrex_pm.execution",
        "tyrex_pm.risk",
        "tyrex_pm.state.wallet_store",
    )
    for rel in (
        "research/normalize/run.py",
        "research/normalize/io.py",
        "research/normalize/quality.py",
    ):
        text = _src_text(rel)
        for snippet in forbidden:
            assert snippet not in text, f"{snippet} in {rel}"


def test_research_lib_does_not_import_trading_modules() -> None:
    forbidden = (
        "tyrex_pm.runtime.paired_binary_run",
        "tyrex_pm.runtime.pipeline",
        "tyrex_pm.execution",
        "tyrex_pm.risk",
        "tyrex_pm.strategies",
        "tyrex_pm.survival",
    )
    for rel in (
        "research/lib/loaders.py",
        "research/lib/markets.py",
        "research/lib/episodes.py",
        "research/lib/latency.py",
        "research/lib/eda.py",
        "research/lib/exploratory.py",
        "research/m2b4/pipeline.py",
        "research/m2b4/exploratory.py",
    ):
        text = _src_text(rel)
        for snippet in forbidden:
            assert snippet not in text, f"{snippet} in {rel}"


def test_research_import_does_not_load_oms_or_wallet() -> None:
    before = set(sys.modules)
    for name in list(sys.modules):
        if name.startswith("research"):
            del sys.modules[name]
    importlib.import_module("research.normalize.run")
    importlib.import_module("research.lib.loaders")
    loaded = set(sys.modules) - before
    offenders = [m for m in loaded if any(x in m for x in ("oms", "wallet_store", "paired_binary_run", "pipeline"))]
    assert not offenders, offenders


def test_pandas_pyarrow_only_in_research_optional_deps() -> None:
    text = _src_text("pyproject.toml")
    assert "[project.optional-dependencies]" in text
    assert "research = [" in text
    assert '"pandas>=2.2"' in text
    assert '"pyarrow>=15"' in text
    core_deps = text.split("[project.optional-dependencies]")[0]
    assert "pandas" not in core_deps
    assert "pyarrow" not in core_deps
