"""Dataset catalog: batch experiments (v3, case runs), preserved study bundles, explanations."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

from tyrex_pm.research.strategy_lab import explorer as ex  # noqa: E402
from tyrex_pm.research.strategy_lab import timelines as tl  # noqa: E402

PARAMS = {"entry": {"min_edge_per_share": "0.02"}}
CONFIG = json.dumps(PARAMS, sort_keys=True)


def _summary_row(market: str, scenario: str, **extra) -> dict:
    return {"slug": market, "collection": "c", "market_id": market, "window_start_ns": 0,
            "config": CONFIG, "config_hash": "h1", "scenario_name": scenario,
            "entered": True, "filled": True, "realized_pnl": 1.0, "debit": 1.0,
            "digest": "d", "tag": f"{market}--default--unchanged", **extra}


def _v3_experiment(tmp_path: Path, *, with_column: bool) -> Path:
    path = tmp_path / "v3_verification-ref_v1-x"
    for scenario in ("friction", "favorable"):
        folder = path / scenario
        folder.mkdir(parents=True)
        row = _summary_row("m1", scenario)
        if with_column:
            row["execution_policy"] = json.dumps({"reference_availability": "legacy"})
        pd.DataFrame([row]).to_parquet(folder / "summaries.parquet")
        pd.DataFrame([{"market_id": "m1", "scenario_name": scenario, "config": CONFIG,
                       "at_ns": 1, "action": "WAIT"}]).to_parquet(folder / "decisions.parquet")
    (path / "experiment.json").write_text(json.dumps({
        "kind": "v3_verification", "candidates": {},
        "replay": {"execution_contract": "replay_execution_contract_v1"},
        "execution_policy": {"reference_availability": "reference_availability_v1"}}))
    return path


def test_older_v3_tables_get_the_experiment_policy_and_contract(tmp_path: Path) -> None:
    dataset = ex.load_batch(_v3_experiment(tmp_path, with_column=False))
    assert dataset.kind == "batch" and set(dataset.scenarios) == {"favorable", "friction"}
    row = dataset.runs.iloc[0]
    assert tl._row_policy(row) == {"execution_contract": "replay_execution_contract_v1",
                                   "reference_availability": "reference_availability_v1"}
    # The run's own scenario folder holds its saved trace.
    assert tl.decisions_file(dataset, row).parent.name == row["scenario_name"]
    trace = tl.get_trace(dataset, row)
    assert trace is not None and trace.source == "original"


def test_a_recorded_row_policy_is_never_overwritten(tmp_path: Path) -> None:
    dataset = ex.load_batch(_v3_experiment(tmp_path, with_column=True))
    assert tl._row_policy(dataset.runs.iloc[0]) == {"reference_availability": "legacy"}


def test_p7_splits_are_discovered_separately_with_saved_traces(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "QUALIFICATION_ROOT", tmp_path)

    def no_replay(*args, **kwargs):
        pytest.fail("Browsing P7 must not launch a replay")

    monkeypatch.setattr(tl, "_replay", no_replay)
    root = tmp_path / "p7-test"
    root.mkdir()
    policy = {"execution_contract": "replay_execution_contract_v2",
              "reference_availability": "reference_availability_v1"}
    (root / "qualification.json").write_text(json.dumps({
        "kind": "p7_qualification", "execution": policy}))
    for split, market in (("development", "dev-window"), ("evaluation", "eval-window")):
        for scenario in ("friction", "favorable"):
            folder = root / split / scenario
            folder.mkdir(parents=True)
            pd.DataFrame([_summary_row(
                market, scenario, tag=f"{market}--default--legacy_volatility")]
            ).to_parquet(folder / "summaries.parquet")
            pd.DataFrame([{"market_id": market, "scenario_name": scenario,
                           "config": CONFIG, "at_ns": 1, "action": "WAIT"}]
            ).to_parquet(folder / "decisions.parquet")

    menu = dict((key, title) for title, key in ex.available_datasets())
    for split, market in (("development", "dev-window"), ("evaluation", "eval-window")):
        key = ex.dataset_key(root / split)
        assert menu[key] == f"P7 {split} p7-test"
        dataset = ex.load_dataset(key)
        assert dataset.spec.title == menu[key]
        assert set(dataset.runs["market_id"]) == {market}
        assert dataset.spec.evidence_split == split
        assert dataset.counts["runs"] == 2
        assert dataset.configs["label"].tolist() == ["default · legacy_volatility"]
        row = dataset.runs.iloc[0]
        assert tl._row_policy(row) == policy
        assert tl.get_trace(dataset, row).source == "original"
        if split == "evaluation":
            assert "These are development windows" not in ex.scope_html(dataset)
            assert "evaluation windows have now been used" in ex.scope_html(dataset)


def test_case_run_explanations_are_found_for_exactly_the_selected_run(tmp_path) -> None:
    path = tmp_path / "cases-abc"
    path.mkdir()
    pd.DataFrame([_summary_row("m1", "friction",
                               execution_policy=json.dumps({"execution_contract": "v2"}))]
                 ).to_parquet(path / "summaries.parquet")
    indicators = json.dumps({"ewma_vol:1": {"sigma": 0.001, "variance": 1e-6,
                                            "variance_units": "per_second",
                                            "sample_count": 30, "tape_age_ms": 20.0}})
    pd.DataFrame([
        {"market_id": "m1", "config": CONFIG, "scenario_name": "friction",
         "tag": "m1--default--unchanged", "at_ns": 5, "tau_s": 100.0, "action": "WAIT",
         "reason": "EDGE_BELOW_COST", "context_for_at_ns": None, "indicators": indicators},
        {"market_id": "m2", "config": CONFIG, "scenario_name": "friction",
         "tag": "m2--default--unchanged", "at_ns": 6, "tau_s": 99.0, "action": "WAIT",
         "reason": "x", "context_for_at_ns": None, "indicators": indicators},
    ]).to_parquet(path / "explanations.parquet")
    dataset = ex.load_batch(path)
    assert dataset.configs["label"].tolist() == ["default · unchanged"]
    frame = tl.explanations_for(dataset, dataset.runs.iloc[0])
    assert frame["market_id"].tolist() == ["m1"]
    table = tl.explanation_table(frame)
    assert table.loc[0, "sigma"] == 0.001 and table.loc[0, "samples"] == 30
    assert ex.dataset_key(path).startswith(ex.BATCH_PREFIX)


REAL_V3 = ex.STAGED / "v3_verification-ref_v1-141434f36a"
REAL_STUDY = ex.REPO / "var/research/settlement_extreme_cases/development_cases_v1-51563af1bc8c"


@pytest.mark.skipif(not REAL_V3.exists(), reason="local v3 verification results absent")
def test_real_v3_verification_is_in_the_catalog_and_keeps_its_policy() -> None:
    keys = dict((k, t) for t, k in ex.available_datasets())
    key = ex.dataset_key(REAL_V3)
    assert key in keys and not any(k == ex.STAGED_PREFIX + REAL_V3.name for k in keys)
    dataset = ex.load_dataset(key)
    assert {tl._row_policy(r)["reference_availability"]
            for _, r in dataset.runs.iterrows()} == {"reference_availability_v1"}


@pytest.mark.skipif(not REAL_STUDY.exists(), reason="local notebook 06 study bundles absent")
def test_preserved_study_bundles_open_with_saved_traces() -> None:
    dataset = ex.load_dataset(ex.dataset_key(REAL_STUDY))
    assert dataset.kind == "study" and len(dataset.runs) == 42
    row = dataset.runs.iloc[0]
    trace = tl.get_trace(dataset, row)
    assert trace is not None and "saved study trace" in trace.provenance
    params, problems, notes = tl.replay_check(dataset, row)
    assert not problems and any("production path" in n for n in notes)
