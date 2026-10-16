"""Notebook 05 helpers: frozen candidates, run-config agreement, v2/v3 comparison."""

from __future__ import annotations

import pandas as pd
import pytest

from tyrex_pm.research.strategy_lab import v3

V2_IDS = {
    "default": "3b6dbbf4edbdd6d3ac771ecb964342286c391b6fa30bb6e3822b6e54d8805618",
    "comparison": "23ca8d880ec1345c0aa13831d50823affe3253de8ca6b29379f7835ff4143975",
}


def test_candidates_keep_their_v2_identity() -> None:
    candidates = v3.load_candidates()
    assert {k: c.config_id for k, c in candidates.items()} == V2_IDS
    assert candidates["default"].label == "TWAP | edge 2¢ | entry from 150s remaining"
    assert candidates["comparison"].label == "TWAP | edge 5¢ | entry from 240s remaining"


def test_run_configs_match_the_candidates_with_features_off() -> None:
    table = v3.check_run_configs(v3.load_candidates())
    assert not (table["kind"] == "DECISION").any(), table[table["kind"] == "DECISION"]
    assert set(table.loc[table["setting"] == "new features", "config_value"]) == {"off"}


def test_feature_run_ids_and_validation() -> None:
    candidates = v3.load_candidates()
    windows = pd.DataFrame([{"market_id": "m", "warehouse": "w", "collection": "c",
                             "slug": "s", "window_start_ns": 1}])
    on = v3.FeatureRun("retry", candidates["default"], {"retry": {"enabled": True}},
                       windows=windows)
    again = v3.FeatureRun("retry", candidates["default"], {"retry": {"enabled": True}},
                          windows=windows)
    other = v3.FeatureRun("retry", candidates["default"],
                          {"retry": {"enabled": True, "max_attempts_per_episode": 2}},
                          windows=windows)
    assert on.path == again.path != other.path
    assert not on.path.exists()  # constructing never writes
    with pytest.raises(Exception, match="managed_exit"):
        v3.FeatureRun("bad", candidates["default"], {"reentry": {"max_trades_per_window": 2}},
                      windows=windows)


def test_comparison_tables() -> None:
    candidates = v3.load_candidates()
    cid = candidates["default"].config_id

    def rows(pnl3, filled3):
        return pd.DataFrame([
            {"config_hash": cid, "scenario_name": "friction", "market_id": "a", "slug": "a",
             "entered": True, "filled": True, "side": "UP", "debit": 5.0, "realized_pnl": 5.0,
             "entry_attempts": 1, "entry_not_sent": 0, "entry_zero_fill": 0,
             "spend_amount": 4.9},
            {"config_hash": cid, "scenario_name": "friction", "market_id": "b", "slug": "b",
             "entered": True, "filled": filled3, "side": "DOWN" if filled3 else None,
             "debit": 4.9 if filled3 else 0.0, "realized_pnl": pnl3,
             "entry_attempts": 1, "entry_not_sent": 0, "entry_zero_fill": 0 if filled3 else 1,
             "spend_amount": 4.9},
        ])

    v2 = rows(-5.0, True)
    v3_rows = rows(None, False)
    table = v3.compare_v2_v3(v2, v3_rows, candidates)
    assert table.loc[("default", "friction"), "fills_v2"] == 2
    assert table.loc[("default", "friction"), "fills_v3"] == 1
    changed = v3.changed_windows(v2, v3_rows, candidates)
    assert list(changed["slug_v2"]) == ["b"]
    assert changed["reason"].iloc[0] == "no fill under the amount order"


@pytest.mark.skipif(not (v3.v2_experiment_path() / "experiment.json").exists(),
                    reason="v2 experiment not present")
def test_frozen_windows_and_v2_rows() -> None:
    windows = v3.frozen_windows()
    assert len(windows) == 94
    v2 = v3.v2_summaries(v3.load_candidates())
    assert set(v2["scenario_name"]) == {"friction", "favorable"}
    assert set(v2["config_hash"]) == set(V2_IDS.values())
