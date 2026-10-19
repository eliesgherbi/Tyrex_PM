"""P7 qualification: stage order, one-shot evaluation, analysis helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from tyrex_pm.research.strategy_lab import qualification as ql


@pytest.fixture()
def qual(tmp_path: Path, monkeypatch) -> ql.Qualification:
    q = ql.Qualification()
    q.path = tmp_path / "p7"
    monkeypatch.setattr(ql, "holdout_untouched",
                        lambda exclude=None: {"untouched": True, "summaries_using_holdout": {},
                                              "timeline_cache_entries": 0})
    return q


def test_freeze_requires_completed_development(qual) -> None:
    with pytest.raises(ql.QualificationOrderError):
        qual.freeze(["corrected_volatility"], "too early")


def test_evaluation_requires_a_freeze_and_runs_frozen_policies_once(qual, monkeypatch) -> None:
    with pytest.raises(ql.QualificationOrderError):
        qual.run_evaluation(workers=1)
    record = qual.record()
    for scenario in qual.scenarios:
        record["stages"][f"development/{scenario}"] = {"status": "completed"}
    qual._save(record)
    for scenario in qual.scenarios:
        folder = qual.path / "development" / scenario
        folder.mkdir(parents=True)
        pd.DataFrame([{"tag": "s--default--legacy_volatility", "scenario_name": scenario,
                       "digest": "d"}]).to_parquet(folder / "summaries.parquet")
    frozen = qual.freeze(["corrected_volatility"], "time policy is a correctness fix")
    assert list(frozen["policies"]) == ["corrected_volatility"]
    with pytest.raises(ql.QualificationOrderError):
        qual.freeze(["legacy_volatility"], "second thoughts")
    launched: list = []
    monkeypatch.setattr(ql, "holdout_windows", lambda: pd.DataFrame(
        [{"market_id": "m", "slug": "s", "warehouse": "w", "collection": "c"}]))
    monkeypatch.setattr(ql.batch, "run_batch",
                        lambda specs, **kw: launched.append({s.tag for s in specs}))
    qual.run_evaluation(workers=1)
    assert launched and all(t == {"s--default--corrected_volatility",
                                  "s--comparison--corrected_volatility"} for t in launched)
    qual.run_evaluation(workers=1)  # completed stages are never rerun
    assert len(launched) == len(qual.scenarios)
    saved = json.loads(qual.record_file.read_text())
    assert all(saved["stages"][f"evaluation/{s}"]["status"] == "completed"
               for s in qual.scenarios)


def test_a_touched_holdout_refuses_the_evaluation(qual, monkeypatch) -> None:
    record = qual.record()
    record["frozen"] = {"policies": {}, "holdout_check": {"untouched": False}}
    qual._save(record)
    with pytest.raises(ql.QualificationOrderError):
        qual.run_evaluation(workers=1)


def test_veto_evidence_counts_what_a_threshold_would_remove() -> None:
    decisions = pd.DataFrame([
        {"tag": "a", "scenario_name": "friction", "action": "ENTER", "at_ns": 1,
         "up_bid": 0.4, "up_ask": 0.42, "p_up_point": 0.71},   # 30 pp
        {"tag": "b", "scenario_name": "friction", "action": "ENTER", "at_ns": 1,
         "up_bid": 0.4, "up_ask": 0.42, "p_up_point": 0.45},   # 4 pp
    ])
    summaries = pd.DataFrame([
        {"tag": "a", "scenario_name": "friction", "filled": True, "realized_pnl": 2.0},
        {"tag": "b", "scenario_name": "friction", "filled": True, "realized_pnl": -4.0},
    ])
    table = ql.veto_evidence(decisions, summaries, thresholds=(9.0,))
    row = table.iloc[0]
    assert row["fills_vetoed"] == 1 and row["vetoed_wins"] == 1 and row["vetoed_losses"] == 0
