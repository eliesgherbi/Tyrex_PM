# Research workspace

The research workspace is a local web app for recording market data, building window sets and replaying strategies on them. It never places an order, never starts a paper session, and never runs a live strategy. Replays use the same host components as live trading (see [architecture.md](architecture.md)).

## Start it

```powershell
tools\start_ihm.ps1      # builds frontend/ (npm install the first time), then serves it
```

or, with the frontend already built:

```bash
python -m tyrex_pm.research.ihm --host 127.0.0.1 --port 4173
```

Open http://127.0.0.1:4173/. Run **one** server per port (Windows allows a second process to bind the same port; the server disables address reuse there).

| Setting | Effect |
|---|---|
| `TYREX_WAREHOUSE_ROOTS` | Warehouses the workspace reads, separated by `;` on Windows. Default: `var/warehouse_btc5m`, `var/warehouse_btc5m_s3`, `var/warehouse`, `var/warehouse_phase_b_validation` and every `var/warehouse_rec_*` created from the page. |
| `TYREX_REPLAY_WORKERS` | Parallel replay processes per job (default 3). |

Workspace records (sets, experiments, jobs, configurations, recordings, trash) live under `var/research/workflows/ihm/`.

## Data

### Recording sessions

- **Record market data** runs the collector for the BTC 5-minute family: both order books, the Chainlink reference, Binance trades and depth, metadata and resolutions. It checks first that Polymarket, the reference feed and Binance are reachable and that credentials are present.
- **Finish the last window** (on by default) keeps recording until the window open at the planned end closes, plus 30 s, so the last window is complete.
- **Then wait for winners** (default up to 15 min) stops books and feeds and only polls Polymarket's resolutions until every recorded window has its winner.
- **Fetch the missing winner(s)** on a recording card asks Polymarket again for winners the recording did not observe and stores the answers as observations received at that time.
- **Recording health** shows, per window, the book time lost and whether it is kept under the current policy.

### Window sets

A window set is a frozen list of windows plus the exact warehouse builds they were read from (checksums are verified when a set is opened).

- **Policy.** `replay_research_v2_1` (default): both books valid for at least 90% of the window, no hole longer than 20 s, none in the last 60 s, blips under 250 ms ignored, the reference covering the window, and the winner reported by Polymarket. Excluded windows keep a reason (for example *recording ended before the window closed* or *winner not observed yet*).
- **Goal.** Pick a **cookbook** card (recipes in `config/research/set_recipes/`, for example *US open hour* or *High-volatility windows*; percentile thresholds are computed at preview and saved with the set), **all windows that pass the policy**, or a **custom** set (hours, weekdays, time zone, market conditions). A custom set's filters can be saved as a new cookbook recipe.
- **Versions.** Re-save a set under the current rules, or add recordings to make a new version; the original never changes.
- **Reserve for Tune** marks windows as a future holdout; previews warn when a set uses them.
- **Export** writes CSV or JSON. **Delete** moves sets and results to the trash, which can restore them.

## Research

### Replay

1. Choose a window set (or **Use in experiment** from its card).
2. Choose the **strategy**: every registered strategy that declares a research description is offered (settlement_value, z_gap, ask70, feature_probe). The form comes from that strategy's own parameters; fields owned by the set's policy are shown locked. Strategies that require protection get their default protection block.
3. Choose the execution **scenario**: `friction_v3` (default: recorded fees, venue-reconciled books) or `friction_stress_v2` (adverse tick, lower participation, longer delays). Older scenarios stay selectable to reproduce earlier results.
4. **Validate** each configuration (server side), then **Preview** and **Run**. A batch can mix strategies; configurations that share a strategy and execution assumptions form one experiment.

Jobs run in background processes; stopping keeps finished windows, and **Resume** continues an interrupted or failed job.

### Results

Each result is one run and one named configuration:

- **Qualification** per window: *qualified*, *qualified with notes* (for example book blips under the gap tolerance) or *unqualified* (book or reference invalid while an order was in flight). Unqualified windows are shown, never folded into P&L silently.
- **Engine version**: results made before the current replay engine (`replay_v1`) say so; replay the set again to compare under the current engine (`replay_v2`).
- **Compare** puts runs side by side; **Candidates** freezes a configuration with its evidence. **Tune** is a later phase.

## What the replay does

`research/strategy_replay/run.py` replays one window: recorded rows feed the live market-session runtime, scheduler, indicator graph and decision-context builder on recorded time; intents go through the live planner and final-gate checks; orders fill in the simulated venue (`research/replay/engine.py` with `execution/fill_model.py`, the paper venue's rule); outcomes go back to the strategy. Strategies whose contract reads Binance depth get it rebuilt at the window start (slower: about 3 minutes per window).

## Notebooks

`research/strategy_lab/` holds the settlement_value research notebooks and helpers. Notebook studies saved before the October 2026 re-normalization replay on the parser-1 builds they used.
