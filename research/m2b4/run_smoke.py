"""Run M2B.4 smoke pipeline: artifacts + notebook decision outputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from research.lib.buckets import format_decision_output
from research.lib.latency import build_latency_prior, write_latency_prior
from research.lib.loaders import load_day
from research.lib.markets import build_clean_markets, notebook_01_decisions, ws_seq_gap_audit, write_clean_markets
from research.m2b4.exploratory import (
    run_notebook_01_exploratory,
    run_notebook_02_exploratory,
    run_notebook_03_exploratory,
    run_notebook_04_exploratory,
    run_notebook_05_exploratory,
    run_notebook_06_exploratory,
)
from research.lib.exploratory import write_exploratory_trace
from research.m2b4.pipeline import (
    run_notebook_02,
    run_notebook_03,
    run_notebook_04,
    run_notebook_05,
    run_notebook_06,
    write_decision_memo,
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parents[2], here.parents[1], Path.cwd()]:
        if (parent / "var" / "parquet").is_dir() or (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def main() -> int:
    repo = _repo_root()
    parquet_root = repo / "var" / "parquet"
    partition = parquet_root / "date=2026-07-05"
    if not partition.is_dir():
        partition = parquet_root / "date=golden_day"
    if not partition.is_dir():
        print("No parquet partition found", file=sys.stderr)
        return 1

    out_dir = repo / "research" / "output" / "m2b4"
    out_dir.mkdir(parents=True, exist_ok=True)

    day = load_day(partition, load_books=True)
    clean = build_clean_markets(day)
    clean_path = write_clean_markets(clean, out_dir / "clean_markets.csv")
    gap_audit = ws_seq_gap_audit(day.tables.get("lifecycle_events", clean.iloc[0:0]))
    nb01 = {"decisions": notebook_01_decisions(day, clean, gap_audit)}
    write_decision_memo("01_coverage_quality", nb01, out_dir)

    plot_dir = out_dir / "plots"
    exp01 = run_notebook_01_exploratory(day, clean, gap_audit)
    write_exploratory_trace("01", exp01, out_dir)

    prior = build_latency_prior(default_roots=[repo / "var" / "runs", repo / "var" / "live_runs"])
    prior_path = write_latency_prior(prior, out_dir / "latency_prior.json")

    nb02 = run_notebook_02(day, clean_path, prior_path)
    nb03 = run_notebook_03(day, clean_path)
    nb04 = run_notebook_04(day, clean_path)
    nb05 = run_notebook_05(day, clean_path)
    nb06 = run_notebook_06(nb01, nb02, nb03, nb04, nb05)

    exp02 = run_notebook_02_exploratory(day, clean_path, prior_path, plot_dir=plot_dir)
    exp03 = run_notebook_03_exploratory(nb03, day, clean_path)
    exp04 = run_notebook_04_exploratory(day, clean_path, plot_dir=plot_dir)
    exp05 = run_notebook_05_exploratory(day, clean_path, plot_dir=plot_dir)
    exp06 = run_notebook_06_exploratory(exp01, exp02, exp03, exp04, exp05)
    for tag, exp in [
        ("01", exp01),
        ("02", exp02),
        ("03", exp03),
        ("04", exp04),
        ("05", exp05),
        ("06", exp06),
    ]:
        write_exploratory_trace(tag, exp, out_dir)

    for name, result in [
        ("02_jump_protection_distances", nb02),
        ("03_survivor_reachability", nb03),
        ("04_depth_slippage_vacuum", nb04),
        ("05_pm_btc_chainlink_leadlag", nb05),
        ("06_prereplay_counterfactual", nb06),
    ]:
        write_decision_memo(name, result, out_dir)
        md = format_decision_output(
            decisions=result["decisions"],
            confidence=result.get("confidence", "low"),
            total_sample_size=int(result.get("total_sample_size") or 0),
            per_bucket_samples=result.get("per_bucket_samples"),
            provisional=bool(result.get("provisional", True)),
            provisional_reason="sample below stability threshold; M2B.1-B long-run gate pending",
            follow_up="Validate via M2B.5 replay on larger accepted recorder artifact",
            extra={k: v for k, v in result.get("decisions", {}).items() if k.startswith("insufficient")},
        )
        (out_dir / f"{name}_DECISION_OUTPUT.md").write_text(md, encoding="utf-8")

    summary = {
        "partition": str(partition),
        "clean_markets": str(clean_path),
        "latency_prior": str(prior_path),
        "markets_included": int(clean["include_for_analysis"].sum()),
        "exploratory_traces": [f"{i:02d}_exploratory_trace.json" for i in range(1, 7)],
    }
    (out_dir / "smoke_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
