"""Generate M2B.4 notebook stubs (one-time helper)."""

from __future__ import annotations

import json
from pathlib import Path


def _nb(title: str, code: str) -> dict:
    bootstrap = """
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
    return {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": [f"# {title}\n"]},
            {
                "cell_type": "code",
                "metadata": {},
                "source": [line + "\n" for line in bootstrap.splitlines()],
                "outputs": [],
                "execution_count": None,
            },
            {
                "cell_type": "code",
                "metadata": {},
                "source": [line + "\n" for line in code.strip().splitlines()],
                "outputs": [],
                "execution_count": None,
            },
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


NOTEBOOKS = {
    "01_coverage_quality.ipynb": _nb(
        "M2B.4 Notebook 01 — Coverage, quality, label validity",
        """
from research.lib.loaders import load_day
from research.lib.markets import build_clean_markets, notebook_01_decisions, ws_seq_gap_audit, write_clean_markets
from research.lib.buckets import format_decision_output

PARTITION = REPO_ROOT / "var/parquet/date=2026-07-05"
OUT = REPO_ROOT / "research/output/m2b4"
OUT.mkdir(parents=True, exist_ok=True)

day = load_day(PARTITION, load_books=False)
clean = build_clean_markets(day)
write_clean_markets(clean, OUT / "clean_markets.csv")
gap_audit = ws_seq_gap_audit(day.tables.get("lifecycle_events"))
decisions = notebook_01_decisions(day, clean, gap_audit)
print(format_decision_output(
    decisions=decisions,
    confidence="low",
    total_sample_size=len(clean),
    per_bucket_samples={"included": int(clean["include_for_analysis"].sum())},
    provisional=True,
    provisional_reason="27-market sample; M2B.1-B pending",
    follow_up="Notebooks 02-06 require clean_markets.csv",
))
clean.head()
""",
    ),
    "02_jump_protection_distances.ipynb": _nb(
        "M2B.4 Notebook 02 — Jump distribution + protection distances + resolution tail",
        """
from research.lib.loaders import load_day
from research.lib.latency import build_latency_prior, write_latency_prior
from research.m2b4.pipeline import run_notebook_02
from research.lib.buckets import format_decision_output

OUT = REPO_ROOT / "research/output/m2b4"
day = load_day(REPO_ROOT / "var/parquet/date=2026-07-05", load_books=True)
prior_path = write_latency_prior(build_latency_prior(default_roots=[REPO_ROOT / "var/runs"]), OUT / "latency_prior.json")
result = run_notebook_02(day, OUT / "clean_markets.csv", prior_path)
print(format_decision_output(
    decisions=result["decisions"],
    confidence=result["confidence"],
    total_sample_size=result["total_sample_size"],
    per_bucket_samples=result.get("per_bucket_samples"),
    provisional=True,
    extra={"insufficient_latency_prior": result["decisions"].get("insufficient_latency_prior")},
))
""",
    ),
    "03_survivor_reachability.ipynb": _nb(
        "M2B.4 Notebook 03 — Survivor reachability (proxy; not M2B.7 label)",
        """
from research.lib.loaders import load_day
from research.m2b4.pipeline import run_notebook_03
from research.lib.buckets import format_decision_output

day = load_day(REPO_ROOT / "var/parquet/date=2026-07-05", load_books=True)
result = run_notebook_03(day, REPO_ROOT / "research/output/m2b4/clean_markets.csv")
print(format_decision_output(decisions=result["decisions"], confidence=result["confidence"], total_sample_size=result["total_sample_size"], provisional=True))
""",
    ),
    "04_depth_slippage_vacuum.ipynb": _nb(
        "M2B.4 Notebook 04 — Depth, slippage, liquidity vacuum",
        """
from research.lib.loaders import load_day
from research.m2b4.pipeline import run_notebook_04
from research.lib.buckets import format_decision_output

day = load_day(REPO_ROOT / "var/parquet/date=2026-07-05", load_books=True)
result = run_notebook_04(day, REPO_ROOT / "research/output/m2b4/clean_markets.csv")
print(format_decision_output(decisions=result["decisions"], confidence=result["confidence"], total_sample_size=result["total_sample_size"], provisional=True))
""",
    ),
    "05_pm_btc_chainlink_leadlag.ipynb": _nb(
        "M2B.4 Notebook 05 — PM / BTC / Chainlink lead-lag",
        """
from research.lib.loaders import load_day
from research.m2b4.pipeline import run_notebook_05
from research.lib.buckets import format_decision_output

day = load_day(REPO_ROOT / "var/parquet/date=2026-07-05", load_books=False)
result = run_notebook_05(day, REPO_ROOT / "research/output/m2b4/clean_markets.csv")
print(format_decision_output(decisions=result["decisions"], confidence=result["confidence"], total_sample_size=result["total_sample_size"], provisional=True))
""",
    ),
    "06_prereplay_counterfactual.ipynb": _nb(
        "M2B.4 Notebook 06 — Pre-replay counterfactual (NOT M2B.5)",
        """
from research.lib.loaders import load_day
from research.lib.latency import build_latency_prior, write_latency_prior
from research.lib.markets import build_clean_markets, notebook_01_decisions, ws_seq_gap_audit, write_clean_markets
from research.m2b4.pipeline import run_notebook_02, run_notebook_03, run_notebook_04, run_notebook_05, run_notebook_06
from research.lib.buckets import format_decision_output

OUT = REPO_ROOT / "research/output/m2b4"
day = load_day(REPO_ROOT / "var/parquet/date=2026-07-05", load_books=True)
clean = build_clean_markets(day)
write_clean_markets(clean, OUT / "clean_markets.csv")
nb01 = {"decisions": notebook_01_decisions(day, clean, ws_seq_gap_audit(day.tables.get("lifecycle_events")))}
prior_path = write_latency_prior(build_latency_prior(default_roots=[REPO_ROOT / "var/runs"]), OUT / "latency_prior.json")
nb02 = run_notebook_02(day, OUT / "clean_markets.csv", prior_path)
nb03 = run_notebook_03(day, OUT / "clean_markets.csv")
nb04 = run_notebook_04(day, OUT / "clean_markets.csv")
nb05 = run_notebook_05(day, OUT / "clean_markets.csv")
result = run_notebook_06(nb01, nb02, nb03, nb04, nb05)
print(format_decision_output(decisions=result["decisions"], confidence="low", total_sample_size=result["total_sample_size"], provisional=True, follow_up="Hand grid to M2B.5 replay"))
""",
    ),
}


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "research" / "notebooks"
    out.mkdir(parents=True, exist_ok=True)
    Path(__file__).resolve().parents[1].joinpath("research/output/m2b4/.gitkeep").touch()
    for name, nb in NOTEBOOKS.items():
        (out / name).write_text(json.dumps(nb, indent=1), encoding="utf-8")
    print(f"wrote {len(NOTEBOOKS)} notebooks to {out}")


if __name__ == "__main__":
    main()
