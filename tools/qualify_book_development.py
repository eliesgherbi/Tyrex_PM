"""Paired book-policy diagnostic on frozen development candidates; no holdout/grid."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from tyrex_pm.research.strategy_lab.batch import Collection, RunSpec, run_batch
from tyrex_pm.research.strategy_lab.experiments import REPO, merge
from tyrex_pm.research.strategy_lab.staged import code_identity
from tyrex_pm.research.strategy_lab.v3 import frozen_windows, load_candidates

PRIOR = REPO / "var/research/p7_qualification/p7-4863e0595c/development"
POLICY = {"execution_contract": "replay_execution_contract_v2",
          "reference_availability": "reference_availability_v1",
          "book_reconstruction": "venue_top_v1"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=("comparison", "default", "both"),
                        default="comparison")
    args = parser.parse_args()
    windows, candidates = frozen_windows(), load_candidates()
    if args.candidate != "both":
        candidates = {args.candidate: candidates[args.candidate]}
    identity = {"kind": "book_development_comparison", "code": code_identity(),
                "policy": POLICY, "prior": str(PRIOR), "windows": windows.slug.tolist(),
                "candidates": {k: v.parameters for k, v in candidates.items()},
                "scenarios": ["friction", "favorable"],
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    root = REPO / "var/research/trading_integrity" / f"book-development-{digest[:12]}"
    root.mkdir(parents=True, exist_ok=True)
    (root / "experiment.json").write_text(json.dumps(identity, indent=2, default=str), "utf-8")
    specs = [RunSpec(
        collection=Collection(str(w.warehouse), str(w.collection)), market_id=str(w.market_id),
        parameters=merge(c.parameters, {"volatility": {"variance_time_policy": "per_sample_v0"}}),
        scenario=scenario, tag=f"{w.slug}--{name}--venue_top_v1", execution_policy=POLICY)
        for w in windows.itertuples() for name, c in candidates.items()
        for scenario in identity["scenarios"]]
    print(json.dumps({"root": str(root), "windows": len(windows), "runs": len(specs)}), flush=True)
    current, _ = run_batch(specs, out=root, workers=2, keep_decisions=True)
    metrics, changes = [], []
    for scenario in identity["scenarios"]:
        old = pd.read_parquet(PRIOR / scenario / "summaries.parquet")
        old = old[old.tag.str.endswith("--legacy_volatility")].copy()
        old["candidate"] = old.tag.str.split("--").str[-2]
        new = current[current.scenario_name.eq(scenario)].copy()
        new["candidate"] = new.tag.str.split("--").str[-2]
        paired = old.merge(new, on=["market_id", "candidate"], suffixes=("_old", "_new"),
                           validate="one_to_one")
        if len(paired) != len(windows) * len(candidates):
            raise ValueError("not every frozen development run has an old/new pair")
        for name in candidates:
            pairs = paired[paired.candidate.eq(name)]
            metrics.append({"candidate": name, "scenario": scenario, "windows": len(pairs),
                            **{f"{key}_{side}": float(pairs[f"{key}_{side}"].sum())
                               for key in ("filled", "realized_pnl", "debit")
                               for side in ("old", "new")}})
        for row in paired.to_dict("records"):
            differences = {}
            for key in ("entered", "filled", "side", "realized_pnl", "debit", "vwap"):
                a, b = row[key + "_old"], row[key + "_new"]
                if not ((pd.isna(a) and pd.isna(b)) or a == b):
                    differences[key] = {"old": a, "new": b}
            if differences:
                changes.append({"market_id": row["market_id"], "candidate": row["candidate"],
                                "scenario": scenario, "differences": differences,
                                "old_entry_reasons": row.get("entry_reason_counts_old"),
                                "new_entry_reasons": row.get("entry_reason_counts_new")})
    report = {"identity": identity, "runs": len(current), "metrics": metrics,
              "changed_runs": changes,
              "limits": ["Development diagnostic only; not fresh evaluation or profit proof.",
                         "Selected frozen candidate(s), unchanged volatility/entry/hold rules.",
                         "Historical counterpart is saved P7; focused legacy regression passed."]}
    (root / "comparison.json").write_text(json.dumps(report, indent=2, default=str), "utf-8")
    print(json.dumps({"report": str(root / "comparison.json"), "changed_runs": len(changes),
                      "metrics": metrics}, default=str), flush=True)


if __name__ == "__main__":
    main()
