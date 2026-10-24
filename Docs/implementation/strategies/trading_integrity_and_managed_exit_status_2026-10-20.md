# Trading integrity and managed exits: implementation status

Updated 2026-10-20, Europe/Paris. Implementation was authorised by the user.

The four code changes are implemented in the current checkout. The bounded paper,
reconstruction and 94-window book regression checks are complete. Full live qualification
is **not complete**: the paper probes had no fills, so a captured BUY/sale/restart and
representative order-path latency remain unproven. Nothing was committed or merged; no real
order, redemption or production-ledger repair was performed. Existing live templates
and the frozen comparison entry parameters are unchanged.

This report distinguishes code delivery from qualification. It supersedes any reading
of the original plan suggesting that passing unit tests qualifies a strategy for live.

## T1 — settlement ownership

Implemented and verified with local subprocess and CLI tests.

- A lock beside the canonical ledger path excludes competing writers even when two
  configs use different execution directories. The runner acquires its execution lock
  first, then ledger ownership, and retains both until shutdown.
- Manual reconciliation, redemption confirmation/discovery, manual marking and loss
  acknowledgements acquire ownership before opening a writable projection or doing
  network/command work. Ownership repair uses the same lock order.
- Read-only status/worklist remain usable while the runner owns the ledger. They read
  a complete-line prefix and do not repair a partially written tail.
- A writer backs up and repairs an incomplete tail before the next append. A parseable
  JSON object without the terminating newline is also treated as an incomplete append.
  Interior corruption still refuses recovery. Expired projections cannot write.
- Production reuses one writable projection. Compatibility callers reload under the
  guard for an atomic check/append. There is no lock acquisition on every decision.

Supported storage is a local filesystem. UNC ledgers and hard-linked ledger files are
refused. This is process ownership, not a distributed locking scheme. Accounting rules,
cash recognition and loss allowances were not reset or changed.

## T2 — shared book correction and deferred publication

Correctness changes and focused economics regression are verified. End-to-end speed
qualification remains open.

`venue_top_v1` retains the best bid/ask reported by the venue and reconciles each
token's complete message before validating it. It removes levels contradicted by
the reported top, never invents missing quantities, and keeps missing/locked/crossed
or out-of-order books unavailable. Missing venue-top fields provide structural checks
only. Existing binding/epoch checks and REST race protections remain.

Live stores select this correction for future runs. Historical replay defaults remain
`legacy`; new replay policies explicitly select `book_reconstruction: venue_top_v1`.
Policy identity and timeline cache keys prevent mixing those results. This is a book
semantics change even though the selected focused trades happened to remain identical.

Book depth is materialized and sorted only when requested, once per version. Size-only
updates invalidate the cached depth too. Quote/readiness checks avoid full-depth copies,
and unused book publication is skipped. Maps are still copied at each delta boundary
to preserve failure atomicity; this is not a claim of O(changes) mutation cost.
REST recovery is coalesced behind a 250 ms non-blocking debounce. Books remain
unavailable during recovery and a repaired snapshot can make the pending REST call
unnecessary.

Evidence: `var/research/trading_integrity/book_audit.json`.

- Two saved captures, 880,039 CLOB messages, both policies applied to the same inputs.
- Legacy reported READY with a venue-top mismatch 1,390 times; v1 had zero such cases.
- V1 was more conservative: 7,471 unavailable token updates versus 1,520 under legacy.
  These are message counts, not missed trades or seconds. Removing warnings is not the
  success criterion; unsupported depth must stay unavailable.
- Per-message median mutation measurements were 116–175 microseconds for v1 versus
  88–130 for legacy using the same optimized representation. These are indicative,
  shared-load micro measurements, excluding depth reads, transport and submission.
  They do not compare against the original implementation and prove no 30x speedup.

Observation regression:

- `observation_legacy/reconstruction.json`: all 919 actions/reasons match; 901 rows
  match every compared field. Eighteen probability differences are at most
  0.000001886, with the previously documented sub-millisecond timing discrepancy.
- `observation_venue_top_v1/reconstruction.json`: all 919 actions/reasons still match;
  899 rows match every field. The same eighteen probability differences remain.
  Two additional evaluations with legacy quotes have unavailable quotes under v1:
  window `1792418700`, index 16, and window `1792419000`, index 42. The decisions do
  not change. Their stricter book availability is retained rather than forcing equality.
- Both strict `decisions_reconstructed_equal` flags remain false. The report does not
  relabel these captures as exact matches.

Focused trade regression: 8 development windows, two scenarios, old-book hold versus
v1 hold. Entries, sides, fills and P&L are identical. The eight old-book friction rows
also match saved P7 comparison/legacy results on entry, fill, side, debit, VWAP and P&L.

## T3 — recorded execution context

Implementation and deterministic components are verified. A complete public-feed
trading capture with a fill, hold/restart and managed sale has not been proven.

When `recording.raw_inputs: true`, the trading host now captures:

- one initial strategy-state seed per window and subsequent entry/exit/hold/budget
  feedback, with the original intent links;
- each evaluated or skipped decision's stable evaluation ID;
- the position/lifecycle and host permissions used by that evaluation, its applied
  execution-event cursor and settlement sequence;
- account snapshots once per new snapshot, referenced by the decision;
- accepted REST book applications, distinct from a merely received HTTP response.

These records use the existing bounded background recorder. No per-decision RPC or
SQLite read was added. At shutdown a read-only SQLite backup includes committed WAL
pages; a complete settlement prefix and checksums accompany it. Export work happens
at shutdown, not in the trading loop. Recording remains optional; capture failure
is visible through its existing policy.

Offline reconstruction independently reduces execution evidence to the recorded
prefix. It checks position quantity, captured cost and held token where present,
lifecycle, account references, ledger sequence, checksums and raw sequence gaps.
It restores strategy feedback causally, then recomputes probabilities from raw market
inputs. It does not inject the final position or simulate replacement fills.
Missing/unsupported evidence and intent divergences produce failure diagnostics;
they cannot receive a global equality result. Trading imports have a new version;
historical observation imports retain their original identities.

Scope limit: host permissions are captured inputs. Reconstruction does not independently
re-authorize historical orders or reproduce the entire account governor/dispatch gate.
The generic explorer bundle compares captured/reconstructed decisions as diagnostics;
its placeholder summary fields must not be interpreted as backtest economics.

Deterministic paper test: a real `PaperVenue`, coordinator and SQLite journal perform
a simulated BUY, partial SELL and coordinator restart. The exported journal reduces to
the correct pre-sale and post-sale holdings. Tests also refuse missing cursors, wrong
positions, raw sequence gaps, missing intent references and altered/missing checksums.
This is component evidence, not the missing end-to-end capture proof.

Public-feed paper attempt:
`var/research/trading_integrity/paper_hold/out/run_summary.json`.
Market discovery failed on a TLS hostname mismatch for `gamma-api.polymarket.com`.
There were no sessions or orders. Configuration selected `venue: paper`; its
`live_requested` flag requests the execution path, not a real exchange account.
Certificate verification was kept enabled. The network/certificate problem must be
resolved before repeating this paper check.

Usage for a newly recorded supported run:

```powershell
.venv/Scripts/tyrex-pm.exe recording reconstruct --capture <run-folder> --mode trading --out <diagnostic-folder>
```

Reconstruction is offline. Open its diagnostic folder with the explorer using
`ex.dataset_key(Path(...))`. Older trading runs cannot acquire missing raw/execution
context retroactively.

## T4 — value exit and optional downward bid trend

Implemented in the shared strategy path, with mechanics tests and selected development
case research. Neither exit variant is qualified or enabled in a live template.

- E0: comparison + legacy volatility, hold to resolution.
- E1: sell bid depth whose estimated fee-net value exceeds the **held side's point
  probability** by 2 cents. UP uses `p_up`; DOWN uses `1 - p_up`.
- E2: E1 plus a 2-cent evaluated-best-bid decline over 5 seconds, confirmed for 2
  seconds. Gaps over 2 seconds, epoch changes or non-monotonic time reset the trend.

Those numbers are provisional research settings, not tuned defaults. A value exit
compares with executable bids after estimated fees, not midpoint/ask or entry cost.
It will not rescue a falling token if the model still assigns it probability 0.9995.
This remains a model-dependent exit, not an independent overconfidence detector.

Configuration uses the existing `strategy.parameters.exit` block. New fields are
`value_exit_trend_enabled`, `value_exit_trend_policy`, `value_exit_trend_lookback_s`,
`value_exit_trend_min_drop`, `value_exit_trend_confirmation_s` and
`value_exit_trend_max_gap_s`. Trend defaults off. `value_exit_basis: point` is explicit
in the new profiles; existing profiles retain their existing basis/default behavior.
Default-off settings preserve historical candidate identities.

Live and replay recheck a value exit before sending, without advancing the evaluated
trend or consuming another strategy decision. Existing sell ownership, partial-fill
accounting and uncertainty protections remain. New profiles stop initiating exits at
30 seconds remaining and commit proven residual holdings to resolution, including
when the model is unavailable. The host's later mandatory-flatten setting does not
silently override this residual policy; inability to prove the hold escalates explicitly.

Paper-only profiles:

- `config/runs/settlement_value_leader_5c_240s_e1_value_paper.yaml`
- `config/runs/settlement_value_leader_5c_240s_e2_value_trend_paper.yaml`

They use separate paper state/ledger/report paths and simulated collateral. Both
validate. Explanations are on; raw capture is off by default. A reconstruction trial
requires a separate copied diagnostic config with raw capture enabled.

## Case results and notebook usage

Dataset: `var/research/exit_cases/exits-a87d4015b233`.
Full focused review: `var/research/trading_integrity/focused-562f0a9852f3/review.json`.
Eight disclosed development windows include the comparison strategy's largest
development winner. There are 48 E0/E1/E2 replays plus 16 old-book hold regressions.
No used evaluation window was rerun and no new grid was launched.

Friction:

- E0 hold: 5 filled windows, +66.44.
- E1 value: same 5 fills, +47.87; 38.05 shares sold early.
- E2 trend: same 5 fills, +66.44; no early sale triggered.

Favorable sensitivity:

- E0 hold: 6 fills, +73.79.
- E1 value: 6 fills, +9.55.
- E2 trend: 6 fills, +68.47.

The friction loss in window `1790870700` is unchanged at -4.8843 in all variants.
E1 reduces two winners (`1791303300`, +19.42 to +6.16; `1792078800`, +8.84 to +3.54).
It leaves the largest cheap winner unchanged. Thus this set shows premature sales of
winners, not a demonstrated improvement in loss control. E2's friction equality means
the additional condition prevented sales; it does not establish a useful exit.
All economics use the declared simulated scenarios and estimated sell fees.

Notebook 06 has a new section 9. Run All reads saved results; `RUN_EXIT_CASES` remains
False. Select **Exit cases (experimental)** in the Dataset menu, then choose E0, E1 or
E2 and the same window/scenario. Section 9 also opens the latest saved exit bundle.
Restart the kernel after source updates if necessary. `ipywidgets` is needed for the
interactive controls. The data loader, saved trace, explanations and rendered plot
were checked; this environment's unit suite skips widget-only checks.

Timelines distinguish ENTER requests, EXIT requests at point probability, and confirmed
SELL fills. The exit-input table preserves recorded held-side probability, executable
exit facts and trend evidence. It never recomputes them from a later market state.

## Initial verification and gates (before the follow-up below)

The initial implementation unit suite passed **942 tests, with 3 skipped**, and no failures. Its report
is `var/research/trading_integrity/unit-tests.xml`.
The recorded-window integration module passed 3 tests. Changed Python files pass Ruff;
`git diff --check` passes. Notebook code cells compile, both paper profiles validate,
and old notebook 04/05 contents and saved experiments were preserved.

At that point the remaining gates were:

1. Resolve the public API TLS problem and complete recorded-paper hold/restart and
   managed-exit reconstruction end to end. Cover uncertain/late and detached-position
   cases through deterministic integration fixtures, not by waiting for a rare live event.
2. Measure loop and gate/submission latency on isolated comparable paper runs with
   sample counts and agreed thresholds. The audit micro timings do not satisfy this.
3. Review stricter availability on broader development data before approving the new
   book policy for live use. Current evidence is focused, not a full grid qualification.
4. Review exit case mechanics before broader exit research. There is no evidence here
   to promote E1 or E2. Freeze any justified policy before a future untouched evaluation.

The old P7 holdout remains used. This implementation creates no new independent
profitability evidence and does not authorize another real-money run.

## Follow-up qualification on 2026-10-20

The user authorised another qualification attempt after disabling the VPN. The Gamma
connection now passes TLS verification and returns HTTP 200. The original network
blocker above is historical; it is no longer the reason qualification is incomplete.

Evidence root: `var/research/trading_integrity/qualification-paper-20261020T063116Z`.
Its `protocol.json` records the bounded paper runs and engineering screening limits.
No real exchange orders or signing credentials are used. Paper measurements and
offline replay work are serialised.

Completed capture/restart checks:

- The recorded hold run completed cleanly with 731 evaluations and no entry signal.
  One full window had no exact boundary tick and was correctly skipped. In the other
  eligible window the conservative edge never passed.
- Restarting the same simulated account/ledger also completed cleanly, with 576
  evaluations and no entry signal. The startup window is intentionally observation-only;
  the next prepared window became entry-enabled. This is not a timing-cutoff defect.
- The restart recovered an exact boundary tick from subscription history at 26.685
  seconds, inside the 30-second recovery deadline. The next boundary arrived live in
  1.963 seconds. This adds actual feed evidence for the bounded recovery path.
- A read-only status command succeeds beside the active paper runner. A competing
  ledger writer is refused with exit code 3 before mutating the ledger; see
  `ledger_lock_probe.json`.

Qualification exposed three reconstruction issues, now corrected:

1. The stream reader included an unfinished compressed segment. It now reads only
   completed manifests and checks the compressed checksum, row count and missing files.
   Trading reconstruction also checks the complete raw tail against its recorded count.
2. A capture callback occurs slightly after the immutable evaluation snapshot is made.
   Recorded reconstruction now uses the snapshot's recorded clock as an exogenous input,
   rather than recalculating the model at callback time. New contexts record it directly;
   the first captures already retained it in the original decision's timestamp field.
   No model output is injected and no numerical tolerance is widened.
3. Windows can record a later input and an earlier decision at the same wall timestamp.
   The restart's one remaining quote mismatch was exactly this case: a later deletion
   was replayed before the decision. Recorded execution now orders those ties by the
   captured input prefix. Receipt timestamps are preserved. The ordinary research
   engine's priority rules are unchanged, and this ordering cannot simulate new orders.

The first capture matches all 731 evaluations after the snapshot-clock correction;
its point probability and conservative bounds also match exactly in the saved tables.
The restart matches all 576 evaluations after both timing corrections, including
quotes, K, sigma, point and conservative bounds at their declared tolerances.
Initial mismatch reports remain saved beside the corrected reports.

The full unit suite after these fixes passes **961 tests, with 3 skipped**. Evidence:
`var/research/trading_integrity/qualification-unit-tests.xml`.

All four bounded public-feed paper runs finished cleanly: recorded hold, restart of
the same simulated account, managed point-value exit, and optional-recording-off
hold baseline. There were no submitted orders or fills. The managed probe reached
entry-ready evaluations but found insufficient conservative edge or executable depth;
it did not exercise a sale. Its captured 731 evaluations reconstruct with no field
mismatches, including point probability and conservative bounds. The managed probe
and recording-off baseline used the same source fingerprint.

After excluding each timing run's first minute, the 12 recorded-hold minute p99
values range from 20.34 to 64.86 ms; the 12 optional-recording-off minute p99 values
range from 16.83 to 27.70 ms. These are ranges of minute quantiles, not run-wide p99
or a matched causal overhead estimate. Both pass the provisional 100 ms loop screen.
The recorded hold preceded the reconstruction fixes and context timestamp addition;
these are not a frozen same-build A/B benchmark. Formal overhead qualification needs
the same reviewed source revision in both conditions.
Both median minute p50 values round to zero at this Windows timer resolution, which
does not establish zero overhead. There are zero posted orders in either condition,
below the protocol's minimum of 30 each; gate/submission latency is **not qualified**.
Paper also includes simulated order latency and cannot prove real HTTP performance.
With one POST per five-minute window, these ten-minute probes could never supply 30
orders each. They are bounded stability/loop screens; the sample threshold belongs
to a later, longer order-path measurement, not a pass claim for this protocol.

The new partial/uncertain/late-entry, partial-sale and restart mechanics are exercised
through deterministic fixtures, including the real paper venue/coordinator/SQLite
path. They are not a substitute for a naturally filled captured paper run. No-fill
public runs do not prove held-position restart, sell execution or their full-context
reconstruction. Do not promote the exit or claim full live qualification from them.

The broader old/new book-policy comparison completed on the frozen comparison +
legacy hold candidate only: all 94 development windows, friction and favorable,
188 replays. It used no new parameter grid or untouched holdout. Evidence:

- `var/research/trading_integrity/book-development-5655a975a4a6/comparison.json`
- `var/research/trading_integrity/book-development-5655a975a4a6/readiness_review.json`

All 26 checked trade-summary fields match per window, including entry time, limit,
fill ratio, shares, attempt/POST counts, spend, expected value and realised P&L.
Friction remains 32 fills, debit 147.3670 and P&L +77.4204769753; favorable remains
39 fills, debit 190.7829 and P&L +99.6523857297. A roughly 1e-14 difference in one
aggregate is floating summation order; there is no per-window economic difference.

All 118,483 decision timestamps pair one-to-one, without missing/extra rows. Actions,
chosen sides, K, sigma and all three model probabilities are unchanged. Books are not
identical: availability differs and a few displayed prices change by 1–2 cents. The
saved examples are WAIT/WAIT or HOLD/HOLD and do not change trades. Friction has 23
BOOK_NOT_READY-to-EDGE_BELOW_COST transitions and 4 in reverse; favorable has 14 and
6. Thus the reason-count net improvements are 19 and 8, not a claim of universally
greater availability. Unsupported books remain unavailable.

All three completed captures were finally checked under the same recorded-clock and
input-order comparison contract: hold 731/731, restart 576/576, managed 731/731;
**2,038/2,038 evaluations match**, including position-context checks and probability
bounds. These public-feed captures are still flat-position evidence. The recorded-window
integration module was repeated after the fixes: **3 passed**; its XML is
`var/research/trading_integrity/qualification-recorded-integration.xml`. Changed Python
files pass Ruff and configuration validation passes for both paper exit profiles.

### Qualification verdict

The bounded checks are finished. T1 local writer exclusion is verified; T2 has passed
focused and broader development trade regression; T3 has exact reconstruction on these
flat public-feed probes plus deterministic execution fixtures; T4 is implemented,
case-tested and stable while flat, but has no public-feed sale evidence or demonstrated
return improvement. No new live profile or real-money run was activated.

Still required before claiming full live qualification:

1. A recorded paper BUY/partial-or-complete SELL and held-position restart, reconstructed
   through the full pipeline. The component fixtures are not that end-to-end proof.
2. A frozen same-build recording-on/off order-path measurement with enough posted orders,
   and separate real-provider latency/fee checks. These short probes cannot satisfy it.
3. Sell-fee/provider assumptions and any proposed exit numbers must be qualified before
   live use. The selected exit cases currently do not justify promoting E1 or E2.

The original comparison + legacy hold policy remains the research reference. Further
exit research should use development evidence first and freeze any justified variant
before touching genuinely new evaluation data. This report consumes no fresh evaluation.

For configuration and commands, see [the managed-exit usage guide](managed_value_exit_usage.md).
