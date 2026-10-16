"""Freeze the reviewed v2 research default and refresh notebook 04 after B/C finish."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import nbformat

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import review_settlement_baseline_2026_10_16 as review  # noqa: E402

from tyrex_pm.research.strategy_lab import staged as st  # noqa: E402
from tyrex_pm.research.strategy_replay.results import config_hash  # noqa: E402
from tyrex_pm.strategies.settlement_value.schema import (  # noqa: E402
    settlement_value_config_from_parameters,
)


def usd(value: float) -> str:
    return f"${value:,.2f}"


def main() -> None:
    exp = st.open_experiment(review.EXPERIMENT)
    records = {stage: exp.stage_record(stage) for stage in ("A", "B", "C")}
    if any(not record or record["status"] != "completed" for record in records.values()):
        raise RuntimeError("Finish A, B and C before freezing the research baseline.")
    source = st.code_identity()["source_fingerprint"]
    if any(record["code"]["source_fingerprint"] != source for record in records.values()):
        raise RuntimeError("Stages must use the same frozen source.")
    review.main()
    analysis_path = review.OUT / "analysis.json"
    analysis = json.loads(analysis_path.read_text())
    selected = review.ROBUST_ALTERNATIVE
    alternative = review.RAW_LEADER
    configs = {c["config_id"]: c for c in records["A"]["configurations"]}
    comparisons = {(r["stage"], r["config_id"]): r for r in analysis["comparisons"]}
    for config in (selected, alternative):
        if comparisons[("C", config)]["net_pnl"] <= 0:
            raise RuntimeError("A finalist lost in C; review manually before freezing a default.")
    baseline = configs[selected]
    effective = baseline["effective"]
    parsed = settlement_value_config_from_parameters(effective)
    if config_hash("settlement_value", parsed) != selected:
        raise RuntimeError("Exported parameters do not reproduce the selected identity.")
    config_path = REPO / "config/research/settlement_value_hold_baseline_v2.json"
    config_path.write_text(json.dumps(effective, indent=2) + "\n", encoding="utf-8")
    selection = {
        "status": "provisional_development_default_for_workflow_comparison",
        "experiment_id": exp.id,
        "runner": exp.record["replay"]["runner"],
        "clock_policy": exp.record["replay"]["clock_policy"],
        "source_fingerprint": source,
        "selected_config_id": selected,
        "selected_label": baseline["label"],
        "raw_pnl_leader_config_id": alternative,
        "raw_pnl_leader_label": configs[alternative]["label"],
        "selection_basis": (
            "User prefers a provisional, less concentrated baseline to finish the workflow. "
            "TWAP 2c/150s spreads gains across the two larger collections; detailed KPI "
            "optimization and untouched evaluation are deferred. It is not a proven optimum."
        ),
        "configuration_file": str(config_path.relative_to(REPO)).replace("\\", "/"),
        "config_file_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "development_windows": len(exp.windows),
        "holdout_evaluated": False,
        "retry": "absent in v2; one emitted entry consumes the window",
        "exit": "hold_to_resolution only; no early sales",
        "reentry": "disabled by the v2 single-attempt lifecycle",
        "live_qualified": False,
        "analysis_file": str(analysis_path.relative_to(REPO)).replace("\\", "/"),
        "full_effective_parameters": effective,
    }
    (review.OUT / "baseline_selection.json").write_text(
        json.dumps(selection, indent=2) + "\n", encoding="utf-8"
    )
    primary = comparisons[("A", selected)]
    favorable = comparisons[("C", selected)]
    leader_a = comparisons[("A", alternative)]
    leader_c = comparisons[("C", alternative)]
    benchmark = next(r for r in analysis["comparisons"] if r["stage"] == "B")
    config_link = "../../../config/research/settlement_value_hold_baseline_v2.json"
    selection_link = (
        f"../../../var/research/strategy_runs/staged/{exp.id}/"
        "baseline_review/baseline_selection.json"
    )
    text = f"""# V2 hold-to-resolution baseline: completed A/B/C and provisional default

Experiment: `{exp.id}`. Replay: `strategy_closed_loop_v2`, original share-based execution.
Scope: settlement_value, one entry attempt per window, hold to resolution, no retries,
early exits, re-entry or new live-execution features. The experiment source stayed unchanged.

## Decision

Freeze **TWAP, minimum conservative edge 2 cents/share, entry from 150 seconds remaining
until 30 seconds remaining** as the provisional development baseline. This implements the
user's preference for less concentrated performance and completes the workflow; it is
not a claim that this is the optimal or live-ready strategy. Detailed KPI selection is deferred.

Keep **TWAP 5 cents / 240 seconds** as the raw-P&L comparison candidate.

Full effective parameters: [baseline JSON]({config_link}).
Selection/provenance: [selection manifest]({selection_link}).

## What A, B and C mean

- **A:** six TWAP settings under friction, each on the same 94 development windows:
  564 runs. Only edge (2/5/8 cents) and earliest entry time (240/150 seconds remaining)
  vary. This is a small entry search with other parameters fixed.
- **B:** one Endpoint benchmark at 5 cents / 240 seconds under friction on those same
  94 windows: 94 runs. Compare it with A's TWAP 5-cent/240-second row to isolate the
  model change. B is not another split, a new data period or a full Endpoint grid.
- **C:** the two preselected A finalists under favorable execution on the same windows:
  188 runs. It changes latency, depth participation and adverse ticks; it does not
  change the entry parameters or provide untouched-data validation. Favorable is not
  a guaranteed profitability upper bound.

All 846 simulations are complete, with timelines. No holdout strategy evaluation was
launched or used for selection.

## Results used for the provisional choice

- Selected 2-cent/150-second baseline: friction {usd(primary["net_pnl"])},
  {primary["fills"]} fills from {primary["requests"]} requests,
  {primary["wins"]} wins / {primary["losses"]} losses; favorable
  {usd(favorable["net_pnl"])}, {favorable["fills"]} fills.
- Raw leader 5-cent/240-second candidate: friction {usd(leader_a["net_pnl"])},
  {leader_a["fills"]} fills; favorable {usd(leader_c["net_pnl"])},
  {leader_c["fills"]} fills.
- Endpoint benchmark: friction {usd(benchmark["net_pnl"])}, {benchmark["fills"]} fills,
  {benchmark["wins"]} wins / {benchmark["losses"]} losses.
- Selected baseline's friction P&L by recording date:
  {", ".join("/".join(r["utc_days"]) + ": " + usd(r["net_pnl"]) for r in primary["by_collection"])}.
- The raw leader's largest win contributes {usd(leader_a["largest_win"])} of its
  {usd(leader_a["net_pnl"])}. Removing each candidate's largest win leaves
  {usd(primary["net_without_best_window"])} for the selected baseline and
  {usd(leader_a["net_without_best_window"])} for the raw leader. This is a diagnostic,
  not a reason to discard legitimate winning trades.

The preference is based on distribution across recordings and lower concentration,
not merely the highest win rate. Favorable/friction outcomes can differ in which
signal is reached or filled, so favorable need not always make more P&L.

## Fixed parameters and how to reuse them

- Model: `twap_proxy_diffusion`; sigma band 0.25, reference band 1 bps, Binance spot.
- Volatility: half-life 30 seconds, sigma floor 0.00001, sigma scale 1.0.
- Entry: 2-cent minimum edge, remaining-time band 30–150 seconds,
  maximum price 0.97, one-tick limit buffer, `cap_only` sizing.
- Intended debit/loss target: 5 USDC; actual v2 replay debit can exceed this target.
- The $3 expected-profit field stays a research diagnostic, not a forced sizing target.

In notebook 04 the frozen JSON is loaded as `BASELINE_PARAMETERS`. Its six-point grid
remains unchanged for reproducibility. Reopening/running the notebook leaves all stages
disarmed. B/C are already complete, so no rerun is needed to view them.

For future v3 experiments, explicitly select the live-aligned order contract and start
with retry off, hold-to-resolution and one trade per window. First measure alignment
alone, then compare retry and exit variants against that baseline. The v2 identity and
economics are not interchangeable with v3; this file freezes the strategy hypothesis,
not a production execution profile.

## Limits and deferred work

- All outcomes are proxy-labelled; K is provisional, fees and execution are assumed.
- 94 windows come from only three recording dates; nearby windows are dependent.
- These totals sum independent window replays; they do not test continuous shared
  capital or redemption availability. That is a later ordered-window experiment.
- All A filled runs carry `gap_with_exposure` and `order_constraints_unqualified` flags.
  The biggest win's book becomes invalid after entry at several in-window timestamps;
  do not interpret the traces as uninterrupted executable liquidity for exit research.
- V2's share-based friction fills can exceed the intended debit target. Selected
  baseline maximum actual debit is {usd(primary["maximum_actual_debit"])}, with
  {primary["runs_spending_above_5"]} runs above 5 USDC; raw leader maximum is
  {usd(leader_a["maximum_actual_debit"])}. This study preserves v2, rather than silently
  changing execution rules mid-experiment.
- Window-bootstrap intervals in the analysis assume independence and do not account
  for grid selection or serial dependence. They do not establish validated profitability.
- Detailed risk/KPI selection, untouched-data evaluation, provider qualification and
  live activation are outside this workflow closeout. No real orders were submitted.

## Reproducibility

Source fingerprint: `{source}`. Result files are under
`var/research/strategy_runs/staged/{exp.id}/stage_{{A,B,C}}/`.
Scripts `finish_settlement_baseline_2026_10_16.py` and
`review_settlement_baseline_2026_10_16.py` preserve stage identities and read saved outputs.
The original notebook is backed up under `scratchpad/baseline_v2_closeout_2026-10-16/`.
"""
    report_path = REPO / (
        "Docs/implementation/strategies/settlement_value_v2_baseline_selection_2026-10-16.md"
    )
    report_path.write_text(text, encoding="utf-8")
    notebook_path = REPO / (
        "src/tyrex_pm/research/strategy_lab/04_staged_settlement_research.ipynb"
    )
    backup = REPO / "scratchpad/baseline_v2_closeout_2026-10-16"
    backup.mkdir(parents=True, exist_ok=True)
    backup_path = backup / "04_staged_settlement_research.before.ipynb"
    if not backup_path.exists():
        shutil.copy2(notebook_path, backup_path)
    nb = nbformat.read(notebook_path, as_version=4)
    for cell in nb.cells:
        source_text = cell.source
        if cell.cell_type != "code":
            continue
        if source_text.startswith("# Reload edited project code"):
            source_text = (
                "# Use source from this checkout, even when the interpreter is shared.\n"
                "import sys\n"
                "from pathlib import Path\n"
                "_cwd = Path.cwd().resolve()\n"
                "_repo_root = next(p for p in (_cwd, *_cwd.parents)\n"
                "                  if (p / 'src/tyrex_pm').is_dir())\n"
                "sys.path.insert(0, str(_repo_root / 'src'))\n\n" + source_text
            )
        for stage in ("A", "B", "C"):
            source_text = source_text.replace(
                f"RUN_STAGE_{stage} = True", f"RUN_STAGE_{stage} = False"
            )
        if source_text.startswith("FINALISTS ="):
            source_text = (
                "FINALISTS = [\n"
                f"    '{alternative}',  # TWAP 5 cents / 240 seconds\n"
                f"    '{selected}',  # TWAP 2 cents / 150 seconds\n"
                "]\n"
                "if FINALISTS:\n"
                "    display(st.preview(exp, 'C', workers=WORKERS, finalists=FINALISTS,\n"
                "                       keep_timelines=KEEP_TIMELINES))\n"
            )
        if 'EXPLORE = "full_development-' in source_text:
            source_text = source_text.replace(
                'EXPLORE = "full_development-6ae3051843"', f'EXPLORE = "{exp.id}"'
            )
        if "app = ex.Explorer" in source_text:
            source_text = (
                "if dataset is not None:\n"
                "    try:\n"
                "        import ipywidgets\n"
                "    except ImportError:\n"
                "        print('Static charts are ready. The interactive explorer '\n"
                "              'requires ipywidgets in this kernel.')\n"
                "    else:\n"
                "        app = ex.Explorer(dataset=dataset.spec.key, scenario='friction')\n"
                f"        app.w_config.value = '{selected}'\n"
                "        display(app.view)\n"
                "else:\n"
                "    print('No completed stage to explore.')"
            )
        cell.source = source_text
    marker = "## Frozen provisional v2 baseline"
    if not any(cell.source.startswith(marker) for cell in nb.cells):
        at = next(
            i
            for i, cell in enumerate(nb.cells)
            if cell.source.startswith("## What these results can and cannot show")
        )
        nb.cells[at:at] = [
            nbformat.v4.new_markdown_cell(
                f"{marker}\n\n"
                f"A/B/C are complete for **{exp.id}**, with 94 development windows.\n\n"
                "Provisional default: **TWAP, edge 2 cents/share, entry with 30–150 seconds "
                "remaining**, one attempt, hold to resolution. Retry, early exits and re-entry "
                "are absent from this v2 baseline. The raw-P&L leader, 5 cents / 240 seconds, "
                "is retained as an alternative.\n\n"
                "This choice prioritizes less concentrated development performance to close "
                "the workflow. Detailed KPI optimization and untouched evaluation remain "
                "later work; this is not a live-ready or proven optimal configuration.\n\n"
                "B compares Endpoint under friction; C changes execution assumptions for the "
                "two TWAP finalists. Neither is a holdout test. All stages remain disarmed."
            ),
            nbformat.v4.new_code_cell(
                "import json\n"
                "from pathlib import Path\n\n"
                "BASELINE_PARAMETERS = json.loads(\n"
                "    (st.REPO / 'config/research/settlement_value_hold_baseline_v2.json')\n"
                "    .read_text(encoding='utf-8')\n"
                ")\n"
                "_review = json.loads((exp.path / 'baseline_review/analysis.json')\n"
                "                     .read_text(encoding='utf-8'))\n"
                "_chosen = {\n"
                f"    '{selected}',\n"
                f"    '{alternative}',\n"
                "}\n"
                "_rows = [{k: r[k] for k in\n"
                "          ('stage', 'label', 'scenario', 'requests', 'fills', 'wins',\n"
                "           'losses', 'net_pnl', 'total_debit', 'largest_win')}\n"
                "         for r in _review['comparisons']\n"
                "         if r['config_id'] in _chosen or r['stage'] == 'B']\n"
                "display(pd.DataFrame(_rows).round(2))\n"
                "display(st.flat(BASELINE_PARAMETERS))\n"
                "_baseline_windows = ex.windows_for(\n"
                f"    dataset, '{selected}', 'friction')\n"
                "ex.plot_windows(_baseline_windows,\n"
                "                title='Provisional hold baseline: TWAP 2 cents / 150 seconds')\n"
                "plt.show()\n"
                "print('Baseline saved; no holdout evaluated and no new replay launched.')"
            ),
        ]
    nbformat.validate(nb)
    nbformat.write(nb, notebook_path)
    if st.code_identity()["source_fingerprint"] != source:
        raise RuntimeError("Source fingerprint changed unexpectedly.")
    print(
        json.dumps(
            {
                "selected": baseline["label"],
                "report": str(report_path),
                "config": str(config_path),
                "notebook": str(notebook_path),
            }
        )
    )


if __name__ == "__main__":
    main()
