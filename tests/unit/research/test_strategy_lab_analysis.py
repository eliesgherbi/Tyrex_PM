"""Analysis helpers: common-row probability quality, bootstrap, neighbourhoods, split."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from tyrex_pm.research.strategy_lab.analysis import (
    bootstrap_total,
    checkpoint_rows,
    neighbour_scores,
    probability_quality,
    select_shortlist,
    trade_economics,
)
from tyrex_pm.research.strategy_lab.batch import chronological_split


def decisions() -> pd.DataFrame:
    rows = []
    for m, winner in (("a", "UP"), ("b", "DOWN")):
        for i, tau in enumerate((250.0, 200.0, 130.0, 100.0)):
            rows.append({
                "config": "c", "scenario_name": "favorable", "market_id": m, "at_ns": i,
                "tau_s": tau, "label_type": "proxy_outcome", "winner": winner,
                "endpoint_p_up": 0.8 if winner == "UP" else 0.3,
                "twap_p_up": 0.9 if winner == "UP" else 0.2,
                "book_implied_up": None if (m == "b" and tau == 200.0) else 0.6,
            })
    return pd.DataFrame(rows)


def test_checkpoints_take_first_row_at_or_after_and_quality_uses_common_rows() -> None:
    rows = checkpoint_rows(decisions(), checkpoints=(240.0, 120.0))
    assert set(rows["checkpoint_s"]) == {240.0, 120.0}
    assert rows[rows["checkpoint_s"] == 240.0]["tau_s"].tolist() == [200.0, 200.0]
    quality = probability_quality(rows)
    at240 = quality[quality["checkpoint_s"] == 240.0]
    assert set(at240["n"]) == {1}  # window b has no book at that row -> dropped for all
    twap = at240[at240["source"] == "twap_p_up"]["brier"].iloc[0]
    assert twap == pytest.approx(0.01)


def test_bootstrap_interval_contains_the_total() -> None:
    values = np.array([1.0, -1.0, 2.0, 0.5])
    low, high = bootstrap_total(values, draws=500)
    assert low <= values.sum() <= high


def summaries(config: str, pnl: list[float]) -> list[dict]:
    return [
        {"config": config, "scenario_name": "friction", "filled": True, "entered": True,
         "realized_pnl": p, "debit": 1.0, "ev_conservative": 0.1}
        for p in pnl
    ]


def test_neighbourhood_scores_prefer_stable_regions() -> None:
    def cfg(edge, tau):
        return json.dumps({"entry": {"min_edge_per_share": edge, "tau_max_s": tau}}, sort_keys=True)

    rows = (
        summaries(cfg("0.02", 240), [1.0] * 5)
        + summaries(cfg("0.05", 240), [0.8] * 5)
        + summaries(cfg("0.02", 120), [0.9] * 5)
        + summaries(cfg("0.08", 180), [5.0] * 5)   # isolated spike, no neighbours
        + summaries(cfg("0.05", 120), [0.1] * 2)   # too few fills
    )
    econ = trade_economics(pd.DataFrame(rows))
    scores = neighbour_scores(
        econ, axes=["entry.min_edge_per_share", "entry.tau_max_s"], min_fills=5
    )
    assert scores["fills"].min() >= 5
    assert not scores["config"].str.contains('"0.08"').any()  # isolated spike unscored
    best = select_shortlist(scores, k=1)[0]
    assert best["entry"]["min_edge_per_share"] == "0.02"


def test_chronological_split_rejects_duplicates_and_unlabelled() -> None:
    inventory = pd.DataFrame(
        {
            "market_id": ["m1", "m2", "m3", "m3", "m4", "m5"],
            "window_start_ns": [1, 2, 3, 3, 4, 5],
            "has_resolution": [True, True, True, True, False, True],
            "duplicate_rejected": [False, False, False, True, False, False],
        }
    )
    split = chronological_split(inventory, holdout_fraction=0.25)
    assert split["holdout"] == ["m5"]
    assert split["development"] == ["m1", "m2", "m3"]


def test_neighbour_scores_with_no_eligible_configuration_is_empty_not_an_error() -> None:
    rows = summaries(json.dumps({"entry": {"min_edge_per_share": "0.02"}}), [1.0])
    scores = neighbour_scores(
        trade_economics(pd.DataFrame(rows)), axes=["entry.min_edge_per_share"], min_fills=5
    )
    assert scores.empty and "neighbourhood_mean" in scores.columns
    assert select_shortlist(scores) == []
