"""Tests for M2B.4-B exploratory layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from research.lib.eda import build_market_feature_frame
from research.lib.exploratory import SAFETY_TAGS, wrap_exploratory, write_exploratory_trace
from research.lib.latency import build_latency_prior
from research.lib.loaders import load_day
from research.lib.markets import build_clean_markets, ws_seq_gap_audit
from research.m2b4.exploratory import run_notebook_01_exploratory
from research.m2b4.pipeline import run_notebook_02, write_decision_memo
from research.normalize.run import normalize_day

GOLDEN_DAY = Path(__file__).resolve().parent / "fixtures" / "recordings" / "golden_day"


@pytest.fixture
def golden_parquet(tmp_path: Path) -> Path:
    out = tmp_path / "parquet"
    normalize_day(
        day_dir=GOLDEN_DAY,
        date="golden_day",
        out_root=out,
        market_id=None,
        include_external_btc=True,
        strict=False,
        overwrite=True,
    )
    return out


def test_exploratory_wrap_carries_safety_tags() -> None:
    trace = wrap_exploratory({"foo": 1})
    assert trace["exploratory_only"] is True
    assert trace["do_not_use_in_live_yaml"] is True
    assert trace["overfit_warning"] == SAFETY_TAGS["overfit_warning"]


def test_exploratory_not_in_strict_decision_json(tmp_path: Path, golden_parquet: Path) -> None:
    day = load_day(golden_parquet / "date=golden_day", load_books=False)
    clean = build_clean_markets(day)
    gap = ws_seq_gap_audit(day.tables.get("lifecycle_events"))
    exp = run_notebook_01_exploratory(day, clean, gap)
    out = tmp_path / "m2b4"
    write_exploratory_trace("01", exp, out)
    strict = {"decisions": {"markets_included": 1}}
    write_decision_memo("01_coverage_quality", strict, out)
    decision = json.loads((out / "01_coverage_quality_decision.json").read_text())
    exploratory = json.loads((out / "01_exploratory_trace.json").read_text())
    assert "exploratory_only" not in decision
    assert exploratory["exploratory_only"] is True
    assert "do_not_use_in_live_yaml" not in str(decision)


def test_strict_decision_schema_compatible(golden_parquet: Path, tmp_path: Path) -> None:
    day = load_day(golden_parquet / "date=golden_day", load_books=True)
    clean = build_clean_markets(day)
    clean_path = tmp_path / "clean.csv"
    clean.to_csv(clean_path, index=False)
    prior_path = tmp_path / "latency.json"
    prior_path.write_text("{}", encoding="utf-8")
    result = run_notebook_02(day, clean_path, prior_path)
    assert "decisions" in result
    assert result["decisions"].get("insufficient_latency_prior") is True
    assert "exploratory_only" not in result


def test_build_market_feature_frame_smoke(golden_parquet: Path, tmp_path: Path) -> None:
    day = load_day(golden_parquet / "date=golden_day", load_books=True)
    clean = build_clean_markets(day)
    clean_path = tmp_path / "clean.csv"
    clean.to_csv(clean_path, index=False)
    ff = build_market_feature_frame(day, clean_path)
    assert not ff.empty
    assert "market_id" in ff.columns
    assert "spread_median" in ff.columns


def test_partial_latency_prior_submit_to_ack_only(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_a"
    run_dir.mkdir()
    facts = run_dir / "facts.jsonl"
    facts.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-07-05T12:00:00.000Z",
                        "fact_type": "oms_submit",
                        "correlation_id": "c1",
                        "payload": {"client_order_id": "ord-1"},
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-07-05T12:00:00.150Z",
                        "fact_type": "oms_result",
                        "correlation_id": "c1",
                        "payload": {"client_order_id": "ord-1"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    prior = build_latency_prior(run_roots=[tmp_path])
    assert prior["sample_count_latency_chain"] == 1
    assert prior["p50_submit_to_ack_ms"] == pytest.approx(150.0, rel=0.01)
    assert "submit_to_ack" in prior["available_fields"]
    assert "total_trigger_to_ack" in prior["missing_fields"]
    assert prior["partial_submit_to_ack_only"] is True
    assert prior["confidence"] == "low"
