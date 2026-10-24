"""Explicit offline validation: eight development windows, no holdout or venue access."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from tyrex_pm.research.strategy_lab import exit_cases
from tyrex_pm.research.strategy_lab.batch import run_batch
from tyrex_pm.research.strategy_lab.experiments import REPO


def main():
    specs, exit_out, identity = exit_cases.specification()
    legacy = [replace(spec, execution_policy={k: v for k, v in spec.execution_policy.items()
                                               if k != "book_reconstruction"},
                      tag=spec.tag.replace("--E0_hold", "--old_book_hold"))
              for spec in specs if spec.tag.endswith("--E0_hold")]
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    key = hashlib.sha256(json.dumps([identity, script_hash], sort_keys=True,
                                    default=str).encode()).hexdigest()[:12]
    root = REPO / "var/research/trading_integrity" / f"focused-{key}"
    print(json.dumps({"output": str(root), "exit_dataset": str(exit_out),
                      "runs": len(specs) + len(legacy), "windows": identity["windows"]}),
          flush=True)
    summaries, decisions = run_batch([*specs, *legacy], out=root, workers=2, keep_decisions=True)
    exit_out.mkdir(parents=True, exist_ok=True)
    for name in ("summaries.parquet", "decisions.parquet", "explanations.parquet"):
        frame = pd.read_parquet(root / name)
        frame = frame[~frame["tag"].str.endswith("--old_book_hold")]
        frame.to_parquet(exit_out / name, index=False)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["selection"] = "E0_hold, E1_value, E2_value_trend only; old-book regression excluded"
    (exit_out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), "utf-8")
    (exit_out / "experiment.json").write_text(json.dumps(
        {**identity, "status": "completed", "validation_source": str(root)},
        indent=2, default=str), "utf-8")

    frame = summaries.copy()
    frame["variant"] = frame.tag.str.rsplit("--", n=1).str[-1]
    metrics = frame.groupby(["scenario_name", "variant"]).agg(
        windows=("market_id", "nunique"), fills=("filled", "sum"),
        pnl=("realized_pnl", "sum"), sold_shares=("sold_shares", "sum"))
    print(metrics.to_string(), flush=True)
    prior = pd.read_parquet(REPO / (
        "var/research/p7_qualification/p7-4863e0595c/development/friction/summaries.parquet"))
    prior = prior[prior.tag.str.endswith("--comparison--legacy_volatility")]
    old = frame[(frame.variant == "old_book_hold") & (frame.scenario_name == "friction")]
    joined = old.merge(prior, on="market_id", suffixes=("_now", "_p7"))
    same = {}
    for field in ("entered", "filled", "side", "realized_pnl", "debit", "vwap"):
        a, b = joined[field + "_now"], joined[field + "_p7"]
        same[field] = bool(((a.isna() & b.isna()) | (a == b)).all())
    report = {"root": str(root), "exit_dataset": str(exit_out),
              "case_purposes": identity["case_purposes"], "runs": len(frame),
              "scenarios": identity["scenarios"], "script_sha256": script_hash,
              "metrics": metrics.reset_index().to_dict("records"),
              "legacy_regression_windows": len(joined), "legacy_matches_saved_p7": same,
              "limitations": ["Selected development cases; not qualification or tuning.",
                              "Numeric exit settings are provisional, unchanged after this run.",
                              "No untouched evaluation data or real money used."]}
    (root / "review.json").write_text(json.dumps(report, indent=2, default=str), "utf-8")
    print(json.dumps({"review": str(root / "review.json"), "legacy_matches": same}), flush=True)


if __name__ == "__main__":
    main()
