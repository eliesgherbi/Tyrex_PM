# Captured live runs → replay; notebook 06 explorer; indicator explanations

Status: implemented 2026-10-19.

Commits:

| Commit | Content |
|---|---|
| `e0e2cf6` | explorer catalog |
| `a402eea` | capture → replay |
| `4c47019` | qualification stages and loop-lag summaries |

## 1. Notebook 06 explorer (shared dataset catalog)

**How to use it.** Open notebook 06 and Run All. Nothing launches, because `RUN_CASE_REPLAYS = False`. Section 7 shows the same explorer as notebooks 03–05. Pick a dataset, then a configuration, then a window. The timeline section shows the saved trace, or a **Generate timeline** button for exactly that run. The new section 7 of the explorer shows the recorded indicator explanations.

### Datasets in the catalog

| Kind | Source | Saved traces | Exact execution policy |
|---|---|---|---|
| fixed / staged | notebooks 03/04 | `decisions.parquet` | as before |
| `batch` (new) | v3 verification and feature runs, `case_runs` | per-scenario `decisions.parquet` | the row's `execution_policy`. Older v3 tables lack the column, so it is filled from `experiment.json`: `reference_availability_v1` plus the contract `replay_execution_contract_v1` |
| `study` (new) | preserved notebook 06 bundles (`settlement_extreme_cases/*`) | the bundle's own decisions, labelled "saved study trace" | `reference_availability_v1`, contract v1 |

The v3 verification run used to appear as an unloadable "staged experiment". It now opens as a batch dataset.

### Saved versus regenerated

- A saved trace is labelled as such.
- A regenerated trace is replayed on demand with the run's exact parameters, scenario and policy. Its digest is compared with the saved run, and its cache record stores the policy used.
- For study bundles, regeneration replays the variant as strategy settings on the production path. The migration check showed this is trading-equal. For the veto variant, decision labels differ and the caption says so.

Study bundles are indexed once into `var/research/case_runs/study_index/`. The bundles themselves are never modified.

The notebook was verified with nbconvert: it runs end to end with no errors and shows the explorer widget. Every catalog entry also renders headlessly, with timelines and explanation sections.

## 2. Indicator explanations

`var/research/case_runs/cases-e57988885be6` (launched from a script, 252 s) contains:

- 6 development windows × 2 candidates × {unchanged, corrected volatility};
- contract v2, friction scenario;
- `recording: {level: explain, explain_trigger: entry_band}`;
- 12,605 explanation records, 482–584 per run.

| Window | Category (v3 verification friction, default candidate) |
|---|---|
| `…1792085700` | large gap (31.6 pp), model right (+0.75) |
| `…1792088400` | large gap (21.5 pp), model right (+4.56) |
| `…1792089300` | large gap (20.2 pp), model wrong (−4.90) |
| `…1791307200` | large gap (19.3 pp), model wrong (−4.90) |
| `…1790870400` | notebook 06 "ordinary winner" |
| `…1790870700` | notebook 06 "ordinary loser" |

Each explanation row holds the estimator's variance and its units, the last return and dt, the sample count, effective samples, the jump guard, sigma, and the tape age. It also says whether it is context before an actionable evaluation.

Finding: every filled development entry already had a large model-vs-midpoint gap. Of the default candidate's 36 friction fills, 34 had a gap of 9 pp or more and the other 2 were between 5 and 9 pp. "Ordinary" (small-gap) entries therefore do not exist in this data. The case set covers right and wrong outcomes at large gaps instead.

The four live loss windows (`…1792263300`, `…1792266900`, `…1792262700`, `…1792273500`) are not in any local recording, so they cannot be replayed. Their compact saved evidence remains in notebook 06 sections 1–2.

## 3. Captured live data → replay (decision reconstruction)

**How to use it.** Run a signal-only observation with `recording: {raw_inputs: on}`, then:

```
tyrex-pm recording reconstruct --capture <observation output folder>
```

The command imports the capture into its own warehouse collection. Rerunning it is idempotent: the same raw segments give the same collection. It then reconstructs every evaluated window and writes `<capture>/reconstruction/reconstruction.json`.

### Path

1. The capture is converted into `RawEnvelope`s that keep each record's actual receipt wall and monotonic times and its order. These are written by `RawWriter.submit_envelope` into `var/warehouse_captures/collections/capture-<hash>`.
2. The existing normalizers process them (`normalize_once`), plus:
   - a new `rtds` branch that uses the live RTDS parser;
   - for live ticks, the receipt time is kept, and source timestamps are never used as arrivals;
   - subscription history is flagged `subscription_history`.
3. `WindowCatalog` and `load_window` load each window starting at the **capture start**. This gives the same indicator warm-up and connection generations as live.
4. `run_window(evaluation_instants=…, observe_only=True)` runs with:
   - evaluations exactly at the captured instants (not the replay scheduler);
   - no simulated orders, so the context stays flat, as in observation;
   - the favorable scenario (zero delays) and the live reference-availability rule;
   - the reference series reported as `polymarket_rtds:crypto_prices_twap_sixty:btc/usd`;
   - the clock taken from the captured live snapshots, applied as live with the same 60 s ageing (policy `captured_live_clock_snapshots_v1`);
   - K sealed by the same no-SSR rule.
5. The result is compared with the run's own decision rows, evaluation by evaluation.

### Fidelity labels (also in the import report)

| Input | Fidelity |
|---|---|
| RTDS, Binance trades | byte-exact |
| CLOB market websocket | SDK event dump, rewritten as the venue message (content-equivalent, not venue bytes) |
| CLOB REST book | SDK dump, audit only. Book reconstruction anchors on websocket snapshots, as for collector data |
| Gamma | SDK-converted event, with the event slug added to each market for the family matcher. Missing Gamma fields stay missing |
| Clock | the live `ClockSyncSnapshot`. It is stored in the existing warehouse `clock_observations` columns: status in `measurement_source`, measurement time in `server_at` |
| Connections, bindings, evaluation instants | host events |

### Proof (signal-only observation, 2026-10-19, 16 min, 4 windows)

The capture is `var/research/captures/capture_replay_probe_20261019`:

- 420,666 raw records imported;
- 841,577 normalized records, 0 rejected;
- inputs captured and complete; Binance depth is not in the input contract.

| Window | Evaluations live / replay | Action, reason and side equal | All compared fields equal |
|---|---|---|---|
| `…1792418400` (partial) | 38 / 38 | 38 | 38 |
| `…1792418700` | 296 / 296 | 296 | 289 |
| `…1792419000` | 293 / 293 | 293 | 288 |
| `…1792419300` | 292 / 292 | 292 | 286 |

In addition:

- Recommendations: 3 live, 3 reconstructed, at the same instants.
- K: the same value in every window, sealed from live boundary arrivals (lag 1.0–1.3 s).
- Book quotes and sigma: identical everywhere.
- `p_up_point`: differs in 18 of 919 evaluations, by at most 1.9×10⁻⁶. The evaluation marker is captured about 0.5 ms before live takes its snapshot time, so tau differs by that much.

`decisions_reconstructed_equal` is therefore `false` under the strict all-fields rule, even though every decision matches. The report keeps both counts.

An earlier 3.5-minute probe gave 197 of 197 decisions equal, with 2 tiny probability differences.

### Reconstructing decisions versus simulating fills

- **Reconstructing decisions** is what is proven above, and it is **observation-only**. A trading run also needs the position timeline from the execution journal after its first order; that is not implemented.
- **Simulating alternative fills** uses the ordinary `run_window` with a scenario. It needs nothing capture-specific and is not part of this proof.

The raw completeness manifest never claims decision completeness at capture time. It points to the reconstruction report.

### Limitations

- Binance `@depth20` partial snapshots are not normalized. They are not used by `settlement_value`.
- CLOB and Gamma remain SDK-derived, so their fidelity is "content-equivalent".
- The importer assumes the observation folder layout: `decisions.jsonl`, `observation_manifest.json` and `recording/`.
