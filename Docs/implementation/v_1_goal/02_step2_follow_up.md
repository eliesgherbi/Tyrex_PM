# Step 2 follow-up — deferred validation work

Status: deferred at the user's request on 20 October 2026. Return to this work after the Step 3/4 workflow and interface work. This is a retained backlog, not authorization to start fixes or new runs now.

## Sequencing decision

Step 3's independent contracts, data library, pinned selections, offline experiments, result adapters and profile generation can proceed. Step 4 can design and implement views/actions over those available operations. Keep incomplete or inconsistent evidence visible.

This is separate from final trading integration acceptance. Step 3 P5 and Step 4's paper launch/restart/reconstruction acceptance remain open until the relevant checks below pass. An offline workflow or interface can be delivered first; Step 2 and the full trading journey must not be marked validated because the interface is finished.

Do not repair deferred execution/reconstruction issues as hidden Step 3/4 refactoring. If an offline packet depends on one, expose the limitation or isolate the packet and bring the dependency back for an explicit scope decision. Use a fixed implementation/data identity for demonstrations and separate output directories.

No real-money orders or on-chain transactions are authorized. No automatic restart or scheduled follow-up is requested.

## What already exists

The detailed record is [Step 2 execution and results](02_execution_and_research_live_gaps.md). Retain that history and the original captures.

- Retry episode timing, backoff, side and usage restoration were implemented. Service-failure progression and original lifecycle-policy recovery were also checked.
- Startup tracking of an already committed hold was corrected after the first S0 restart kept shares but stopped. The replacement tracked the hold and rolled into a second filled/committed window.
- Paper reports distinguish execution arming from actual venue and real-order authorization.
- Five validated verification profiles and their guide exist under `config/runs/coock_book_conf/`; the reference profile was preserved.
- Recorded automated checks passed: 73 focused tests, a broader selection of 487 passed/1 skipped, a separate window-debit check, and four later regressions. These overlap and were not all run on one final build; do not add their counts or treat them as a fresh whole-suite result.
- Paper evidence observed fills/hold commitment, restart preservation/rollover, a retry backoff and eligibility refusal, zero-fill handling, and one protection sale. No real orders were sent.

## Why validation remains open

| Setup | Evidence obtained | Limitation |
|---|---|---|
| S0 hold | Fill, commitment, shares retained after restart, replacement process continued and rolled | No raw trading-context stream; exact reconstruction refused. Share quantities also need the accounting review below. |
| S1 retry | First-post fill in one window; another window rejected before POST, waited backoff, then stayed ineligible | No second POST or paper restart during backoff. Positions matched the journal; 1 of 1,078 decisions differed. |
| S2 value exit | Two zero-fill posts; retry-off episodes closed | No position or strategy sale. All 1,016 decisions and position contexts reconstructed, but this is not sale evidence. |
| S2 protection | Stop triggered and sold 10.36 shares | Sale-window reconstruction refused for a host-originated intent; ledger/account residual quantities disagree. |
| S3 re-entry | One entry rejected before POST | No fill, sale or second episode. Positions matched the journal; 1 of 1,121 decisions differed at rollover. |

Matching a captured position context to its execution journal proves consistency with that journal. It does not independently prove that the journal agrees with the paper account or fee-adjusted inventory.

## Ordered work when we return

### F0 — Re-establish the baseline and preserve evidence

1. Read this backlog, the Step 2 execution record and the current Step 1 inventory.
2. Identify which source/configuration/reporting contracts changed during Step 3/4. Reassess applicability of older results; do not assume a passing result covers the new tree.
3. Preserve original account snapshots, execution exports, ledgers, raw captures, reconstruction reports and build fingerprints. Put corrected/diagnostic outputs in new versioned locations.
4. Pin the resumed source build, including relevant uncommitted changes. Keep the reference YAML unchanged.

**Done when:** the retained evidence and the resumed build/configuration identities are explicit, and each check below has a reproducible input.

### F1 — Reconcile gross/net shares and residual accounting

This is the first correctness issue to investigate. Evidence is under `var/research/trading_integrity/step2/paper/s2_protect/`.

- `settlement.jsonl` records `FILL_CONFIRMED.net_shares: 10.78`, sale quantity 10.36 and `dust_shares: 0.42`.
- `execution/paper_account.json` records gross BUY fills totaling 10.78 shares. Dividing the recorded BUY fees by their respective fill prices gives 0.411684 shares; 10.368316 remain after those fees.
- Subtracting the SELL fill of 10.36 leaves 0.008316, which equals the recorded paper-account balance.

The discrepancy is observed; a gross/net boundary mistake is the working explanation, not a completed root-cause finding. S0 also reports account balances below its ledger fill quantities and should be checked through the same trace.

Trace fill/fee normalization, execution reduction, account inventory, strategy position context and settlement-ledger updates. Establish the supported paper fee convention and ensure it is applied exactly once. Do not assume this proves the real provider's fee semantics.

Add a captured-case regression covering a fee-charged BUY, partial/complete sale and residual. Extend to hold/restart and re-entry accounting where the shared code is affected. Fix the smallest owning boundary; do not patch reports just to hide the difference.

**Pass:** gross fills, fees, credited net shares, confirmed sold shares, true residual, cost basis and ledger/account state reconcile under a declared rounding/dust rule. Re-entry still respects cumulative debit; sale proceeds do not reset it.

### F2 — Explain and resolve the two decision mismatches

Reuse the original reconstruction reports and captures first:

- **S1:** 1,077/1,078 decisions matched. On window `1792502400`, the captured decision is `EDGE_BELOW_COST` with a probability; reconstruction is `MODEL_NOT_READY` with null probability/sigma.
- **S3:** 1,120/1,121 matched. At the end of window `1792505700`, the captured decision has tau about 298.8 seconds, while reconstruction has tau about -0.68 and missing inputs.

Trace window binding/promotion, captured input prefix, clock snapshot, scheduler order and indicator warm-up. Classify each discrepancy as a runtime, capture, or reconstruction defect before fixing it. A boundary error can matter even when only one evaluation differs.

**Pass:** a deterministic regression reproduces each cause; a corrected comparison on the same evidence accounts for every declared evaluation and field. Do not silently discard boundary rows, inject recorded probabilities, or widen tolerances to produce equality. If historical evidence cannot support exact comparison, report its precise limitation and obtain a new capture under a fixed contract.

### F3 — Support host-originated protection orders in reconstruction

The S2-protection sale window `1792504800` is refused with `feedback has no preceding captured intent`. The exit was created by the host protection overlay, not by strategy evaluation.

Trace what the existing raw/journal evidence actually contains. Support an explicitly evidenced host-originated intent/order and its feedback, with origin/causal identity and ordering. Extend capture where evidence is genuinely absent. Do not fabricate a strategy signal to satisfy a strategy-only assumption.

**Pass:** a protection-order regression and supported captured sale-window reconstruction preserve decision/position context and order origin. Verify other host exit sources only where they share the affected boundary. Re-run comparable non-sale windows to detect regressions.

### F4 — Obtain a complete S0 hold/restart capture

The old S0 raw context cannot be manufactured retrospectively. After F1 and relevant reconstruction fixes:

- Use the same reachable hold settings in a new isolated paper account/ledger/output location. Enable the raw trading context needed by reconstruction and confirm that the recording setup produces it before scoring the run.
- Set the protocol before launch: at most three counted market windows or 20 minutes wall clock, whichever comes first. Define window counting explicitly; the older duration-based runs bound a fourth window at termination. Count startup and intentional restart inside the same wall-clock bound.
- Obtain entry/fill, hold commitment, a controlled restart on the same account/ledger, and rollover while obligations remain tracked. Use the host's controlled stop; test abrupt-crash recovery separately where needed.
- Keep summaries and evidence for both sides of restart. Compare fee-adjusted quantities and permit only causally explained settlement changes.

**Pass:** the intended events occur, inventory reconciles, the restarted runner tracks the hold and continues as configured, and the complete capture reconstructs under the declared contract. If the market offers no fill, retain the run and leave the branch open; do not extend it indefinitely.

### F5 — Close or explicitly retain the optional event gaps

Use deterministic host-path fixtures for precise or rare conditions. Plan additional bounded paper attempts only where operational evidence is still required:

- Retry actually leading to another permitted attempt/POST, including restored timing and ceilings. Existing paper backoff/eligibility refusal is useful but not that event.
- Point-value strategy sale and fee-adjusted remaining shares.
- Residual commitment at the managed-exit cutoff.
- Uncertain exit, with no competing sell or premature re-entry.
- Re-entry after confirmed flat, correct episode identity, cooldown/side rules and cumulative window debit.

Reuse the cookbook setups. Any adjustment to reach a condition is a named, justified verification overlay fixed before the run; it is not profit optimization. An absent event remains unverified. Qualify optional capabilities individually rather than treating all five setup files as blanket proof.

**Pass:** each item has retained appropriate evidence, or an explicit deferred status and the exact evidence still required. A deferred item does not count as validated or automatically block acceptance of an independently proven hold path.

### F6 — Refresh the final evidence and reconnect Step 3/4

After the relevant fixes, run affected automated regressions and appropriate broader execution/research checks on the final pinned build. Validate the actual cookbook/generated profiles again. Preserve old outputs and distinguish historical-behavior reconstruction from corrected-code diagnostics.

Update the Step 1 inventory and Step 2 execution record with: expected/observed outcomes, source/config/data identities, test skips/failures, comparison fields/tolerances, artifact paths and remaining limitations. Link accepted evidence into Step 3/4; remove a paper-operation gate only for the capabilities now demonstrated.

**Pass:** no known accounting/reconstruction issue is presented as resolved without evidence. The hold workflow has its required proof. Optional features retain their own status. UI labels agree with the inventory and actual mode/ownership.

## How Step 3/4 proceed meanwhile

- Proceed with operation contracts, read-only library, pinned data selections, offline jobs/resume, saved-result adapters, candidate/profile generation and their interface.
- Preserve distinctions between input selection and results, request and fill, gross and net shares, observed and qualified, and paper and real orders.
- Mark affected historical accounting/reconstruction views provisional or inconsistent. Rendering old evidence is not endorsing its correctness; avoid using affected metrics for automatic promotion.
- Paper-control contracts and interface layouts can be prepared. Their end-to-end execution/restart/reconstruction acceptance remains open; a demo or fixture must be labeled as such.
- Final offline/IHM delivery can precede this follow-up. Full Step 3/4 trading acceptance cannot.

## Resume instruction

When the user returns to this work, start at F0, then prioritize F1–F3 before new market sessions. Run F4 only after the required fixes and recording prerequisites hold. Work through F5 by capability, finish with F6, and report what is accepted versus still open. Do not restart all old runs or launch real-provider checks automatically.

Real venue submission latency, provider fee qualification and redemption remain separate, explicitly scoped work. They are not part of this deferred paper follow-up.
