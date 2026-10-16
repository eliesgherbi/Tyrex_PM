"""Results explorer: metric definitions, configuration identity, filtering, widgets."""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

from tyrex_pm.research.strategy_lab import explorer as ex  # noqa: E402

SPEC = ex.DatasetSpec("t", "test", Path("unused"), "scope", ("lim",), "traces")
A = json.dumps(
    {
        "entry": {"min_edge_per_share": "0.05", "tau_max_s": 150.0},
        "model": {"backend": "twap_proxy_diffusion"},
        "sizing": {"max_debit_usdc": "5"},
    }
)
B = json.dumps(
    {
        "entry": {"min_edge_per_share": "0.05", "tau_max_s": 240.0},
        "model": {"backend": "twap_proxy_diffusion"},
        "sizing": {"max_debit_usdc": "5"},
    }
)


def run(
    config,
    chash,
    market,
    start,
    *,
    entered,
    filled,
    pnl=None,
    debit=None,
    scenario="friction",
    ev=1.0,
    p=0.6,
):
    return {
        "config": config,
        "config_hash": chash,
        "scenario_name": scenario,
        "market_id": market,
        "window_start_ns": start,
        "collection": "c",
        "entered": entered,
        "filled": filled,
        "realized_pnl": pnl,
        "debit": debit,
        "ev_point": ev if entered else None,
        "ev_conservative": ev / 2 if entered else None,
        "p_conservative": p if entered else None,
        "entry_tau_s": 150.0 if entered else None,
        "slug": market,
        "side": "UP" if entered else None,
        "winner": "UP",
        "limitations": "",
        "entry_reason_counts": '{"WAIT:X": 3}',
    }


def frame() -> pd.DataFrame:
    rows = [
        run(A, "ha", "m1", 3, entered=True, filled=True, pnl=2.0, debit=4.0),
        run(A, "ha", "m2", 1, entered=True, filled=True, pnl=-5.0, debit=5.0),
        run(A, "ha", "m3", 2, entered=True, filled=True, pnl=0.0, debit=3.0),
        run(A, "ha", "m4", 4, entered=True, filled=False),
        run(A, "ha", "m5", 5, entered=False, filled=False),
        run(A, "ha", "m6", 6, entered=True, filled=True, pnl=None, debit=2.0),  # unknown outcome
        run(B, "hb", "m1", 3, entered=False, filled=False),
        run(A, "ha", "m1", 3, entered=True, filled=True, pnl=1.0, debit=4.0, scenario="favorable"),
    ]
    return pd.DataFrame(rows)


def test_metrics_follow_stated_definitions() -> None:
    ds = ex.prepare(frame(), SPEC)
    m = ex.comparison(ds, "friction").set_index("config_id").loc["ha"]
    assert (m.windows, m.entry_requests, m.fills, m.unfilled, m.skipped) == (6, 5, 4, 1, 1)
    assert (m.resolved_fills, m.unresolved_fills) == (3, 1)
    assert (m.wins, m.losses, m.breakeven) == (1, 1, 1)
    assert m.win_rate == pytest.approx(1 / 3)  # unknown outcome excluded, not a loss
    assert m.net_pnl == pytest.approx(-3.0)  # missing P&L not counted as zero... nor summed
    assert m.spending == pytest.approx(14.0)
    assert m.return_on_spending == pytest.approx(-3.0 / 12.0)  # resolved spending only
    assert m.avg_pnl_per_fill == pytest.approx(-1.0)
    assert (m.avg_win, m.avg_loss) == (pytest.approx(2.0), pytest.approx(-5.0))
    assert m.mean_ev_point == pytest.approx(1.0)


def test_no_fills_has_zero_pnl_but_no_rates_and_scenarios_stay_separate() -> None:
    ds = ex.prepare(frame(), SPEC)
    b = ex.comparison(ds, "friction").set_index("config_id").loc["hb"]
    assert b.net_pnl == 0.0 and math.isnan(b.win_rate) and math.isnan(b.return_on_spending)
    fav = ex.comparison(ds, "favorable")
    assert list(fav["config_id"]) == ["ha"] and fav["net_pnl"].iloc[0] == 1.0


def test_identity_is_the_full_configuration_and_labels_stay_unique() -> None:
    rows = frame()
    clone = rows[rows["config_hash"] == "hb"].assign(config_hash="hc")  # same overrides
    ds = ex.prepare(pd.concat([rows, clone]), SPEC)
    assert ds.configs["config_id"].nunique() == 3
    assert ds.configs["label"].is_unique
    assert ex.config_label(json.loads(A)) == "TWAP | edge 5¢ | entry from 150s remaining | cap 5"
    assert (
        ex.config_label(
            {"entry": {"limit_buffer_ticks": 1}, "model": {"backend": "endpoint_diffusion"}}
        )
        == "Endpoint | limit +1 tick"
    )
    assert ex.config_label({"volatility": {"half_life_s": 30}}) == "volatility.half_life_s=30"
    bad = rows.assign(config=[A, B, A, A, A, A, A, A])  # hash hb now has two configs
    with pytest.raises(ValueError):
        ex.prepare(bad, SPEC)


def test_status_filtering_and_windows_order() -> None:
    ds = ex.prepare(frame(), SPEC)
    w = ex.windows_for(ds, "ha", "friction")
    assert list(w["market_id"]) == ["m2", "m3", "m1", "m4", "m5", "m6"]
    assert list(w["status"]) == ["loss", "breakeven", "win", "unfilled", "skipped", "unresolved"]
    assert len(ex.comparison(ds, "friction", min_fills=1)) == 1
    fields, reasons = ex.window_detail(w.iloc[4])
    assert fields.loc["Realized simulated P&L", "Value"] == "no trade"
    assert reasons.to_dict() == {"WAIT:X": 3}


def test_charts_handle_empty_and_small_samples() -> None:
    ds = ex.prepare(frame(), SPEC)
    table = ex.comparison(ds, "friction")
    for metric in ex.METRICS:
        assert ex.plot_comparison(table, metric, selected="ha") is not None
    assert ex.plot_comparison(table.iloc[0:0]) is None
    w = ex.windows_for(ds, "ha", "friction")
    assert ex.plot_windows(w, selected="m1") is not None
    assert ex.plot_distribution(w) is not None  # 3 trades -> dots
    assert ex.plot_distribution(ex.windows_for(ds, "hb", "friction")) is None
    assert "No resolved" in ex.distribution_note(ex._metrics(ex.windows_for(ds, "hb", "friction")))


@pytest.mark.skipif(
    not all(s.path.exists() for s in ex.DATASETS.values()), reason="saved S2 summaries not present"
)
def test_widget_controls_update_views_consistently() -> None:
    pytest.importorskip("ipywidgets")
    app = ex.Explorer(dataset="validation_seed", scenario="friction")
    assert len(app.w_config.options) == ex.load_dataset("validation_seed").configs.shape[0]
    app.w_dataset.value = "grid_partial"
    grid = ex.load_dataset("grid_partial")
    assert [o[1] for o in app.w_config.options] == list(grid.configs["config_id"])
    assert app.w_scenario.value == "friction"
    target = grid.configs["config_id"].iloc[5]
    app.w_config.value = target
    expected = ex.windows_for(grid, target, "friction")["market_id"].tolist()
    assert [o[1] for o in app.w_window.options] == expected
    app.w_scenario.value = "favorable"
    assert [o[1] for o in app.w_window.options] == ex.windows_for(grid, target, "favorable")[
        "market_id"
    ].tolist()
    app.w_metric.value = "win_rate"
    app.w_min.value = 1
    app.w_window.value = expected[-1]
    assert ex.load_dataset("grid_partial") is grid  # cached, not reloaded


@pytest.mark.skipif(not all(s.path.exists() for s in ex.DATASETS.values()),
                    reason="saved S2 summaries not present")
def test_redraws_replace_outputs_instead_of_appending() -> None:
    pytest.importorskip("ipywidgets")
    app = ex.Explorer(dataset="validation_seed", scenario="friction")
    areas = {name: getattr(app, name) for name in
             ("out_scope", "out_compare", "out_summary", "out_windows", "out_detail",
              "out_timeline", "out_dist")}
    first = {name: len(out.children) for name, out in areas.items()}
    assert all(first.values())  # every section rendered something
    for _ in range(3):  # redraw everything several times
        app.w_metric.value = "win_rate"
        app.w_metric.value = "net_pnl"
        app.w_scenario.value = "favorable"
        app.w_scenario.value = "friction"
        app.w_window.value = [o[1] for o in app.w_window.options][-1]
    assert {name: len(out.children) for name, out in areas.items()} == first
    import ipywidgets as w

    assert len(app.out_scope.children) == 1  # one scope box, not one per redraw
    assert any(isinstance(c, w.Image) for c in app.out_compare.children)
