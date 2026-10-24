"""P7 research qualification: development comparison, freeze, one untouched evaluation.

Both frozen entry configurations (``v3.load_candidates``) are compared under the
corrected execution contract (``replay_execution_contract_v2``, live
reference-availability rule):

* ``legacy_volatility``     — ``variance_time_policy: per_sample_v0`` (original formula);
* ``corrected_volatility``  — ``variance_time_policy: elapsed_variance_v1``;
* a veto variant only when separately justified on development evidence
  (``veto_evidence``); never promoted automatically.

Order is enforced by the saved record:

1. ``run_development``: every frozen development window, every scenario.
2. ``freeze``: the policies taken to evaluation, with the rationale and an untouched
   check of the evaluation windows (no saved result on disk uses them). Written once.
3. ``run_evaluation``: exactly the frozen policies on the frozen holdout windows,
   once. Refused without a freeze, after a completed evaluation, or when the holdout
   was touched by any saved result outside this qualification.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from . import batch
from .batch import Collection, RunSpec
from .experiments import REPO, merge
from .staged import code_identity, frozen_split
from .v3 import effective_parameters, frozen_windows, load_candidates

QUALIFICATION_ROOT = REPO / "var" / "research" / "p7_qualification"
POLICIES = {
    "legacy_volatility": {"volatility": {"variance_time_policy": "per_sample_v0"}},
    "corrected_volatility": {"volatility": {"variance_time_policy": "elapsed_variance_v1"}},
}
EXECUTION = {"reference_availability": "reference_availability_v1",
             "execution_contract": "replay_execution_contract_v2"}
SCENARIOS = ("friction", "favorable")


class QualificationOrderError(RuntimeError):
    pass


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def holdout_windows() -> pd.DataFrame:
    hold = set(frozen_split()["holdout"])
    inventory = batch.window_inventory()
    frame = inventory[inventory["market_id"].isin(hold)].sort_values("window_start_ns")
    return frame.reset_index(drop=True)


def holdout_untouched(
    exclude: Path | None = None,
    *,
    research_root: Path | None = None,
    split_file: Path | None = None,
) -> dict:
    """Saved results that already use holdout windows."""
    root = research_root or (REPO / "var" / "research")
    split = frozen_split(path=split_file) if split_file else frozen_split()
    hold = set(split["holdout"])
    used: dict[str, int] = {}
    if root.exists():
        for path in root.rglob("workflow_usage.json"):
            if exclude is not None and Path(exclude) in path.parents:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                ids = set(payload.get("market_ids") or [])
            except (OSError, ValueError, TypeError):
                used[str(path)] = -1
                continue
            if ids & hold:
                used[str(path)] = len(ids & hold)
    for path in root.rglob("summaries.parquet") if root.exists() else []:
        if exclude is not None and Path(exclude) in path.parents:
            continue
        try:
            ids = set(pd.read_parquet(path, columns=["market_id"])["market_id"])
        except Exception:  # noqa: BLE001 - unreadable files are reported, not trusted
            used[str(path)] = -1
            continue
        if ids & hold:
            used[str(path)] = len(ids & hold)
    cache = root / "strategy_replay" / "timelines"
    cached = sum(1 for meta in cache.glob("*.json")
                 if json.loads(meta.read_text("utf-8")).get("run", {}).get("market_id") in hold)
    return {"untouched": not used and not cached, "summaries_using_holdout": used,
            "timeline_cache_entries": cached, "holdout_windows": len(hold),
            "checked_at": datetime.now(timezone.utc).isoformat()}


class Qualification:
    def __init__(self, *, policies: dict[str, dict] | None = None,
                 scenarios: tuple[str, ...] = SCENARIOS) -> None:
        self.candidates = load_candidates()
        self.policies = dict(policies or POLICIES)
        self.scenarios = tuple(scenarios)
        self.development = frozen_windows()
        identity = {
            "kind": "p7_qualification",
            "candidates": {k: c.parameters for k, c in self.candidates.items()},
            "policies": self.policies, "execution": EXECUTION,
            "scenarios": list(self.scenarios),
            "development": sorted(self.development["market_id"]),
            "holdout": sorted(frozen_split()["holdout"]),
        }
        self.fingerprint = _digest(identity)
        self.path = QUALIFICATION_ROOT / f"p7-{self.fingerprint[:10]}"
        self.identity = identity

    # -- record ------------------------------------------------------------------------
    @property
    def record_file(self) -> Path:
        return self.path / "qualification.json"

    def record(self) -> dict:
        if self.record_file.exists():
            return json.loads(self.record_file.read_text("utf-8"))
        return {**self.identity, "fingerprint": self.fingerprint, "stages": {}}

    def _save(self, record: dict) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.record_file.write_text(json.dumps(record, indent=1, default=str), "utf-8")

    # -- specs -------------------------------------------------------------------------
    def specs(self, windows: pd.DataFrame, scenario: str,
              policies: dict[str, dict] | None = None) -> list[RunSpec]:
        out = []
        for w in windows.itertuples():
            for cname, candidate in self.candidates.items():
                for pname, overlay in (policies or self.policies).items():
                    out.append(RunSpec(
                        collection=Collection(str(w.warehouse), str(w.collection)),
                        market_id=w.market_id,
                        parameters=effective_parameters(merge(candidate.parameters, overlay)),
                        scenario=scenario, tag=f"{w.slug}--{cname}--{pname}",
                        execution_policy=dict(EXECUTION)))
        return out

    # -- stages ------------------------------------------------------------------------
    def run_development(self, *, workers: int) -> None:
        record = self.record()
        record.setdefault("code_at_start", code_identity())
        for scenario in self.scenarios:
            stage = f"development/{scenario}"
            if record["stages"].get(stage, {}).get("status") == "completed":
                continue
            record["stages"][stage] = {"status": "running", "started": _now()}
            self._save(record)
            batch.run_batch(self.specs(self.development, scenario),
                            out=self.path / "development" / scenario, workers=workers,
                            keep_decisions=True, split=frozen_split())
            record["stages"][stage] = {"status": "completed", "finished": _now(),
                                       "runs": len(self.development) * 2 * len(self.policies)}
            self._save(record)

    def development_summaries(self) -> pd.DataFrame:
        frames = [pd.read_parquet(p) for p in
                  sorted((self.path / "development").glob("*/summaries.parquet"))]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def freeze(self, policies: list[str], rationale: str) -> dict:
        record = self.record()
        if "frozen" in record:
            raise QualificationOrderError("already frozen; the evaluation set is used once")
        if any(record["stages"].get(f"development/{s}", {}).get("status") != "completed"
               for s in self.scenarios):
            raise QualificationOrderError("freeze only after every development stage")
        unknown = set(policies) - set(self.policies)
        if unknown or not policies:
            raise ValueError(f"unknown or empty policies: {sorted(unknown)}")
        check = holdout_untouched(exclude=self.path)
        record["frozen"] = {"policies": {p: self.policies[p] for p in policies},
                            "rationale": rationale, "frozen_at": _now(),
                            "code": code_identity(), "holdout_check": check,
                            "development_digest": _digest(
                                self.development_summaries()[["tag", "scenario_name", "digest"]]
                                .sort_values(["tag", "scenario_name"]).values.tolist())}
        self._save(record)
        return record["frozen"]

    def run_evaluation(self, *, workers: int) -> None:
        record = self.record()
        frozen = record.get("frozen")
        if frozen is None:
            raise QualificationOrderError("freeze the compared policies before evaluating")
        if not frozen["holdout_check"]["untouched"]:
            raise QualificationOrderError("holdout was already used by saved results")
        now_check = holdout_untouched(exclude=self.path)
        if not now_check["untouched"]:
            raise QualificationOrderError(
                f"holdout touched after the freeze: {now_check['summaries_using_holdout']}")
        windows = holdout_windows()
        for scenario in self.scenarios:
            stage = f"evaluation/{scenario}"
            status = record["stages"].get(stage, {}).get("status")
            if status == "completed":
                continue
            record["stages"][stage] = {"status": "running", "started": _now()}
            self._save(record)
            batch.run_batch(self.specs(windows, scenario, frozen["policies"]),
                            out=self.path / "evaluation" / scenario, workers=workers,
                            keep_decisions=True, split=frozen_split())
            record["stages"][stage] = {"status": "completed", "finished": _now(),
                                       "runs": len(windows) * 2 * len(frozen["policies"])}
            self._save(record)

    def evaluation_summaries(self) -> pd.DataFrame:
        frames = [pd.read_parquet(p) for p in
                  sorted((self.path / "evaluation").glob("*/summaries.parquet"))]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- analysis --------------------------------------------------------------------------

def _tag_columns(frame: pd.DataFrame) -> pd.DataFrame:
    parts = frame["tag"].str.split("--", expand=True)
    return frame.assign(slug=parts[0], candidate=parts[1], policy=parts[2])


def compare(frame: pd.DataFrame) -> pd.DataFrame:
    """Per candidate x policy x scenario: windows, fills, wins, losses, P&L, per-fill."""
    f = _tag_columns(frame)
    f["pnl"] = pd.to_numeric(f["realized_pnl"], errors="coerce")
    f["filled"] = f["filled"].fillna(False).astype(bool)
    rows = []
    for (cand, pol, scen), g in f.groupby(["candidate", "policy", "scenario_name"]):
        fills = g[g["filled"]]
        rows.append({"candidate": cand, "policy": pol, "scenario": scen, "windows": len(g),
                     "fills": len(fills), "wins": int((fills["pnl"] > 0).sum()),
                     "losses": int((fills["pnl"] <= 0).sum()),
                     "pnl": round(float(fills["pnl"].sum()), 4),
                     "pnl_per_fill": round(float(fills["pnl"].mean()), 4) if len(fills) else None,
                     "debit": round(float(pd.to_numeric(fills["debit"]).sum()), 4)})
    return pd.DataFrame(rows)


def paired(frame: pd.DataFrame, scenario: str, a: str = "legacy_volatility",
           b: str = "corrected_volatility") -> pd.DataFrame:
    """Same window, same candidate: what changed between two policies."""
    f = _tag_columns(frame[frame["scenario_name"].eq(scenario)])
    f["pnl"] = pd.to_numeric(f["realized_pnl"], errors="coerce").fillna(0.0)
    wide = f.pivot_table(index=["slug", "candidate"], columns="policy",
                         values=["pnl", "filled", "side"], aggfunc="first")
    out = pd.DataFrame({
        "pnl_a": wide["pnl"][a], "pnl_b": wide["pnl"][b],
        "filled_a": wide["filled"][a].astype(bool), "filled_b": wide["filled"][b].astype(bool),
        "side_a": wide["side"][a], "side_b": wide["side"][b]}).reset_index()
    out["delta"] = out["pnl_b"] - out["pnl_a"]
    out["changed"] = (out["filled_a"] != out["filled_b"]) | (
        out["side_a"].fillna("") != out["side_b"].fillna("")) | (out["delta"].abs() > 1e-9)
    return out


def veto_evidence(decisions: pd.DataFrame, summaries: pd.DataFrame,
                  thresholds: tuple[float, ...] = (5.0, 9.0, 15.0, 20.0)) -> pd.DataFrame:
    """Entries by model-vs-UP-midpoint gap at the first ENTER: would a veto have removed
    mostly losers? (Development evidence only; a veto is tested only if it would.)"""
    d = decisions[decisions["action"].eq("ENTER")].copy()
    d["mid"] = (pd.to_numeric(d["up_bid"]) + pd.to_numeric(d["up_ask"])) / 2
    d["gap_pp"] = 100 * (pd.to_numeric(d["p_up_point"]) - d["mid"]).abs()
    first = d.sort_values("at_ns").groupby(["tag", "scenario_name"]).first().reset_index()
    s = summaries[["tag", "scenario_name", "filled", "realized_pnl"]].copy()
    m = first.merge(s, on=["tag", "scenario_name"], how="inner")
    m = m[m["filled"].fillna(False).astype(bool)]
    m["pnl"] = pd.to_numeric(m["realized_pnl"], errors="coerce")
    rows = []
    for t in thresholds:
        over = m[m["gap_pp"] > t]
        rows.append({"threshold_pp": t, "fills_vetoed": len(over),
                     "of_fills": len(m), "vetoed_wins": int((over["pnl"] > 0).sum()),
                     "vetoed_losses": int((over["pnl"] <= 0).sum()),
                     "vetoed_pnl": round(float(over["pnl"].sum()), 4)})
    return pd.DataFrame(rows)
