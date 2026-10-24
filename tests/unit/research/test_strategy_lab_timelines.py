"""Probability-vs-book timelines: formulas, gaps, markers, lookup, cache and replay rules."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import matplotlib
import numpy as np
import pandas as pd
import pytest

matplotlib.use("Agg")

from tyrex_pm.research.strategy_lab import staged  # noqa: E402
from tyrex_pm.research.strategy_lab import timelines as tl  # noqa: E402

S = 10**9
START = 1_000 * S
END = START + 300 * S


def frame(rows=None) -> pd.DataFrame:
    """Five evaluations: WAIT, host skip, ENTER, HOLD, HOLD (probabilities continue)."""
    base = dict(p_up_point=0.7, p_up_low=0.6, p_up_high=0.8, up_bid=0.5, up_ask=0.6,
                down_bid=0.3, down_ask=0.45, chosen_side=None, reason="", tau_s=0.0,
                window_end_ns=END)
    out = []
    for i, action in enumerate(rows or ["WAIT", "HOST_SKIPPED", "ENTER", "HOLD", "HOLD"]):
        r = {**base, "at_ns": START + (i + 10) * S, "action": action}
        if action == "HOST_SKIPPED":
            r.update(p_up_point=None, p_up_low=None, p_up_high=None, up_bid=None,
                     up_ask=None, down_bid=None, down_ask=None)
        if action == "ENTER":
            r["chosen_side"] = "UP"
        out.append(r)
    return pd.DataFrame(out)


def summary_row(**kw) -> pd.Series:
    base = dict(market_id="m1", scenario_name="friction", config='{"a": 1}', config_hash="h1",
                collection="c", window_start_ns=START, entered=True, filled=True, side="UP",
                fill_ratio=1.0, vwap=0.6, digest="d-saved", stage="A", slug="m1")
    base.update(kw)
    return pd.Series(base)


def test_up_down_formulas_and_independent_midpoints() -> None:
    c = tl.curves(frame()).iloc[0]
    assert c.up_point == pytest.approx(0.7) and c.down_point == pytest.approx(0.3)
    assert c.up_conservative == pytest.approx(0.6)
    assert c.down_conservative == pytest.approx(0.2)  # 1 - p_up_high, not 1 - p_up_low
    assert c.up_mid == pytest.approx(0.55)
    assert c.down_mid == pytest.approx(0.375)  # its own book, not 1 - up_mid (0.45)
    assert c.up_conservative + c.down_conservative < 1


@pytest.mark.parametrize("side,expected", [("UP", 0.7), ("DOWN", 0.3)])
def test_exit_request_marker_uses_held_side_point_probability(side, expected):
    trace = tl.Trace(frame(["EXIT"]), "original", "test")
    marker = tl.markers(trace, summary_row(side=side))[0]
    assert marker["kind"] == "exit_requested"
    assert marker["y"] == pytest.approx(expected)
    assert marker["label"].startswith("EXIT requested")


def test_exit_context_table_keeps_saved_trend_evidence_and_prioritizes_requests():
    data = frame(["HOLD", "EXIT"])
    data["held_side"] = "UP"
    data["exit_trend"] = ['{"confirmed":false}', '{"confirmed":true}']
    rows = tl.exit_context_table(tl.Trace(data, "original", "test"))
    assert rows.action.tolist() == ["EXIT", "HOLD"]
    assert rows.iloc[0].exit_trend == '{"confirmed":true}'
    assert tl.exit_context_table(tl.Trace(frame(), "original", "test")).empty


def test_missing_values_are_kept_and_gaps_break_lines() -> None:
    c = tl.curves(frame())
    assert c.iloc[1][["up_point", "down_conservative", "up_mid", "down_mid"]].isna().all()
    gappy = frame().drop(index=3)  # one evaluation missing entirely: 2 s gap
    gappy.loc[4, "at_ns"] += 5 * S  # and a 6 s gap
    broken = tl.break_gaps(tl.curves(gappy))
    assert len(broken) == len(gappy) + 1
    assert broken["up_point"].isna().sum() == 2  # host skip + inserted break, nothing filled
    assert c.iloc[3:]["up_point"].notna().all()  # curves continue after the entry


def test_markers_distinguish_request_partial_fill_and_unfilled_remainder() -> None:
    events = [
        {"at_ns": START + 12 * S + 300, "event": "arrived", "order_id": "o1"},
        {"at_ns": START + 12 * S + 300, "event": "fill", "order_id": "o1", "leg": "UP",
         "quantity": "3", "price": "0.62"},
        {"at_ns": START + 12 * S + 300, "event": "unfilled_cancelled", "order_id": "o1",
         "quantity": "2"},
    ]
    orders = [{"order_id": "o1", "side": "UP", "quantity": "5", "limit_price": "0.63"}]
    trace = tl.Trace(frame(), "original", "t", orders=orders, events=events,
                     has_event_times=True)
    marks = tl.markers(trace, summary_row(fill_ratio=0.6))
    kinds = [m["kind"] for m in marks]
    assert kinds == ["requested", "arrived", "partial", "unfilled"]
    requested, _, partial, unfilled = marks
    assert requested["label"] == "ENTER requested (UP)" and requested["y"] == 0.6
    assert "probability" in requested["y_meaning"]
    assert partial["y"] == 0.62 and "price" in partial["y_meaning"]
    assert unfilled["y"] == 0.63 and unfilled["at_ns"] == START + 12 * S + 300
    down = frame()
    down.loc[2, "chosen_side"] = "DOWN"
    assert tl.markers(tl.Trace(down, "original", "t"), summary_row(side="DOWN"))[0][
        "y"] == pytest.approx(0.2)


def test_without_event_times_the_outcome_is_text_not_a_marker() -> None:
    trace = tl.Trace(frame(), "original", "t")
    marks = tl.markers(trace, summary_row(filled=False))
    assert [m["kind"] for m in marks] == ["requested", "status"]
    assert marks[1]["at_ns"] is None and "nothing filled" in marks[1]["label"]
    fills = tl.markers(trace, summary_row())
    assert "no fill timestamp saved" in fills[1]["label"]


def test_original_trace_matches_exact_run_only(tmp_path) -> None:
    rows = []
    for cfg in ('{"a": 1}', '{"a": 2}'):
        for scenario in ("friction", "favorable"):
            for market in ("m1", "m2"):
                f = frame().assign(config=cfg, scenario_name=scenario, market_id=market)
                f["p_up_point"] = 0.1 if (cfg, scenario, market) == ('{"a": 1}', "friction",
                                                                     "m1") else 0.9
                rows.append(f)
    pd.concat(rows).to_parquet(tmp_path / "decisions.parquet", index=False)
    ds = SimpleNamespace(spec=SimpleNamespace(path=tmp_path / "summaries.parquet"), stages=None)
    trace = tl.load_original(ds, summary_row())
    assert trace.source == "original" and len(trace.frame) == 5
    assert (trace.frame["p_up_point"] == 0.1).all()
    assert tl.load_original(ds, summary_row(config='{"a": 3}')) is None  # never borrowed


def fake_result(digest: str) -> dict:
    from tyrex_pm.research.strategy_lab.batch import DECISION_COLUMNS

    decisions = [{k: r.get(k) for k in DECISION_COLUMNS} for r in frame().to_dict("records")]
    return {
        "decisions": decisions,
        "window": {"window_end_ns": END, "up_instrument_id": "UP-ID"},
        "orders": [{"order_id": "o1", "side": "UP", "quantity": "5", "limit_price": "0.63",
                    "decided_at_ns": START + 12 * S}],
        "engine": {"ledger": [
            {"at_ns": START + 12 * S + 300, "event": "fill", "order_id": "o1",
             "instrument_id": "UP-ID", "quantity": "5", "price": "0.61"}]},
        "decision_digest": digest,
        "config_hash": "h1",
        "provenance": {"clock_correction": "test"},
    }


@pytest.fixture
def replay_env(monkeypatch, tmp_path):
    calls = []

    def fake_replay(collection, market_id, params, scenario, execution_policy=None, parser=None):
        calls.append((collection, market_id, scenario))
        fake_replay.policies.append(execution_policy)
        return fake_result(fake_replay.digest)

    fake_replay.digest = "d-saved"
    fake_replay.policies = []
    monkeypatch.setattr(tl, "_replay", fake_replay)
    monkeypatch.setattr(tl, "replay_check", lambda ds, row: ({"a": 1}, [], []))
    monkeypatch.setattr(tl, "_data_parser", lambda ds, row: None)
    monkeypatch.setattr(staged, "code_identity", lambda: {"source_fingerprint": "x"})
    ds = SimpleNamespace(spec=SimpleNamespace(path=tmp_path / "summaries.parquet"), stages=None)
    return ds, tmp_path / "cache", calls, fake_replay


def test_cache_reuse_and_replay_only_on_explicit_generate(replay_env) -> None:
    ds, root, calls, _ = replay_env
    row = summary_row()
    assert tl.get_trace(ds, row, root) is None and calls == []  # lookup never replays
    trace = tl.generate(ds, row, root)
    assert len(calls) == 1 and calls[0] == ("c", "m1", "friction")
    assert trace.matches_original is True and trace.is_original_execution
    assert [m["kind"] for m in tl.markers(trace, row)] == ["requested", "fill"]
    again = tl.get_trace(ds, row, root)
    assert len(calls) == 1 and again.source == "diagnostic"  # reopened from cache
    assert tl.get_trace(ds, summary_row(scenario_name="favorable"), root) is None


def test_digest_mismatch_is_labelled_and_hides_execution(replay_env) -> None:
    ds, root, _, fake = replay_env
    fake.digest = "d-new"
    trace = tl.generate(ds, summary_row(), root)
    assert trace.matches_original is False and "differs" in trace.provenance
    marks = tl.markers(trace, summary_row())
    assert [m["kind"] for m in marks] == ["requested", "status"]
    assert "diagnostic replay" in marks[0]["label"] and "saved run" in marks[1]["label"]
    assert tl.plot_timeline(trace, summary_row(), title="t") is not None


def test_refused_replay_never_runs(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(tl, "_replay", lambda *a: calls.append(a))
    monkeypatch.setattr(tl, "_collection", lambda c: object())
    monkeypatch.setattr(tl, "_current_cache_key", lambda c, m, parser=None: "k-now")
    monkeypatch.setattr(tl, "_current_scenario", lambda name: {"name": name})
    monkeypatch.setattr(tl, "_holdout", lambda: {"m-hold"})
    monkeypatch.setattr(tl, "_saved_cache_key", lambda ds, row: "k-then")
    monkeypatch.setattr(tl, "_saved_scenario", lambda ds, row: {"name": "changed"})
    ds = SimpleNamespace(spec=SimpleNamespace(path=tmp_path / "s.parquet"), stages=None)
    row = summary_row(config=json.dumps({"entry": {"limit_buffer_ticks": 1}}),
                      config_hash="not-the-current-hash", market_id="m-hold")
    _, problems, _ = tl.replay_check(ds, row)
    assert len(problems) == 4  # config hash, holdout, data changed, scenario changed
    with pytest.raises(RuntimeError, match="refused"):
        tl.generate(ds, row, tmp_path)
    assert calls == []


def test_run_before_renormalization_replays_on_its_parser_builds(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(tl, "_replay", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(tl, "replay_compact", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(tl, "_save", lambda *a: None)
    monkeypatch.setattr(tl, "_collection", lambda c: object())
    monkeypatch.setattr(tl, "_current_cache_key",
                        lambda c, m, parser=None: "k-then" if parser == "1" else "k-now")
    monkeypatch.setattr(tl, "_holdout", lambda: set())
    monkeypatch.setattr(tl, "_saved_cache_key", lambda ds, row: "k-then")
    monkeypatch.setattr(tl, "_saved_scenario", lambda ds, row: None)
    monkeypatch.setattr(tl, "_current_scenario", lambda name: {"name": name})
    ds = SimpleNamespace(spec=SimpleNamespace(path=tmp_path / "s.parquet"), stages=None)
    row = summary_row(config=json.dumps({"entry": {"limit_buffer_ticks": 1}}))
    row["config_hash"] = staged.config_id(json.loads(row["config"]))
    _, problems, notes = tl.replay_check(ds, row)
    assert problems == [] and any("parser-1 builds" in n for n in notes)
    tl.generate(ds, row, tmp_path)
    assert calls[-1][-1] == "1"  # the replay reads the builds the run used


def test_plot_has_two_probability_panels_and_status_strip() -> None:
    trace = tl.Trace(frame(), "original", "original saved trace")
    fig = tl.plot_timeline(trace, summary_row(), title="t", warmup=True)
    up, down, strip = fig.axes[:3]
    assert up.get_ylim() == (0, 1) and down.get_ylim() == (0, 1)
    assert "original saved trace" in fig._suptitle.get_text()
    assert len(up.lines) >= 3 and len(down.lines) >= 3
    assert "host-skipped" in fig._suptitle.get_text()


def test_lines_join_single_skipped_seconds_but_break_long_gaps() -> None:
    c = tl.curves(frame())  # second 11 host-skipped between valid seconds 10 and 12
    joined = tl.line_series(c, "up_point", join_gap_s=tl.JOIN_GAP_S)
    assert joined["up_point"].notna().all()  # 2 s apart: one readable line, no invented value
    assert len(joined) == 4  # the skipped second is not filled in, just bridged
    strict = tl.line_series(c, "up_point", join_gap_s=1.5)
    assert strict["up_point"].isna().sum() == 1  # strict view breaks at the skip
    long_gap = frame()
    long_gap.loc[3:, "at_ns"] += 30 * S
    broken = tl.line_series(tl.curves(long_gap), "up_point", join_gap_s=tl.JOIN_GAP_S)
    assert broken["up_point"].isna().sum() == 1  # beyond the join limit it always breaks
    up = tl.plot_timeline(tl.Trace(frame(), "original", "t"), summary_row()).axes[0]
    first = np.asarray(up.lines[0].get_ydata(), dtype=float)
    assert not np.isnan(first).any() and not math.isnan(first[1])


def test_generate_many_skips_available_and_resumes(replay_env) -> None:
    ds, root, calls, _ = replay_env
    runs = pd.DataFrame([summary_row(market_id=m, window_label=m) for m in ("m1", "m2", "m3")])
    tl.generate(ds, runs.iloc[0], root)  # m1 already has a timeline
    report = tl.generate_many(ds, runs, workers=1, root=root)
    assert list(report["result"]) == [
        "already available", "generated, matches saved run", "generated, matches saved run"]
    assert [c[1] for c in calls] == ["m1", "m2", "m3"]
    again = tl.generate_many(ds, runs, workers=1, root=root)  # nothing left: no replay
    assert (again["result"] == "already available").all() and len(calls) == 3


def test_cache_key_separates_runs_with_different_saved_digests() -> None:
    assert tl.cache_key(summary_row(digest="a")) != tl.cache_key(summary_row(digest="b"))
    assert tl.cache_key(summary_row(digest="a")) == tl.cache_key(summary_row(digest="a"))
    original = summary_row(execution_policy=json.dumps({"book_reconstruction": "legacy"}))
    revised = summary_row(execution_policy=json.dumps({"book_reconstruction": "venue_top_v1"}))
    assert tl.cache_key(original) != tl.cache_key(revised)
    assert tl.cache_key(original) == tl.cache_key(summary_row())
