"""Development case runs on the production path (integration plan P6).

Notebook 06 compared variance time policies and a disagreement veto through study
hooks (``ProbabilityStudy`` / ``StudyIndicatorGraph`` / the ``research_study`` hook in
``run_window``). Both are now strategy configuration:

* ``volatility.variance_time_policy``: ``per_sample_v0`` | ``elapsed_variance_v1``;
* ``disagreement_veto``: point probability vs the UP two-sided midpoint.

A variant is therefore a plain parameter overlay. Case runs go through
``batch.run_batch`` (or ``run_window`` for verification) like every other experiment,
and indicator state comes from the shared recording framework (``recording: explain``),
not from a study trace.

``migration_check`` re-runs the saved notebook 06 bundles on this path and compares
normalized behaviour (amendment 1):

* evaluation instants and the per-evaluation decision class (a study veto,
  ``BLOCKED/ENTRY_NOT_ALLOWED`` with ``study_veto``, and a strategy veto,
  ``WAIT/DISAGREEMENT_*``, are both class ``VETO``);
* entries (time, side);
* simulated fills (time, instrument, quantity, price, fee);
* economics.

These are compared separately from identity metadata (code fingerprint, config hash,
policy names), which is expected to differ. The check runs under the old execution
contract (v1) first; v2 is measured separately (amendment 2).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from .experiments import REPO, merge

CASE_RUNS = REPO / "var/research/case_runs"
LEGACY_CASES = REPO / "var/research/settlement_extreme_cases"
LEGACY_BUNDLE = LEGACY_CASES / "development_cases_v1-51563af1bc8c"

VETO_CLASS = "VETO"
STRATEGY_VETO_REASONS = frozenset({"DISAGREEMENT_VETO", "DISAGREEMENT_INPUT_INVALID"})
ECONOMICS = ("entered", "filled", "side", "debit", "vwap", "realized_pnl", "entry_attempts",
             "entry_posts", "entry_not_sent", "final_net_shares")


def variant_overlays(threshold_pp: float = 9.0) -> dict[str, dict]:
    """The notebook 06 hypotheses as strategy parameter overlays (not a sweep)."""
    elapsed = {"volatility": {"variance_time_policy": "elapsed_variance_v1"}}
    return {
        "unchanged": {},
        "time_corrected": elapsed,
        "time_corrected_gap_veto": merge(elapsed, {"disagreement_veto": {
            "enabled": True, "max_gap_pp": threshold_pp}}),
    }


def execution_policy(contract: str = "replay_execution_contract_v1") -> dict:
    return {"reference_availability": "reference_availability_v1",
            "execution_contract": contract}


def case_specs(window_ids: Iterable[str], *, candidates: Mapping[str, Any] | None = None,
               variants: Mapping[str, dict] | None = None, scenario: str = "friction",
               contract: str = "replay_execution_contract_v1",
               recording: Mapping[str, Any] | None = None) -> list:
    """``RunSpec``s for ``batch.run_batch``: every case x candidate x variant."""
    from .batch import Collection, RunSpec
    from .v3 import frozen_windows, load_candidates

    windows = frozen_windows().set_index("slug")
    candidates = candidates or load_candidates()
    variants = variants or variant_overlays()
    specs = []
    for slug in window_ids:
        w = windows.loc[slug]
        for cname, candidate in candidates.items():
            for vname, overlay in variants.items():
                specs.append(RunSpec(
                    collection=Collection(str(w.warehouse), str(w.collection)),
                    market_id=str(w.market_id),
                    parameters=merge(candidate.parameters, overlay),
                    scenario=scenario, tag=f"{slug}--{cname}--{vname}",
                    execution_policy=execution_policy(contract),
                    recording=dict(recording or {}),
                ))
    return specs


def run_case_set(window_ids: Iterable[str], *, out: Path, workers: int | None = None,
                 recording: Mapping[str, Any] | None = None, **kwargs: Any):
    """Explicit launch: summaries, decisions and (with ``explain``) explanations."""
    from .batch import run_batch

    specs = case_specs(window_ids, recording=recording or {"level": "explain",
                                                          "explain_trigger": "entry_band"},
                       **kwargs)
    return run_batch(specs, out=out, workers=workers, keep_decisions=True)


# -- normalized behaviour --------------------------------------------------------------

def decision_class(row: Mapping[str, Any]) -> str:
    if row.get("study_veto") and row.get("action") == "BLOCKED":
        return VETO_CLASS
    if row.get("reason") in STRATEGY_VETO_REASONS:
        return VETO_CLASS
    return str(row.get("action"))


def normalized(result: Mapping[str, Any]) -> dict:
    from .frames import summary

    decisions = result.get("decisions") or []
    fills = [
        (e.get("at_ns"), e.get("instrument_id"), str(e.get("quantity")), str(e.get("price")),
         str(e.get("fee")))
        for e in result["engine"]["ledger"] if e.get("event") == "fill"
    ]
    econ = summary(dict(result))
    return {
        "evaluations": [d.get("at_ns") for d in decisions],
        "classes": [decision_class(d) for d in decisions],
        "reasons": [(d.get("action"), d.get("reason")) for d in decisions],
        "entries": [(d.get("at_ns"), d.get("chosen_side")) for d in decisions
                    if d.get("action") == "ENTER"],
        "fills": fills,
        "economics": {k: econ.get(k) for k in ECONOMICS},
    }


def _same_number(a: Any, b: Any) -> bool:
    if a == b:
        return True
    try:
        return abs(float(a) - float(b)) <= 1e-9
    except (TypeError, ValueError):
        return False


def compare(old: dict, new: dict) -> dict:
    """Behaviour comparison; reason renames are reported, not failures."""
    same_instants = old["evaluations"] == new["evaluations"]
    class_diffs = [] if not same_instants else [
        {"at_ns": at, "old": a, "new": b}
        for at, a, b in zip(old["evaluations"], old["classes"], new["classes"]) if a != b]
    reason_changes: dict[str, int] = {}
    if same_instants:
        for a, b in zip(old["reasons"], new["reasons"]):
            if a != b:
                key = f"{a[0]}/{a[1]} -> {b[0]}/{b[1]}"
                reason_changes[key] = reason_changes.get(key, 0) + 1
    econ_diffs = {k: (old["economics"].get(k), new["economics"].get(k))
                  for k in ECONOMICS
                  if not _same_number(old["economics"].get(k), new["economics"].get(k))}
    return {
        "same_evaluation_instants": same_instants,
        "evaluations": (len(old["evaluations"]), len(new["evaluations"])),
        "decision_class_mismatches": len(class_diffs) if same_instants else None,
        "first_class_mismatches": class_diffs[:5],
        "same_entries": old["entries"] == new["entries"],
        "entries": (old["entries"], new["entries"]),
        "same_fills": old["fills"] == new["fills"],
        "economics_differences": econ_diffs,
        "reason_changes": dict(sorted(reason_changes.items(), key=lambda kv: -kv[1])[:10]),
        # Entries, simulated fills and economics (what a trade-level result depends on).
        "trading_equal": bool(old["entries"] == new["entries"]
                              and old["fills"] == new["fills"] and not econ_diffs),
        # Additionally every evaluation's decision class (a veto applied at a different
        # point of the evaluation shows here even when trading is identical).
        "behaviour_equal": bool(same_instants and not class_diffs
                                and old["entries"] == new["entries"]
                                and old["fills"] == new["fills"] and not econ_diffs),
    }


def _check_window(slug: str, warehouse: str, collection: str, market_id: str,
                  bundles: list[tuple[str, str, str]], contract: str, scenario: str,
                  threshold_pp: float) -> list[dict]:
    """Worker: one window, every saved (candidate, variant) bundle for it."""
    from tyrex_pm.research.strategy_replay.run import run_window
    from tyrex_pm.strategies.settlement_value.schema import (
        settlement_value_config_from_parameters,
    )

    from .experiments import RESEARCH_BASE_PARAMETERS, Lab, replay_execution_policy
    from .v3 import load_candidates

    lab = Lab(warehouse=Path(warehouse), collection=collection)
    window = lab.window(market_id)
    clock = lab.catalog.clock_observations_for(window.range_start_ns, window.range_end_ns)
    candidates = load_candidates()
    overlays = variant_overlays(threshold_pp)
    rows = []
    for path, cname, vname in bundles:
        saved = json.loads(Path(path).read_text("utf-8"))
        old = normalized(saved)
        del saved
        params = merge(RESEARCH_BASE_PARAMETERS,
                       merge(candidates[cname].parameters, overlays[vname]))
        started = time.perf_counter()
        result = run_window(window,
                            strategy_config=settlement_value_config_from_parameters(params),
                            scenario=lab.scenario(scenario),
                            execution_policy=replay_execution_policy(execution_policy(contract)),
                            clock_observations=clock)
        new = normalized(result)
        rows.append({"window_id": slug, "candidate": cname, "variant": vname,
                     "contract": contract, "wall_s": round(time.perf_counter() - started, 1),
                     **compare(old, new)})
        del result
    return rows


def migration_check(bundle: Path = LEGACY_BUNDLE, *,
                    contract: str = "replay_execution_contract_v1",
                    workers: int | None = None, out: Path | None = None) -> pd.DataFrame:
    """Replay every saved notebook 06 bundle on the production path and compare."""
    from .v3 import frozen_windows

    identity = json.loads((bundle / "experiment.json").read_text("utf-8"))
    threshold = float(identity["variants"]["time_corrected_gap_veto"]["disagreement_pp"])
    windows = frozen_windows().set_index("slug")
    groups: dict[str, list] = {}
    for path in sorted(bundle.glob("btc-*.json")):
        slug, cname, vname = path.stem.split("--")
        groups.setdefault(slug, []).append((str(path), cname, vname))
    workers = workers or max(1, min(len(groups), (os.cpu_count() or 2) - 2, 7))
    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_check_window, slug, str(windows.loc[slug].warehouse),
                        str(windows.loc[slug].collection), str(windows.loc[slug].market_id),
                        items, contract, identity["scenario"], threshold)
            for slug, items in groups.items()
        ]
        for future in as_completed(futures):
            rows.extend(future.result())
    frame = pd.DataFrame(rows).sort_values(["window_id", "candidate", "variant"])
    frame = frame.reset_index(drop=True)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / f"migration_{contract}.json").write_text(
            json.dumps({"bundle": str(bundle), "contract": contract,
                        "rows": frame.to_dict("records")}, indent=1, default=str), "utf-8")
    return frame


# -- explorer loading ------------------------------------------------------------------

def load_case_run(path: Path) -> dict[str, pd.DataFrame]:
    """A case-run (batch) folder as frames: summaries, decisions, explanations."""
    path = Path(path)
    frames = {"summaries": pd.read_parquet(path / "summaries.parquet")}
    for name in ("decisions", "explanations"):
        file = path / f"{name}.parquet"
        frames[name] = pd.read_parquet(file) if file.exists() else pd.DataFrame()
    return frames


def load_legacy_case_bundle(path: Path = LEGACY_BUNDLE) -> dict[str, pd.DataFrame]:
    """Adapter: a saved notebook 06 study bundle in the same frame layout (read only)."""
    summaries, decisions = [], []
    for file in sorted(Path(path).glob("btc-*.json")):
        result = json.loads(file.read_text("utf-8"))
        study = result.get("case_study", {})
        tag = file.stem
        summaries.append({**result["case_summary"], "tag": tag,
                          "candidate": study.get("candidate"),
                          "variant": study.get("variant")})
        for d in result.get("decisions") or []:
            decisions.append({"tag": tag, "at_ns": d.get("at_ns"), "action": d.get("action"),
                              "reason": d.get("reason"), "tau_s": d.get("tau_s"),
                              "p_up_point": d.get("p_up_point"), "up_bid": d.get("up_bid"),
                              "up_ask": d.get("up_ask"), "sigma": d.get("sigma"),
                              "decision_class": decision_class(d)})
        del result
    return {"summaries": pd.DataFrame(summaries), "decisions": pd.DataFrame(decisions),
            "explanations": pd.DataFrame()}


def case_run_directory(window_ids: Iterable[str], **kwargs: Any) -> Path:
    """Identity-keyed folder for a case set (same specs -> same folder, resumable)."""
    import hashlib
    from dataclasses import asdict

    specs = case_specs(window_ids, **kwargs)
    payload = json.dumps([{**asdict(s), "collection": asdict(s.collection)} for s in specs],
                         sort_keys=True, default=str)
    return CASE_RUNS / f"cases-{hashlib.sha256(payload.encode()).hexdigest()[:12]}"


def latest_case_run(root: Path = CASE_RUNS) -> Path | None:
    runs = sorted((p.parent for p in Path(root).glob("cases-*/summaries.parquet")),
                  key=lambda p: (p / "summaries.parquet").stat().st_mtime)
    return runs[-1] if runs else None


def case_frames(path: Path) -> dict[str, pd.DataFrame]:
    """Case-run folder or legacy notebook 06 bundle, in one layout.

    ``summaries`` gain ``window_id``, ``candidate``, ``variant`` (from the run tag) and
    ``vetoed_evaluations``; ``decisions`` gain the same keys and ``decision_class``.
    """
    path = Path(path)
    legacy = (path / "experiment.json").exists() and not (path / "summaries.parquet").exists()
    frames = load_legacy_case_bundle(path) if legacy else load_case_run(path)
    for name in ("summaries", "decisions", "explanations"):
        frame = frames[name]
        if frame.empty or "tag" not in frame:
            continue
        parts = frame["tag"].str.split("--", expand=True)
        frame["window_id"], frame["candidate"], frame["variant"] = parts[0], parts[1], parts[2]
    decisions = frames["decisions"]
    if not decisions.empty:
        if "decision_class" not in decisions:
            decisions["decision_class"] = [
                decision_class({"action": a, "reason": r})
                for a, r in zip(decisions["action"], decisions["reason"])]
        vetoes = decisions[decisions.decision_class.eq(VETO_CLASS)].groupby("tag").size()
        frames["summaries"]["vetoed_evaluations"] = (
            frames["summaries"]["tag"].map(vetoes).fillna(0).astype(int))
    frames["source"] = pd.DataFrame([{"path": str(path),
                                      "kind": "legacy_study_bundle" if legacy else "case_run"}])
    return frames
