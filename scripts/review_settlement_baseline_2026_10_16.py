"""Describe the saved v2 baseline; never replay windows or read holdout results."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
EXPERIMENT = REPO / "var/research/strategy_runs/staged/full_development-7f132d3ee2"
OUT = EXPERIMENT / "baseline_review"
RAW_LEADER = "23ca8d880ec1345c0aa13831d50823affe3253de8ca6b29379f7835ff4143975"
ROBUST_ALTERNATIVE = "3b6dbbf4edbdd6d3ac771ecb964342286c391b6fa30bb6e3822b6e54d8805618"


def metrics(group: pd.DataFrame) -> dict:
    group = group.sort_values("window_start_ns")
    pnl = pd.to_numeric(group["realized_pnl"]).fillna(0).to_numpy()
    debit = float(group["debit"].sum())
    filled = group[group["filled"]]
    cumulative = np.r_[0.0, pnl.cumsum()]
    rng = np.random.default_rng(20261016)
    boot = rng.choice(pnl, size=(10000, len(pnl)), replace=True).sum(axis=1)
    return {
        "windows": len(group),
        "requests": int(group["entered"].sum()),
        "fills": len(filled),
        "zero_fill_requests": int((group["entered"] & ~group["filled"]).sum()),
        "wins": int((filled["realized_pnl"] > 0).sum()),
        "losses": int((filled["realized_pnl"] < 0).sum()),
        "net_pnl": float(pnl.sum()),
        "total_debit": debit,
        "pnl_per_debit": float(pnl.sum() / debit) if debit else None,
        "maximum_drawdown": float((np.maximum.accumulate(cumulative) - cumulative).max()),
        "largest_win": float(pnl.max()),
        "largest_loss": float(pnl.min()),
        "net_without_best_window": float(pnl.sum() - pnl.max()),
        "top_three_window_pnl": float(np.sort(pnl)[-3:].sum()),
        "ci95_iid_window_bootstrap": np.quantile(boot, [0.025, 0.975]).tolist(),
        "maximum_actual_debit": float(group["debit"].max()),
        "runs_spending_above_5": int((group["debit"] > 5.000001).sum()),
        "limitations": group["limitations"].value_counts().to_dict(),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    record = json.loads((EXPERIMENT / "experiment.json").read_text())
    stage_records = {}
    frames = []
    labels = {}
    for stage in ("A", "B", "C"):
        stage_path = EXPERIMENT / f"stage_{stage}" / "stage.json"
        if not stage_path.exists():
            continue
        saved = json.loads(stage_path.read_text())
        stage_records[stage] = {
            k: saved.get(k) for k in ("status", "scenario", "keep_timelines", "code")
        }
        labels.update({c["config_id"]: c["label"] for c in saved["configurations"]})
        summary_path = stage_path.parent / "summaries.parquet"
        if saved["status"] == "completed" and summary_path.exists():
            frame = pd.read_parquet(summary_path)
            frame["stage"] = stage
            frames.append(frame)
    runs = pd.concat(frames, ignore_index=True)
    runs["pnl"] = pd.to_numeric(runs["realized_pnl"]).fillna(0)
    runs["utc_day"] = pd.to_datetime(runs["window_start_ns"], unit="ns", utc=True).dt.strftime(
        "%Y-%m-%d"
    )
    comparisons = []
    for (stage, config, scenario), group in runs.groupby(["stage", "config_hash", "scenario_name"]):
        row = {"stage": stage, "config_id": config, "scenario": scenario, "label": labels[config]}
        row.update(metrics(group))
        row["by_collection"] = [
            {"collection": col, "utc_days": sorted(part["utc_day"].unique()), **metrics(part)}
            for col, part in group.groupby("collection")
        ]
        comparisons.append(row)
    a = runs[runs["stage"] == "A"]
    paired = a[a["config_hash"].isin((RAW_LEADER, ROBUST_ALTERNATIVE))].pivot(
        index="market_id", columns="config_hash", values="pnl"
    )
    delta = (paired[RAW_LEADER] - paired[ROBUST_ALTERNATIVE]).to_numpy()
    rng = np.random.default_rng(20261016)
    delta_boot = rng.choice(delta, size=(10000, len(delta)), replace=True).sum(axis=1)
    timeline = pd.read_parquet(
        EXPERIMENT / "stage_A/decisions.parquet",
        columns=["market_id", "config", "at_ns", "window_end_ns", "action", "reason"],
    )
    timeline = timeline[
        (timeline["at_ns"] >= timeline["window_end_ns"] - 300_000_000_000)
        & (timeline["at_ns"] < timeline["window_end_ns"])
    ].copy()
    timeline["second"] = timeline["at_ns"] // 1_000_000_000
    timeline = timeline.drop_duplicates(["market_id", "config", "second"], keep="last")
    skipped = timeline[timeline["action"] == "HOST_SKIPPED"]
    report = {
        "experiment": record["experiment_id"],
        "replay": record["replay"],
        "development_windows": len(record["windows"]),
        "stages": stage_records,
        "comparisons": comparisons,
        "paired_A_raw_leader_minus_alternative": {
            "net_difference": float(delta.sum()),
            "ci95_iid_paired_window_bootstrap": np.quantile(delta_boot, [0.025, 0.975]).tolist(),
        },
        "A_quality": {
            "in_window_config_seconds": len(timeline),
            "host_skipped_config_seconds": len(skipped),
            "host_skipped_fraction": len(skipped) / len(timeline),
            "host_skipped_reasons": skipped["reason"].value_counts().to_dict(),
        },
        "caveats": [
            "Development selection only; holdout not read or evaluated.",
            "Window bootstrap assumes independence; ignores serial dependence and grid selection.",
            "Only three recording dates/collections: intervals do not establish generalization.",
            "Proxy outcomes and provisional opening K; uncalibrated execution/fee assumptions.",
            "All A fills carry gap_with_exposure and order_constraints_unqualified flags.",
            "V2 share-based execution can spend more than the configured 5 USDC target.",
            "Favorable is a sensitivity scenario, not a profitability bound or holdout test.",
        ],
        "stage_A_summary_sha256": hashlib.sha256(
            (EXPERIMENT / "stage_A/summaries.parquet").read_bytes()
        ).hexdigest(),
    }
    (OUT / "analysis.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    runs.drop(columns=["config", "execution_events", "entry_reason_counts"]).to_csv(
        OUT / "window_comparisons.csv", index=False
    )
    print(
        json.dumps(
            {
                "stages": {s: r["status"] for s, r in stage_records.items()},
                "comparisons": [
                    {k: r[k] for k in ("stage", "label", "scenario", "fills", "net_pnl")}
                    for r in comparisons
                ],
                "paired": report["paired_A_raw_leader_minus_alternative"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
