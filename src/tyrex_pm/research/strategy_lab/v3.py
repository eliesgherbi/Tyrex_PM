"""Replay v3 research helpers for notebook 05 (verification and optional features).

* ``load_candidates`` — the two frozen v2 hold candidates (default 2c / 30-150 s and
  comparison 5c / 30-240 s), every new trading feature off.
* ``frozen_windows`` — the 94 development windows the v2 experiment froze.
* ``Verification`` — both candidates on those windows under the v2 scenarios with the
  v3 runner, saved as a new experiment directory (v2 results are only read).
* ``compare_v2_v3`` / ``changed_windows`` — what the execution update alone changes.
* ``FeatureRun`` — one optional-feature variant (retry, managed exits, re-entry, SL/TP)
  of one candidate, saved separately; never launched by import or preview.
* ``shared_account`` — ordered-window replay of one candidate through one account.

Every launch is an explicit call. Saved results are never overwritten or mixed: an id is
the digest of everything that determines the results, including replay and code
identity, and a resume with different source code is refused unless accepted.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from . import batch
from .batch import Collection, RunSpec
from .staged import (
    REPO,
    STAGED,
    IncompatibleStage,
    code_identity,
    config_id,
    config_label,
    effective_parameters,
    frozen_split,
    replay_identity,
)

V2_EXPERIMENT = "full_development-7f132d3ee2"
CANDIDATE_FILE = REPO / "config" / "research" / "settlement_value_hold_baseline_v2.json"
RUN_CONFIGS = {
    "default": ("settlement_value_baseline_2c_150s_paper.yaml",
                "settlement_value_baseline_2c_150s_live.template.yaml"),
    "comparison": ("settlement_value_leader_5c_240s_paper.yaml",
                   "settlement_value_leader_5c_240s_live.template.yaml"),
}
SCENARIOS = ("friction", "favorable")  # v2: stage A friction, stage C favorable
VERIFICATION_ROOT = STAGED
FEATURE_ROOT = STAGED / "v3_features"
SHARED_ROOT = STAGED / "v3_shared_account"
# Settings that only label provenance; they do not change any decision.
LABEL_ONLY = {
    "settlement.functional_id",
    "settlement.reference_series",
    "settlement.reference_units",
    "settlement.timestamp_meaning",
    "settlement.provenance_status",
    "settlement.evidence",
    "fees.provenance",
}


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# --------------------------------------------------------------------------- candidates


@dataclass(frozen=True)
class Candidate:
    name: str
    parameters: dict
    role: str

    @property
    def label(self) -> str:
        return config_label(self.parameters)

    @property
    def config_id(self) -> str:
        return config_id(self.parameters)


def load_candidates(path: Path = CANDIDATE_FILE) -> dict[str, Candidate]:
    """Default from the frozen file; the comparison differs only in edge and entry start."""
    base = json.loads(path.read_text(encoding="utf-8"))
    comparison = copy.deepcopy(base)
    comparison["entry"]["min_edge_per_share"] = "0.05"
    comparison["entry"]["tau_max_s"] = 240.0
    return {
        "default": Candidate("default", base, "provisional default: 2c, 30-150 s"),
        "comparison": Candidate("comparison", comparison, "comparison: 5c, 30-240 s"),
    }


def _flatten(effective: dict) -> dict[str, Any]:
    return {f"{s}.{k}": v for s, values in effective.items() for k, v in values.items()}


def check_run_configs(candidates: dict[str, Candidate]) -> pd.DataFrame:
    """Candidate settings vs the strategy block of its paper and live run configs.

    Decision-affecting differences are errors; provenance labels are listed as label-only.
    The new lifecycle sections must be at their feature-off values.
    """
    import yaml

    from tyrex_pm.strategies.settlement_value.schema import (
        settlement_value_config_as_parameters,
        settlement_value_config_from_parameters,
    )

    rows = []
    for name, (paper, live) in RUN_CONFIGS.items():
        expected = _flatten(effective_parameters(candidates[name].parameters))
        for file in (paper, live):
            raw = yaml.safe_load((REPO / "config" / "runs" / file).read_text(encoding="utf-8"))
            params = raw["strategy"]["parameters"]
            cfg = settlement_value_config_from_parameters(params, file=file)
            actual = _flatten(settlement_value_config_as_parameters(cfg))
            off = (not cfg.retry.enabled and cfg.exit.policy == "hold_to_resolution"
                   and not cfg.exit.value_exit_enabled and not cfg.exit.thesis_stop_enabled
                   and cfg.reentry.max_trades_per_window == 1)
            # Lifecycle sections are checked as "features off" below, not value by value
            # (the configs pin retry ceilings to 1, which is also off).
            keys = {k for k in set(expected) | set(actual)
                    if k.split(".")[0] not in ("retry", "exit", "reentry")}
            for key in sorted(keys):
                if expected.get(key) != actual.get(key):
                    rows.append({"candidate": name, "file": file, "setting": key,
                                 "candidate_value": expected.get(key),
                                 "config_value": actual.get(key),
                                 "kind": "label only" if key in LABEL_ONLY else "DECISION"})
            rows.append({"candidate": name, "file": file, "setting": "new features",
                         "candidate_value": "off", "config_value": "off" if off else "ON",
                         "kind": "ok" if off else "DECISION"})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- windows


def v2_experiment_path(experiment: str = V2_EXPERIMENT) -> Path:
    return STAGED / experiment


def frozen_windows(experiment: str = V2_EXPERIMENT) -> pd.DataFrame:
    record = json.loads((v2_experiment_path(experiment) / "experiment.json").read_text("utf-8"))
    frame = pd.DataFrame(record["windows"])
    holdout = set(frozen_split()["holdout"])
    leaked = set(frame["market_id"]) & holdout
    if leaked:
        raise ValueError(f"frozen windows overlap the holdout: {sorted(leaked)[:2]}")
    frame["window_utc"] = pd.to_datetime(frame["window_start_ns"], unit="ns", utc=True)
    return frame.sort_values("window_start_ns").reset_index(drop=True)


def _specs(windows: pd.DataFrame, params_list: list[dict], scenario: str, tag: str,
           execution_policy: dict | None = None) -> list[RunSpec]:
    specs = []
    for w in windows.itertuples():
        for params in params_list:
            specs.append(RunSpec(
                collection=Collection(str(w.warehouse), str(w.collection)),
                market_id=w.market_id,
                parameters=effective_parameters(params),
                scenario=scenario,
                tag=tag,
                execution_policy=dict(execution_policy or {}),
            ))
    return specs


def _done(out: Path, specs: list[RunSpec], keep: bool) -> int:
    parts = out / "parts"
    groups: dict[str, list[RunSpec]] = {}
    for s in specs:
        groups.setdefault(s.market_id, []).append(s)
    return sum(len(g) for g in groups.values()
               if (parts / f"{batch._group_digest(g, keep)}.summaries.parquet").exists())


class _SavedRun:
    """A directory holding one or more batch outputs plus an identity record."""

    path: Path
    record: dict

    @property
    def record_file(self) -> Path:
        return self.path / "experiment.json"

    def _check_or_freeze(self, allow_code_change: bool) -> None:
        code = code_identity()
        if self.record_file.exists():
            saved = json.loads(self.record_file.read_text(encoding="utf-8"))
            if saved["fingerprint"] != self.record["fingerprint"]:
                raise IncompatibleStage(f"{self.path} holds different inputs; never mixed")
            if (saved["code_at_creation"]["source_fingerprint"] != code["source_fingerprint"]
                    and not allow_code_change):
                raise IncompatibleStage(
                    "source code changed since this run started; resuming would mix code "
                    "versions (pass allow_code_change=True to accept, recorded)")
            self.record = saved
            return
        self.path.mkdir(parents=True, exist_ok=True)
        self.record = {**self.record, "created_utc": _now(), "code_at_creation": code}
        self.record_file.write_text(json.dumps(self.record, indent=1, default=str), "utf-8")

    def _write(self) -> None:
        self.record_file.write_text(json.dumps(self.record, indent=1, default=str), "utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- verification


class Verification(_SavedRun):
    """Both candidates, every new feature off, on the frozen v2 windows and scenarios."""

    def __init__(self, candidates: dict[str, Candidate], windows: pd.DataFrame,
                 scenarios: tuple[str, ...] = SCENARIOS, keep_timelines: bool = False):
        self.candidates = candidates
        self.windows = windows
        self.scenarios = tuple(scenarios)
        self.keep = keep_timelines
        identity = {
            "kind": "v3_verification",
            "source_experiment": V2_EXPERIMENT,
            "candidates": {k: c.parameters for k, c in candidates.items()},
            "windows": list(windows["market_id"]),
            "scenarios": list(self.scenarios),
            "replay": replay_identity(),
            "keep_timelines": keep_timelines,
        }
        fingerprint = _digest(identity)
        self.path = VERIFICATION_ROOT / f"v3_verification-{fingerprint[:10]}"
        self.record = {**identity, "experiment_id": self.path.name, "fingerprint": fingerprint,
                       "status": {}}

    @property
    def id(self) -> str:
        return self.path.name

    def specs(self, scenario: str) -> list[RunSpec]:
        params = [c.parameters for c in self.candidates.values()]
        return _specs(self.windows, params, scenario, f"v3_verification:{self.id}:{scenario}")

    def preview(self, workers: int) -> pd.DataFrame:
        from .staged import seconds_per_run

        rows = []
        for scenario in self.scenarios:
            specs = self.specs(scenario)
            done = _done(self.path / scenario, specs, self.keep)
            per_run, _ = seconds_per_run(scenario)
            rows.append({"scenario": scenario, "runs": len(specs), "done": done,
                         "remaining": len(specs) - done,
                         "estimate_min": round((len(specs) - done) * per_run
                                               / max(1, workers) / 60)})
        frame = pd.DataFrame(rows)
        frame.attrs["experiment"] = self.id
        return frame

    def run(self, *, workers: int, allow_code_change: bool = False) -> None:
        self._check_or_freeze(allow_code_change)
        split = frozen_split()
        for scenario in self.scenarios:
            if self.record["status"].get(scenario) == "completed":
                continue
            self.record["status"][scenario] = "running"
            self._write()
            batch.run_batch(self.specs(scenario), out=self.path / scenario, workers=workers,
                            keep_decisions=self.keep, split=split)
            self.record["status"][scenario] = "completed"
            self._write()

    def summaries(self) -> pd.DataFrame:
        frames = [pd.read_parquet(p) for s in self.scenarios
                  if (p := self.path / s / "summaries.parquet").exists()]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --------------------------------------------------------------------------- comparison


def v2_summaries(candidates: dict[str, Candidate],
                 experiment: str = V2_EXPERIMENT) -> pd.DataFrame:
    """v2 rows of the candidates: stage A (friction) and stage C (favorable). Read-only."""
    ids = {c.config_id for c in candidates.values()}
    frames = []
    for stage in ("A", "C"):
        path = v2_experiment_path(experiment) / f"stage_{stage}" / "summaries.parquet"
        if path.exists():
            frame = pd.read_parquet(path)
            frames.append(frame[frame["config_hash"].isin(ids)])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _names(candidates: dict[str, Candidate]) -> dict[str, str]:
    return {c.config_id: name for name, c in candidates.items()}


def totals(frame: pd.DataFrame, candidates: dict[str, Candidate]) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    df = frame.copy()
    df["candidate"] = df["config_hash"].map(_names(candidates))
    pnl = pd.to_numeric(df["realized_pnl"], errors="coerce")
    df["win"] = df["filled"].astype(bool) & (pnl > 0)
    df["loss"] = df["filled"].astype(bool) & (pnl <= 0)
    out = df.groupby(["candidate", "scenario_name"]).agg(
        windows=("market_id", "nunique"),
        entries=("entered", "sum"),
        fills=("filled", "sum"),
        wins=("win", "sum"),
        losses=("loss", "sum"),
        debit=("debit", lambda s: pd.to_numeric(s, errors="coerce").sum()),
        net_pnl=("realized_pnl", lambda s: pd.to_numeric(s, errors="coerce").sum()),
    )
    out["win_rate"] = out["wins"] / out["fills"].where(out["fills"] > 0)
    out["avg_debit_per_fill"] = out["debit"] / out["fills"].where(out["fills"] > 0)
    return out


def compare_v2_v3(v2: pd.DataFrame, v3: pd.DataFrame,
                  candidates: dict[str, Candidate]) -> pd.DataFrame:
    a, b = totals(v2, candidates), totals(v3, candidates)
    if a.empty or b.empty:
        return pd.DataFrame()
    joined = a.join(b, lsuffix="_v2", rsuffix="_v3", how="outer")
    for col in ("fills", "wins", "debit", "net_pnl", "win_rate"):
        joined[f"{col}_change"] = joined[f"{col}_v3"] - joined[f"{col}_v2"]
    order = [f"{c}_{v}" for c in ("windows", "entries", "fills", "wins", "losses", "win_rate",
                                   "debit", "avg_debit_per_fill", "net_pnl") for v in ("v2", "v3")]
    return joined[order + [c for c in joined.columns if c.endswith("_change")]]


def changed_windows(v2: pd.DataFrame, v3: pd.DataFrame,
                    candidates: dict[str, Candidate]) -> pd.DataFrame:
    """One row per (candidate, scenario, window) whose fill, side or P&L changed."""
    keys = ["config_hash", "scenario_name", "market_id"]
    cols = ["slug", "entered", "filled", "side", "debit", "realized_pnl"]
    merged = v2[keys + cols].merge(v3[keys + cols + ["entry_attempts", "entry_not_sent",
                                                     "entry_zero_fill", "spend_amount"]],
                                   on=keys, how="outer", suffixes=("_v2", "_v3"))
    p2 = pd.to_numeric(merged["realized_pnl_v2"], errors="coerce").fillna(0)
    p3 = pd.to_numeric(merged["realized_pnl_v3"], errors="coerce").fillna(0)
    changed = ((merged["filled_v2"] != merged["filled_v3"])
               | (merged["side_v2"].fillna("") != merged["side_v3"].fillna(""))
               | ((p3 - p2).abs() > 1e-9))
    out = merged[changed].copy()
    out["candidate"] = out["config_hash"].map(_names(candidates))
    out["pnl_change"] = (p3 - p2)[changed]

    def reason(row) -> str:
        if bool(row["filled_v2"]) and not bool(row["filled_v3"]):
            if (row.get("entry_not_sent") or 0) > 0:
                return "refused by the pre-send gate"
            return "no fill under the amount order"
        if not bool(row["filled_v2"]) and bool(row["filled_v3"]):
            return "filled only under v3"
        if row["side_v2"] != row["side_v3"]:
            return "different side"
        return "smaller spend (fee reserve, cap)"

    out["reason"] = out.apply(reason, axis=1) if len(out) else []
    return out[["candidate", "scenario_name", "slug_v2", "filled_v2", "filled_v3", "side_v2",
                "side_v3", "debit_v2", "debit_v3", "realized_pnl_v2", "realized_pnl_v3",
                "pnl_change", "reason"]].sort_values(["candidate", "scenario_name", "slug_v2"])


# --------------------------------------------------------------------------- features


def merge_parameters(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for section, values in (overrides or {}).items():
        out.setdefault(section, {}).update(values)
    return out


class FeatureRun(_SavedRun):
    """One optional-feature variant of one candidate on the frozen development windows."""

    def __init__(self, name: str, candidate: Candidate, overrides: dict, *,
                 scenario: str = "friction", windows: pd.DataFrame,
                 execution_policy: dict | None = None):
        self.name = name
        self.candidate = candidate
        self.parameters = merge_parameters(candidate.parameters, overrides)
        effective_parameters(self.parameters)  # validates (raises on invalid settings)
        self.scenario = scenario
        self.windows = windows
        self.policy = dict(execution_policy or {})
        identity = {
            "kind": "v3_feature",
            "name": name,
            "candidate": candidate.name,
            "parameters": self.parameters,
            "execution_policy": self.policy,
            "scenario": scenario,
            "windows": list(windows["market_id"]),
            "replay": replay_identity(),
        }
        fingerprint = _digest(identity)
        self.path = FEATURE_ROOT / f"{name}-{candidate.name}-{scenario}-{fingerprint[:10]}"
        self.record = {**identity, "fingerprint": fingerprint, "status": "not started"}

    def specs(self) -> list[RunSpec]:
        return _specs(self.windows, [self.parameters], self.scenario,
                      f"v3_feature:{self.path.name}", self.policy)

    def preview(self) -> dict:
        specs = self.specs()
        return {"run": self.path.name, "runs": len(specs),
                "done": _done(self.path, specs, False),
                "status": json.loads(self.record_file.read_text("utf-8"))["status"]
                if self.record_file.exists() else "not started"}

    def run(self, *, workers: int, allow_code_change: bool = False) -> pd.DataFrame:
        self._check_or_freeze(allow_code_change)
        summaries, _ = batch.run_batch(self.specs(), out=self.path, workers=workers,
                                       split=frozen_split())
        self.record["status"] = "completed"
        self._write()
        return summaries

    def summaries(self) -> pd.DataFrame:
        path = self.path / "summaries.parquet"
        return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def feature_vs_baseline(feature: pd.DataFrame, baseline: pd.DataFrame,
                        candidate: Candidate, scenario: str) -> pd.DataFrame:
    """Totals of a feature run next to the same candidate's verification run."""
    if baseline.empty and feature.empty:
        return pd.DataFrame({"status": ["no results yet: arm section 2 and this section"]})
    base = baseline if baseline.empty else baseline[
        (baseline["config_hash"] == candidate.config_id)
        & (baseline["scenario_name"] == scenario)]
    rows = []
    for name, frame in (("features off (v3 verification)", base), ("feature run", feature)):
        if frame.empty:
            continue
        pnl = pd.to_numeric(frame["realized_pnl"], errors="coerce")
        filled = frame["filled"].astype(bool)
        rows.append({
            "run": name,
            "windows": frame["market_id"].nunique(),
            "fills": int(filled.sum()),
            "wins": int((filled & (pnl > 0)).sum()),
            "net_pnl": float(pnl.sum()),
            "debit": float(pd.to_numeric(frame["debit"], errors="coerce").sum()),
            "entry_attempts": pd.to_numeric(frame.get("entry_attempts"), errors="coerce").sum(),
            "entry_posts": pd.to_numeric(frame.get("entry_posts"), errors="coerce").sum(),
            "exit_attempts": pd.to_numeric(frame.get("exit_attempts"), errors="coerce").sum(),
            "filled_episodes": pd.to_numeric(frame.get("filled_episodes"),
                                             errors="coerce").sum(),
        })
    return pd.DataFrame(rows).set_index("run") if rows else pd.DataFrame()


# --------------------------------------------------------------------------- shared account


def shared_account(candidate: Candidate, windows: pd.DataFrame, *, scenario: str = "friction",
                   limits: dict, initial_cash: str = "100", run: bool = False) -> dict:
    """Ordered replay of ``windows`` through one account (``sequence.run_sequence``).

    Sequential (one window at a time) and slow: about one replay per window. Saved as
    JSON; ``run=False`` only returns where the result would be and whether it exists.
    """
    from tyrex_pm.research.strategy_replay.sequence import SequenceLimits, run_sequence

    limit_values = {k: (Decimal(str(v)) if k in ("max_debit_per_episode", "daily_loss_limit")
                        and v is not None else v) for k, v in limits.items()}
    identity = {"kind": "v3_shared_account", "candidate": candidate.name,
                "parameters": candidate.parameters, "scenario": scenario,
                "windows": list(windows["market_id"]), "limits": limits,
                "initial_cash": initial_cash, "replay": replay_identity()}
    path = SHARED_ROOT / f"{candidate.name}-{scenario}-{_digest(identity)[:10]}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not run:
        return {"path": str(path), "status": "not run"}
    refs = [SimpleNamespace(ref=SimpleNamespace(market_id=w.market_id, slug=w.slug,
                                                window_start_ns=int(w.window_start_ns)),
                            collection=Collection(str(w.warehouse), str(w.collection)))
            for w in windows.itertuples()]

    def run_one(ref, *, strategy_config, scenario, **_kwargs):
        lab = batch._lab(ref.collection)
        try:
            return lab.run(ref.ref.market_id, parameters=candidate.parameters,
                           scenario=scenario.name.split("[")[0],
                           scenario_overrides={"initial_cash": scenario.initial_cash})
        finally:
            lab._windows.pop(ref.ref.market_id, None)

    from .experiments import Lab

    base_scenario = Lab().scenario(scenario)
    result = run_sequence(refs, strategy_config=None, scenario=base_scenario,
                          limits=SequenceLimits(**limit_values),
                          initial_cash=Decimal(initial_cash), run_one=run_one)
    result.update(identity=identity, created_utc=_now(), code=code_identity())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return result
