"""Generate M2B.4-B tutorial notebooks (01-06 + EX1-EX3)."""

from __future__ import annotations

import json
from pathlib import Path

BOOTSTRAP = """
import sys
from pathlib import Path

_cwd = Path.cwd().resolve()
REPO_ROOT = next(
    (p for p in [_cwd, *_cwd.parents]
     if (p / "pyproject.toml").is_file() and (p / "research" / "lib").is_dir()),
    None,
)
if REPO_ROOT is None:
    raise RuntimeError(
        "Cannot find Tyrex_PM repo root. Start Jupyter from repo root or research/notebooks/."
    )
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
""".strip()

PARTITION = 'REPO_ROOT / "var/parquet/date=2026-07-05"'
OUT = 'REPO_ROOT / "research/output/m2b4"'
PLOT = 'OUT / "plots"'


def _cell_md(text: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": [line + "\n" for line in text.strip().splitlines()]}


def _cell_code(code: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {},
        "source": [line + "\n" for line in code.strip().splitlines()],
        "outputs": [],
        "execution_count": None,
    }


def _nb(cells: list[dict]) -> dict:
    return {
        "cells": [_cell_md("# Bootstrap")] + [_cell_code(BOOTSTRAP)] + cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def nb01() -> dict:
    return _nb(
        [
            _cell_md(
                """## 1. Objective
Audit coverage, quality, and label validity. Produce `clean_markets.csv` gate for notebooks 02-06.

## 2. Strategy relevance
Informs which markets are safe for offline research and which labels (direction vs price-to-beat) are proxy-valid.

## 3. Data used
- `markets.parquet`: one row per BTC 5m market window
- `ws_quality.parquet`, `lifecycle_events`: gap/staleness diagnostics
- `price_to_beat.parquet`: settlement reference labels
Caveat: gap_rate is diagnostic only — not a live strategy feature.

## 4. Key formulas
- `gap_rate = ws_seq_gap_count / event_count` (diagnostic)
- `include_for_analysis` requires PTB present, zero corrupt/dropped, coverage recorded"""
            ),
            _cell_md("### Load partition and build clean-market gate"),
            _cell_code(
                f"""
from research.lib.loaders import load_day
from research.lib.markets import build_clean_markets, notebook_01_decisions, ws_seq_gap_audit, write_clean_markets
from research.lib.exploratory import write_exploratory_trace
from research.m2b4.exploratory import run_notebook_01_exploratory
from research.lib.buckets import format_decision_output
from research.lib.plots import try_import_matplotlib

PARTITION = {PARTITION}
OUT = {OUT}
PLOT = {PLOT}
OUT.mkdir(parents=True, exist_ok=True)
PLOT.mkdir(parents=True, exist_ok=True)
day = load_day(PARTITION, load_books=False)
clean = build_clean_markets(day)
write_clean_markets(clean, OUT / "clean_markets.csv")
gap_audit = ws_seq_gap_audit(day.tables.get("lifecycle_events"))
"""
            ),
            _cell_md(
                """### Visual exploration
Bar chart of included vs excluded markets. Do not conclude live readiness from counts alone."""
            ),
            _cell_code(
                """
plt = try_import_matplotlib()
if plt:
    import matplotlib.pyplot as plt
    inc = int(clean["include_for_analysis"].sum())
    exc = len(clean) - inc
    plt.figure(figsize=(5,3))
    plt.bar(["included","excluded"], [inc, exc])
    plt.title("Clean markets (exploratory)")
    plt.savefig(PLOT / "01_included_excluded_bar.png")
    plt.close()
    clean["gap_rate"].hist(bins=20)
    plt.title("gap_rate distribution (diagnostic only)")
    plt.savefig(PLOT / "01_gap_rate_distribution.png")
    plt.close()
    clean.groupby("ptb_present")["market_id"].count().plot(kind="bar", title="PTB presence")
    plt.savefig(PLOT / "01_ptb_final_reference.png")
    plt.close()
"""
            ),
            _cell_md("## 6. EXPLORATORY TRACE\nExploratory only — not for live YAML."),
            _cell_code(
                """
exp = run_notebook_01_exploratory(day, clean, gap_audit)
write_exploratory_trace("01", exp, OUT)
print("gap_rate_usable_as_feature (exploratory):", exp.get("gap_rate_usable_as_feature"))
"""
            ),
            _cell_md("## 7. STRICT DECISION OUTPUT"),
            _cell_code(
                """
decisions = notebook_01_decisions(day, clean, gap_audit)
print(format_decision_output(
    decisions=decisions, confidence="low", total_sample_size=len(clean),
    per_bucket_samples={{"included": int(clean["include_for_analysis"].sum())}},
    provisional=True, provisional_reason="27-market sample",
    follow_up="Notebooks 02-06 require clean_markets.csv",
))
clean.head()
"""
            ),
            _cell_md(
                """## 8. Conclusions
- Learned: most markets pass PTB gate; one pre-fix exclusion expected.
- Unsafe: using gap_rate as a strategy feature.
- Unlocks confidence: multi-day recorder + M2B.1-B gate."""
            ),
        ]
    )


def nb02() -> dict:
    return _nb(
        [
            _cell_md(
                """## 1. Objective
Study price jumps and honest protection distances; resolution-tail behavior T-60s..T+60s.

## 2. Strategy relevance
Informs `pair_stop_loss_buffer`, trail distance, survivor floor, near-close flatten knobs.

## 3. Data used
- `best_bid_ask`, `trade_prices`: jump distribution (mid/bid moves)
- `latency_prior.json`: strict gate only
Caveat: mid is a quote proxy, not guaranteed tradable price.

## 4. Key formulas
- `jump_dt = |mid(t) - mid(t - dt)|`
- `buffer_exploratory(p90, L) = p90_jump(window + assumed_latency L)`"""
            ),
            _cell_md("### Assumed-latency exploratory grid (strict gate unchanged)"),
            _cell_code(
                f"""
from research.lib.loaders import load_day
from research.lib.latency import build_latency_prior, write_latency_prior
from research.m2b4.pipeline import run_notebook_02
from research.m2b4.exploratory import run_notebook_02_exploratory
from research.lib.exploratory import write_exploratory_trace
from research.lib.buckets import format_decision_output

OUT = {OUT}
PLOT = {PLOT}
day = load_day({PARTITION}, load_books=True)
prior_path = write_latency_prior(build_latency_prior(default_roots=[REPO_ROOT / "var/runs"]), OUT / "latency_prior.json")
strict = run_notebook_02(day, OUT / "clean_markets.csv", prior_path)
exp = run_notebook_02_exploratory(day, OUT / "clean_markets.csv", prior_path, plot_dir=PLOT)
write_exploratory_trace("02", exp, OUT)
print(exp.get("assumed_latency_warning"))
"""
            ),
            _cell_md("## 7. STRICT DECISION OUTPUT"),
            _cell_code(
                """
print(format_decision_output(
    decisions=strict["decisions"], confidence=strict["confidence"],
    total_sample_size=strict["total_sample_size"],
    per_bucket_samples=strict.get("per_bucket_samples"), provisional=True,
    extra={{"insufficient_latency_prior": strict["decisions"].get("insufficient_latency_prior")}},
))
"""
            ),
            _cell_md(
                """## 8. Conclusions
- Assumed-latency buffers show sensitivity; strict layer stays insufficient without measured prior.
- Do not copy exploratory buffers into live YAML."""
            ),
        ]
    )


def nb03() -> dict:
    return _nb(
        [
            _cell_md("## 1. Objective\nSurvivor reachability proxy — recovery touch and never-armed-and-lost frequency."),
            _cell_md("## 2. Strategy relevance\nBreakeven-gated arming, reachability gating, ratchet modules."),
            _cell_code(
                f"""
from research.lib.loaders import load_day
from research.m2b4.pipeline import run_notebook_03
from research.m2b4.exploratory import run_notebook_03_exploratory
from research.lib.exploratory import write_exploratory_trace
from research.lib.buckets import format_decision_output

day = load_day({PARTITION}, load_books=True)
OUT = {OUT}
strict = run_notebook_03(day, OUT / "clean_markets.csv")
exp = run_notebook_03_exploratory(strict, day, OUT / "clean_markets.csv")
write_exploratory_trace("03", exp, OUT)
print("exploratory never_armed_and_lost_rate:", exp.get("never_armed_and_lost_proxy_rate"))
print(format_decision_output(decisions=strict["decisions"], confidence=strict["confidence"], total_sample_size=strict["total_sample_size"], provisional=True))
"""
            ),
            _cell_md("## 8. Conclusions\nProxy episodes suggest M2B.5 replay priorities; strict arming verdicts remain insufficient."),
        ]
    )


def nb04() -> dict:
    return _nb(
        [
            _cell_md("## 1. Objective\nDepth, slippage, liquidity vacuum using book_snapshots + best_bid_ask."),
            _cell_code(
                f"""
from research.lib.loaders import load_day
from research.m2b4.pipeline import run_notebook_04
from research.m2b4.exploratory import run_notebook_04_exploratory
from research.lib.exploratory import write_exploratory_trace
from research.lib.buckets import format_decision_output

day = load_day({PARTITION}, load_books=True)
OUT = {OUT}
PLOT = {PLOT}
strict = run_notebook_04(day, OUT / "clean_markets.csv")
exp = run_notebook_04_exploratory(day, OUT / "clean_markets.csv", plot_dir=PLOT)
write_exploratory_trace("04", exp, OUT)
print(exp.get("strict_no_go_explanation"))
print(format_decision_output(decisions=strict["decisions"], confidence=strict["confidence"], total_sample_size=strict["total_sample_size"], provisional=True))
"""
            ),
        ]
    )


def nb05() -> dict:
    return _nb(
        [
            _cell_md(
                """## 1. Objective\nPM / BTC / Chainlink lead-lag and market-story case studies.

Binance may reflect trader reaction. Chainlink reflects settlement/reference. Do not collapse them."""
            ),
            _cell_code(
                f"""
from research.lib.loaders import load_day
from research.m2b4.pipeline import run_notebook_05
from research.m2b4.exploratory import run_notebook_05_exploratory
from research.lib.exploratory import write_exploratory_trace
from research.lib.buckets import format_decision_output
from research.lib.plots import plot_market_story

day = load_day({PARTITION}, load_books=False)
OUT = {OUT}
PLOT = {PLOT}
strict = run_notebook_05(day, OUT / "clean_markets.csv")
exp = run_notebook_05_exploratory(day, OUT / "clean_markets.csv", plot_dir=PLOT)
write_exploratory_trace("05", exp, OUT)
for mid in exp.get("case_study_market_ids", [])[:3]:
    plot_market_story(day, mid, out_path=PLOT / f"05_story_{{mid}}.png")
    print("case study:", mid)
print(format_decision_output(decisions=strict["decisions"], confidence=strict["confidence"], total_sample_size=strict["total_sample_size"], provisional=True))
"""
            ),
        ]
    )


def nb06() -> dict:
    return _nb(
        [
            _cell_md("## 1. Objective\nPre-replay counterfactual grid assembly. **This is NOT M2B.5 replay.**"),
            _cell_code(
                f"""
from research.lib.loaders import load_day
from research.lib.latency import build_latency_prior, write_latency_prior
from research.lib.markets import build_clean_markets, notebook_01_decisions, ws_seq_gap_audit, write_clean_markets
from research.m2b4.pipeline import run_notebook_02, run_notebook_03, run_notebook_04, run_notebook_05, run_notebook_06
from research.m2b4.exploratory import (
    run_notebook_01_exploratory, run_notebook_02_exploratory, run_notebook_03_exploratory,
    run_notebook_04_exploratory, run_notebook_05_exploratory, run_notebook_06_exploratory,
)
from research.lib.exploratory import write_exploratory_trace
from research.lib.buckets import format_decision_output

OUT = {OUT}
day = load_day({PARTITION}, load_books=True)
clean = build_clean_markets(day)
gap = ws_seq_gap_audit(day.tables.get("lifecycle_events"))
nb01 = {{"decisions": notebook_01_decisions(day, clean, gap)}}
prior_path = write_latency_prior(build_latency_prior(default_roots=[REPO_ROOT / "var/runs"]), OUT / "latency_prior.json")
nb02 = run_notebook_02(day, OUT / "clean_markets.csv", prior_path)
nb03 = run_notebook_03(day, OUT / "clean_markets.csv")
nb04 = run_notebook_04(day, OUT / "clean_markets.csv")
nb05 = run_notebook_05(day, OUT / "clean_markets.csv")
strict = run_notebook_06(nb01, nb02, nb03, nb04, nb05)
exp06 = run_notebook_06_exploratory(
    run_notebook_01_exploratory(day, clean, gap),
    run_notebook_02_exploratory(day, OUT / "clean_markets.csv", prior_path),
    run_notebook_03_exploratory(nb03, day, OUT / "clean_markets.csv"),
    run_notebook_04_exploratory(day, OUT / "clean_markets.csv"),
    run_notebook_05_exploratory(day, OUT / "clean_markets.csv"),
)
write_exploratory_trace("06", exp06, OUT)
print("STRICT grid:", strict["decisions"]["m2b5_parameter_grid"])
print("EXPLORATORY grid:", exp06.get("exploratory_grid"))
"""
            ),
        ]
    )


def ex1() -> dict:
    return _nb(
        [
            _cell_md("# EX1 — Data tour\nGuided tour of Parquet tables. Exploratory only."),
            _cell_code(
                f"""
from research.lib.loaders import load_day, DAY_TABLES
from research.lib.plots import try_import_matplotlib

day = load_day({PARTITION}, load_books=True)
for name in DAY_TABLES:
    df = day.tables.get(name)
    print(name, 0 if df is None else len(df), list(df.columns)[:8] if df is not None and not df.empty else [])
print("book snapshot markets:", len(day.book_snapshots))
"""
            ),
            _cell_md("## Observations & hypotheses\nPM outcome price is not BTC price. Chainlink = reference; Binance = reaction."),
        ]
    )


def ex2() -> dict:
    return _nb(
        [
            _cell_md("# EX2 — Market stories\nLoopable `plot_market_story` viewer."),
            _cell_code(
                f"""
from research.lib.loaders import load_day
from research.lib.markets import load_clean_markets, included_market_ids
from research.lib.plots import plot_market_story

OUT = {OUT}
PLOT = {PLOT}
PLOT.mkdir(parents=True, exist_ok=True)
day = load_day({PARTITION}, load_books=False)
clean = load_clean_markets(OUT / "clean_markets.csv")
mids = included_market_ids(clean)[:5]
for mid in mids:
    plot_market_story(day, mid, out_path=PLOT / f"EX2_{{mid}}.png")
    print("plotted", mid)
"""
            ),
            _cell_md("## Observations & hypotheses\nCompare UP vs DOWN resolution markets visually; no live config recommendations."),
        ]
    )


def ex3() -> dict:
    return _nb(
        [
            _cell_md("# EX3 — Correlations\nn=26 markets — hypotheses only, not findings."),
            _cell_code(
                f"""
from research.lib.loaders import load_day
from research.lib.eda import build_market_feature_frame

OUT = {OUT}
day = load_day({PARTITION}, load_books=True)
ff = build_market_feature_frame(day, OUT / "clean_markets.csv")
print(ff.head())
num = ff.select_dtypes("number").drop(columns=["gap_rate_diagnostic_only"], errors="ignore")
if len(num.columns) > 1:
    print(num.corr(method="pearson").round(2))
"""
            ),
            _cell_md("## Observations & hypotheses\nStrong correlations on tiny n are unstable. gap_rate is diagnostic only."),
        ]
    )


NOTEBOOKS = {
    "01_coverage_quality.ipynb": nb01,
    "02_jump_protection_distances.ipynb": nb02,
    "03_survivor_reachability.ipynb": nb03,
    "04_depth_slippage_vacuum.ipynb": nb04,
    "05_pm_btc_chainlink_leadlag.ipynb": nb05,
    "06_prereplay_counterfactual.ipynb": nb06,
    "EX1_data_tour.ipynb": ex1,
    "EX2_market_stories.ipynb": ex2,
    "EX3_correlations.ipynb": ex3,
}


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "research" / "notebooks"
    out.mkdir(parents=True, exist_ok=True)
    for name, builder in NOTEBOOKS.items():
        (out / name).write_text(json.dumps(builder(), indent=1), encoding="utf-8")
    print(f"wrote {len(NOTEBOOKS)} notebooks to {out}")


if __name__ == "__main__":
    main()
