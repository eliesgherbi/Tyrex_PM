"""Staged experiments: configuration generation, fixed baseline, holdout exclusion, saved
effective settings, compatible resume, finalists and explorer loading (batch stubbed)."""

from __future__ import annotations

import json

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

from tyrex_pm.research.strategy_lab import batch, explorer  # noqa: E402
from tyrex_pm.research.strategy_lab import staged as st  # noqa: E402
from tyrex_pm.research.strategy_lab.experiments import label  # noqa: E402

SPLIT = {"policy": "test", "development": ["d1", "d2", "d3"], "holdout": ["h1"]}
BASELINE = {
    "volatility": {"half_life_s": 30.0, "sigma_floor": 1e-5, "sigma_scale": 1.0},
    "entry": {"tau_min_s": 30.0, "limit_buffer_ticks": 1, "target_mode": "cap_only"},
    "sizing": {"max_debit_usdc": "5", "max_loss_usdc": "5"},
}


def windows(ids=("d1", "d2")) -> pd.DataFrame:
    return pd.DataFrame([
        {"market_id": m, "collection": "c", "warehouse": "w", "slug": m,
         "window_start_ns": i * 300_000_000_000,
         "role": "holdout (excluded)" if m.startswith("h") else "development"}
        for i, m in enumerate(ids)])


@pytest.fixture
def env(monkeypatch, tmp_path):
    calls = []

    def fake_batch(specs, *, out, workers, keep_decisions, split):
        calls.append(specs)
        assert not set(s.market_id for s in specs) & set(SPLIT["holdout"])
        rows = []
        for i, s in enumerate(specs):
            filled = i % 3 != 2
            rows.append({
                "market_id": s.market_id, "slug": s.market_id, "collection": "c",
                "window_start_ns": int(s.market_id[1:]) * 300_000_000_000,
                "config": label(s.parameters), "config_hash": st.config_id(s.parameters),
                "scenario_name": s.scenario, "entered": i % 4 != 3, "filled": filled,
                "realized_pnl": (2.0 if i % 2 else -5.3) if filled else None,
                "debit": 5.3 if filled else None, "winner": "UP", "side": "UP",
                "ev_point": 1.0, "ev_conservative": 0.4, "p_conservative": 0.6,
                "label_type": "proxy_outcome", "limitations": "", "entry_reason_counts": "{}",
            })
        frame = pd.DataFrame(rows)
        out.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(out / "summaries.parquet", index=False)
        return frame, None

    monkeypatch.setattr(st, "frozen_split", lambda path=None: SPLIT)
    monkeypatch.setattr(st, "_cache_keys", lambda ws: {w["market_id"]: "k" for w in ws})
    monkeypatch.setattr(st, "code_identity", lambda: {"source_fingerprint": "code1",
                                                      "versions": {"v": 1}})
    monkeypatch.setattr(batch, "run_batch", fake_batch)
    return tmp_path, calls


def test_default_design_generates_six_primary_and_one_benchmark() -> None:
    design = st.Design(baseline=BASELINE)
    design.validate()
    a, b = design.stage_parameters("A"), design.stage_parameters("B")
    assert len(a) == 6 and len(b) == 1
    labels = [st.config_label(p) for p in a]
    assert len(set(labels)) == 6 and labels[0] == "TWAP | edge 2¢ | entry from 240s remaining"
    assert st.config_label(b[0]) == "Endpoint | edge 5¢ | entry from 240s remaining"
    effective = [st.effective_parameters(p) for p in a + b]
    for eff in effective:  # fixed settings identical across every configuration and stage
        assert eff["volatility"] == effective[0]["volatility"]
        assert eff["sizing"] == effective[0]["sizing"]
        assert eff["entry"]["limit_buffer_ticks"] == 1
        assert eff["entry"]["tau_min_s"] == 30.0
        assert eff["model"]["averaging_window_s"] == 60.0
    assert {e["model"]["backend"] for e in effective} == {"twap_proxy_diffusion",
                                                         "endpoint_diffusion"}
    assert len(st.Design(baseline=BASELINE, edges=("0.05",)).stage_parameters("A")) == 2


def test_locked_and_invalid_settings_are_rejected() -> None:
    with pytest.raises(ValueError, match="evidence-labelled"):
        st.Design(baseline={"model": {"averaging_window_s": 30.0}}).validate()
    with pytest.raises(ValueError):
        st.Design(baseline={"fees": {"rate": "0"}}).validate()
    with pytest.raises(ValueError, match="duplicate"):
        st.Design(edges=("0.02", "0.02")).validate()
    with pytest.raises(Exception):  # ConfigError from the project's own validation
        st.Design(baseline={"entry": {"target_mode": "take_profit"}}).validate()


def test_holdout_windows_are_refused(env) -> None:
    root, _ = env
    with pytest.raises(ValueError, match="holdout"):
        st.create_or_open(st.Design(baseline=BASELINE), windows(("d1", "h1")), root=root)
    table = windows(("d1", "h1", "d2"))
    assert list(st.select_windows(table)["market_id"]) == ["d1", "d2"]
    assert list(st.select_windows(table, smoke_windows=1)["market_id"]) == ["d1"]


def test_stages_save_effective_settings_and_keep_scenarios_apart(env) -> None:
    root, calls = env
    exp = st.create_or_open(st.Design(baseline=BASELINE), windows(), root=root, write=False)
    assert not exp.saved and not any(root.iterdir())  # planning writes nothing
    assert st.preview(exp, "A", workers=2).loc["total simulations", "value"] == 12
    st.run_stage(exp, "A", workers=2)
    rec = exp.stage_record("A")
    assert rec["status"] == "completed" and len(rec["configurations"]) == 6
    for c in rec["configurations"]:
        assert set(c["effective"]) == {"settlement", "model", "volatility", "entry", "sizing",
                                       "quality", "fees"}
        assert c["runs_saved"] == 2
    assert all(s.parameters == st.effective_parameters(s.parameters) for s in calls[0])

    with pytest.raises(ValueError, match="at most"):
        st.run_stage(exp, "C", workers=1, finalists=["a", "b", "c"])
    chosen = [rec["configurations"][2]["label"], rec["configurations"][5]["config_id"]]
    st.run_stage(exp, "C", workers=1, finalists=chosen)
    c_specs = calls[-1]
    assert {s.scenario for s in c_specs} == {"favorable"}
    a_params = {json.dumps(s.parameters, sort_keys=True) for s in calls[0]}
    assert {json.dumps(s.parameters, sort_keys=True) for s in c_specs} <= a_params

    ds = explorer.load_experiment(exp.path)
    assert set(ds.stages["stage"]) == {"A", "C"} and ds.stages["loaded"].all()
    fric, fav = explorer.comparison(ds, "friction"), explorer.comparison(ds, "favorable")
    assert len(fric) == 6 and len(fav) == 2
    assert set(fav["config_id"]) <= set(fric["config_id"])  # same identity across scenarios
    assert fav["label"].str.startswith("TWAP").all()
    assert (fric["over_budget"] > 0).any()  # 5.3 spent against a cap of 5 is flagged
    assert "Fills spending above" in explorer.summary_table(
        explorer._metrics(explorer.windows_for(ds, fav["config_id"].iloc[0], "favorable"))
    ).index[9]
    assert set(explorer.overrides_table(ds, fric["config_id"].iloc[0]).index) == {
        "entry.min_edge_per_share", "entry.tau_max_s"}


def test_resume_refuses_incompatible_inputs(env) -> None:
    root, calls = env
    design = st.Design(baseline=BASELINE)
    exp = st.create_or_open(design, windows(), root=root, name="exp")
    with pytest.raises(st.IncompatibleStage):  # same name, different baseline
        st.create_or_open(st.Design(baseline={**BASELINE, "volatility": {"half_life_s": 60.0}}),
                          windows(), root=root, name="exp")
    assert st.create_or_open(design, windows(), root=root, name="exp").id == "exp"
    st.run_stage(exp, "B", workers=1)
    n = len(calls)
    st.run_stage(exp, "B", workers=1)  # completed: nothing reruns
    assert len(calls) == n

    path = exp.stage_dir("B") / "stage.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["status"] = "interrupted"
    rec["code"]["source_fingerprint"] = "older-code"
    path.write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(st.IncompatibleStage, match="source changed"):
        st.run_stage(exp, "B", workers=1)
    st.run_stage(exp, "B", workers=1, allow_code_change=True)  # explicit, recorded
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["status"] == "completed" and len(rec["attempts"]) == 2

    rec["fingerprint"] = "different-inputs"
    rec["windows"] = ["some-other-window"]  # a completed stage made from other inputs
    path.write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(st.IncompatibleStage, match="different configurations"):
        st.run_stage(exp, "B", workers=1)


def test_explorer_handles_unfinished_experiment(env) -> None:
    root, _ = env
    exp = st.create_or_open(st.Design(baseline=BASELINE), windows(), root=root)
    with pytest.raises(FileNotFoundError, match="no completed stage"):
        explorer.load_experiment(exp.path)
    with pytest.raises(ValueError, match="completed stage A"):
        st.run_stage(exp, "C", workers=1, finalists=["x"])
    assert st.candidate_table(exp).empty


def test_keep_timelines_is_opt_in_fixed_per_stage_and_never_reruns(env, monkeypatch) -> None:
    root, _ = env
    seen = []
    original = batch.run_batch

    def spy(specs, *, out, workers, keep_decisions, split):
        seen.append(keep_decisions)
        return original(specs, out=out, workers=workers, keep_decisions=keep_decisions,
                        split=split)

    monkeypatch.setattr(batch, "run_batch", spy)
    exp = st.create_or_open(st.Design(baseline=BASELINE), windows(), root=root)
    assert st.preview(exp, "B", workers=1, keep_timelines=True).loc[
        "timelines saved", "value"].startswith("yes")
    st.run_stage(exp, "B", workers=1)  # default: summaries only
    st.run_stage(exp, "A", workers=1, keep_timelines=True)
    assert seen == [False, True]
    assert exp.stage_record("A")["keep_timelines"] is True
    st.run_stage(exp, "B", workers=1, keep_timelines=True)  # completed: note, no rerun
    assert seen == [False, True]

    path = exp.stage_dir("A") / "stage.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["status"] = "interrupted"
    path.write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(st.IncompatibleStage, match="keep_timelines"):
        st.run_stage(exp, "A", workers=1, keep_timelines=False)
    st.run_stage(exp, "A", workers=1)  # None: resumes with the recorded value
    assert seen == [False, True, True]


def test_replay_version_changes_experiment_and_guards_old_ones(env, monkeypatch) -> None:
    root, calls = env
    design = st.Design(baseline=BASELINE)
    exp = st.create_or_open(design, windows(), root=root)
    assert exp.record["replay"] == st.replay_identity()
    st.run_stage(exp, "A", workers=1)
    n = len(calls)

    # Simulate an experiment frozen before the replay identity was recorded (runner v1).
    path = exp.path / "experiment.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop("replay")
    path.write_text(json.dumps(record), encoding="utf-8")
    old = st.open_experiment(exp.path)
    assert st.experiment_replay(old)["runner"] == "strategy_closed_loop_v1"
    st.run_stage(old, "A", workers=1)  # completed: reported, nothing reruns
    assert len(calls) == n
    with pytest.raises(st.IncompatibleStage, match="mix replay"):
        st.run_stage(old, "B", workers=1)  # a new stage would mix replay versions
    assert "frozen under replay" in st.preview(old, "B", workers=1).loc["status", "value"]

    monkeypatch.setattr(st, "RUNNER_VERSION", "strategy_closed_loop_v9")
    assert st.experiment_id(design, windows(), "full_development") != exp.id


def test_split_extension_is_frozen_chronological_and_keeps_old_holdout(tmp_path) -> None:
    split_dir = tmp_path / "split"
    split_dir.mkdir()
    base = {"policy": "test", "development": ["d1", "d2"], "holdout": ["h1"],
            "boundary_window_start_ns": 0}
    (split_dir / "split.json").write_text(json.dumps(base), encoding="utf-8")
    ns = 300_000_000_000
    inventory = pd.DataFrame(
        [{"collection": "old", "market_id": m, "window_start_ns": i * ns,
          "has_resolution": True, "duplicate_rejected": False}
         for i, m in enumerate(("d1", "d2", "h1"))]
        + [{"collection": "new", "market_id": f"n{i}", "window_start_ns": (100 + i) * ns,
            "has_resolution": i != 9, "duplicate_rejected": False} for i in range(10)])
    path = split_dir / "split.json"
    record = st.extend_split("new", path=path, inventory=inventory)
    assert record["development"] == [f"n{i}" for i in range(6)]  # earliest windows
    assert record["holdout"] == ["n6", "n7", "n8"]  # latest 30%; n9 has no outcome
    assert record["excluded_without_resolution"] == 1
    merged = st.frozen_split(path)
    assert merged["holdout"][:1] == ["h1"] and set(record["holdout"]) <= set(merged["holdout"])
    assert len(merged["development"]) == 8 and merged["extensions"][0]["collection"] == "new"
    assert json.loads(path.read_text(encoding="utf-8")) == base  # original untouched
    with pytest.raises(st.IncompatibleStage, match="never changed"):
        st.extend_split("new", path=path, inventory=inventory)
    with pytest.raises(ValueError, match="new usable windows"):
        st.extend_split("old", path=path, inventory=inventory)  # all already split
