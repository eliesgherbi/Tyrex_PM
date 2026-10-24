"""Audit completed paired book runs, including entry timing and waiting reasons."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import pandas as pd

TRADE_FIELDS = (
    "winner", "label_type", "entered", "filled", "side", "entry_tau_s",
    "p_conservative", "book_implied_up_at_entry", "limit_price", "vwap", "debit",
    "fill_ratio", "ev_conservative", "ev_point", "realized_pnl", "entry_attempts",
    "entry_not_sent", "entry_zero_fill", "entry_posts", "exit_attempts",
    "filled_episodes", "sold_shares", "exit_proceeds_net", "final_net_shares",
    "spend_amount", "orders",
)
DECISION_TOLERANCES = {
    "p_up_point": 1e-9, "p_up_low": 1e-9, "p_up_high": 1e-9,
    "sigma": 1e-12, "sealed_k": 1e-9,
    "up_bid": 0, "up_ask": 0, "down_bid": 0, "down_ask": 0,
}


def same(a, b):
    return (pd.isna(a) and pd.isna(b)) or a == b


def counts(frame):
    result = Counter()
    for raw in frame.entry_reason_counts:
        result.update(json.loads(raw))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root
    identity = json.loads((root / "experiment.json").read_text())
    if set(identity["candidates"]) != {"comparison"}:
        raise ValueError("this audit is scoped to the frozen comparison candidate")
    new_summary = pd.read_parquet(root / "summaries.parquet")
    new_decisions = pd.read_parquet(root / "decisions.parquet")
    reports = []
    for scenario in identity["scenarios"]:
        prior = Path(identity["prior"]) / scenario
        old = pd.read_parquet(prior / "summaries.parquet")
        old = old[old.tag.str.endswith("--comparison--legacy_volatility")]
        new = new_summary[new_summary.scenario_name.eq(scenario)]
        paired = old.merge(new, on="market_id", suffixes=("_old", "_new"),
                           validate="one_to_one")
        if len(paired) != len(identity["windows"]) or len(old) != len(new):
            raise ValueError("incomplete summary pairing")
        trade_changes = []
        for row in paired.to_dict("records"):
            differences = {key: {"old": row[key + "_old"], "new": row[key + "_new"]}
                           for key in TRADE_FIELDS
                           if not same(row[key + "_old"], row[key + "_new"])}
            if differences:
                trade_changes.append({"market_id": row["market_id"], "fields": differences})
        od = pd.read_parquet(prior / "decisions.parquet")
        od = od[od.tag.str.endswith("--comparison--legacy_volatility")]
        nd = new_decisions[new_decisions.scenario_name.eq(scenario)]
        decisions = od.merge(nd, on=["market_id", "at_ns"], how="outer",
                             suffixes=("_old", "_new"), indicator=True,
                             validate="one_to_one")
        aligned = decisions[decisions._merge.eq("both")]
        categorical = {}
        for key in ("action", "reason", "chosen_side"):
            different = (aligned[key + "_old"].fillna("__ABSENT__")
                         != aligned[key + "_new"].fillna("__ABSENT__"))
            categorical[key] = int(different.sum())
        numeric = {}
        for key, tolerance in DECISION_TOLERANCES.items():
            a = pd.to_numeric(aligned[key + "_old"])
            b = pd.to_numeric(aligned[key + "_new"])
            delta = (a - b).abs()
            numeric[key] = {"tolerance": tolerance,
                            "missingness_differences": int((a.isna() != b.isna()).sum()),
                            "over_tolerance": int((delta > tolerance).sum()),
                            "max_absolute_difference": None if delta.dropna().empty
                            else float(delta.max()),
                            "changed_value_examples": aligned.loc[delta > tolerance, [
                                "market_id", "at_ns", "action_old", "action_new",
                                "reason_old", "reason_new", key + "_old", key + "_new",
                            ]].head(5).to_dict("records")}
        oc, nc = counts(old), counts(new)
        changed_reasons = aligned[
            aligned.reason_old.fillna("") != aligned.reason_new.fillna("")]
        transitions = changed_reasons.groupby(["reason_old", "reason_new"]).size()
        reports.append({
            "scenario": scenario, "paired_windows": len(paired),
            "trade_fields": TRADE_FIELDS, "trade_summary_changes": trade_changes,
            "decision_rows": {"old": len(od), "new": len(nd), "aligned": len(aligned),
                              "missing": int(decisions._merge.eq("left_only").sum()),
                              "extra": int(decisions._merge.eq("right_only").sum())},
            "categorical_changes": categorical, "numeric_changes": numeric,
            "reason_transitions": [{"old": a, "new": b, "count": int(n)}
                                   for (a, b), n in transitions.items()],
            "reason_counts": {"old": dict(oc), "new": dict(nc)},
            "reason_count_changes": {key: nc[key] - oc[key] for key in sorted(oc.keys() | nc.keys())
                                     if nc[key] != oc[key]},
        })
    report = {"experiment": str(root), "scenarios": reports,
              "all_trade_summary_fields_equal": all(
                  not r["trade_summary_changes"] for r in reports),
              "limits": "Development diagnostic only. Book readiness may legitimately change. "
                        "These comparisons do not qualify latency or the exit policy."}
    destination = root / "readiness_review.json"
    destination.write_text(json.dumps(report, indent=2, default=str), "utf-8")
    print(json.dumps({"report": str(destination),
                      "all_trade_summary_fields_equal": report["all_trade_summary_fields_equal"],
                      "scenarios": [{key: r[key] for key in (
                          "scenario", "paired_windows", "decision_rows", "categorical_changes",
                          "reason_count_changes")} for r in reports]}, indent=2))


if __name__ == "__main__":
    main()
