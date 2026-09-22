# R7 — Bounded release-validation campaign

**Status:** Campaign plan accepted. **Offline layer is the current gate.** Live layer C is **not started** and must not run until this revision’s offline checks are green. Do not execute `--live` or R7 processes from this implementation task.
**Depends on:** R1–R5 implemented and offline-green. **Does not depend on R6.**

This is not a runtime feature. After live processes, update the capability table in this file (passed / failed / pending / excluded) against the **release git sha**.

## Three acceptance layers (do not collapse)

| Layer | Passes when | Does not pass when |
|-------|-------------|--------------------|
| **A. Campaign plan accepted** | This file + R1–R5/R6 docs are internally consistent (planning pass). | Implementation not started is fine; this layer is docs-only. |
| **B. Offline validation passed** | Release sha: `--validate-config`, R4 `eval` packs, `pytest -q` green. | Any failing test; missing R4 packs; YAML still `tau_min_s=10`. Offline is **not** live proof. |
| **C. Live-release validation passed** | Required rows in the capability table are `passed` (or `excluded` where this file says excluded). | Any **required** row still `pending` or `failed`. |

Required evidence still pending means **layer C has not passed**. Do not treat plan acceptance or offline green as a live release.

A filled round trip requires **confirmed entry evidence and confirmed exit evidence** (R1 attributed fills, R5 `observed_fills_complete`), not merely successful POST responses (`MAKER_LIMIT_POSTED` / `SubmissionResponseObserved` accepted).

## Historical evidence (corrected)

In-repo `grounding_fixes/README.md` (dated 2026-09-07) still says G3 SL proof is outstanding and cites `…190959Z` as G1+G2. `stop_loss_live_validation.md` records 2026-08-24 TP/trail without `PROTECTION_SL`.

**Operator-saved 2026-09-07 and 2026-09-08 runs (not in git `var/`):** include **stop-loss proof**. Treat as **prior-revision** live evidence of the overlay, **not** proof of the first-release sha.

**Maker tiny-live:** received **fill evidence** and the session **remained unresolved** (not a clean unfilled cancel). That is prior-revision evidence of R1’s gap (attribution/completion), not a successful maker-BUY→flat on this sha.

Do **not** claim: “maker was only POST+cancel with no fill.” Do **not** claim: “SL never printed live.”

## Locked campaign constraints

| Knob | Value |
|------|--------|
| YAML | `config/runs/z_gap_tiny_live.yaml` (R2 `tau_min_s` ≥ 105, $5, resting GTD TP **on**) |
| Mode | One-shot `--live` only (no `--continue`) |
| Family | `btc_updown_5m` |
| Concurrent processes | 1 |
| **Total campaign allowance** | **3** live processes **including retries**. Combined intended entry debit **≤ 15 USDC** (3 × $5). A retry after MANUAL **counts**. Stop the campaign when 3 processes have started **or** cumulative entry POSTs would exceed 15. **Do not start a fourth process** because evidence is still pending. |
| State | Same `state_directory`; **never delete** |
| Interrupt | First Ctrl+C only, **while the runtime is still in the market-data loop**. Wait for R1 drain + R5 report. Kill -9 is a **failed** interruption test, not a pass. |

Exhausting the campaign without required live evidence produces **pending** or **failed** on layer C, not an automatic extension.

## Offline (required before any `--live`) — layer B

On the release sha:

```text
python -m tyrex_pm.application.cli run --config config/runs/z_gap_tiny_live.yaml --validate-config
python -m tyrex_pm.application.cli eval --config config/runs/z_gap_tiny_live.yaml --fixture tests/fixtures/lab/z_gap_entry_eligible.json
python -m tyrex_pm.application.cli eval --config config/runs/z_gap_tiny_live.yaml --fixture tests/fixtures/lab/z_gap_no_entry_eligible.json
python -m tyrex_pm.application.cli eval --config config/runs/z_gap_tiny_live.yaml --fixture tests/fixtures/lab/z_gap_inputs_invalid.json
python -m tyrex_pm.application.cli eval --config config/runs/z_gap_tiny_live.yaml --fixture tests/fixtures/lab/z_gap_exit_held.json
pytest -q
```

Record sha, pytest summary, `source_config_sha256`.

Offline fixtures are **not** live execution proof.

## Live command (exact)

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/z_gap_tiny_live.yaml \
  --dotenv .env \
  --out-dir var/runs/z_gap/release_validation/<shortsha>_p<n> \
  --live
```

`<n>` in 1..3. `--out-dir` must not already contain `run_summary.json` (R5).

## Interruption scenarios (precise)

Run I1 **while feeds and the coordinator are still up**, not after `run_summary.json` has started printing.

| ID | When | Pass |
|----|------|------|
| I1 | Ctrl+C during wait-for-entry / 1 s loop sleep, runtime still active, **no** execution session | R1 drain no-ops; report written; `no_entry_diagnosis` allowed; `prior_session_risk` honest (R5) |
| I2 | Ctrl+C **during** working resting TP or working entry (if any) | R1 drain; terminal no-fill/flat/manual; gateway closed after drain |
| I3 | Ctrl+C **during** flatten POST | bounded **shared** exit budget; not a second BUY; report not missing `run_instance_id` |
| I4 | Second Ctrl+C / kill | **failed** campaign discipline if used to “clear” hang — record as fail, do not delete sqlite |

I1 is the **required** interruption proof (runtime active, no session is acceptable). I2 is required **if** a sitting order exists; if none of the three processes has a sitting order, I2 is **pending**, not silently passed.

## No-entry vs submitted-unfilled vs filled round trip

These are **different** capabilities. Do not score them as one row.

| Capability | Live pass | Not a pass |
|------------|-----------|------------|
| **No-entry, no execution session** | `state is None`, `COMPLETED_NO_ENTRY_SIGNAL` / strategy-input blocked, R5 no-trading meaning, `prior_session_risk=false` (or documented true leftover, in which case this is **not** a clean no-entry pass) | Submitted session scored as “no entry”; empty fills + complete=true on MANUAL |
| **Submitted but unfilled** | A submission **ended unfilled**; `COMPLETED_NO_FILL` with R1 four-part proof and R5 complete=true | Ambiguous POST; timeout without proof; POST-accepted-only; a POST that later filled (that is the round-trip row, not this one) |
| **Filled round trip** | Confirmed BUY **and** confirmed SELL (or host flatten/SL / TP fill flattening to `COMPLETED_FLAT` / dust) with `observed_fills_complete` | POST accepted without confirmed fills; lab R4 connected test; no-entry windows |

**Layer C requires a confirmed live round trip.** If the bounded 3 processes produce no confirmed entry+exit, layer B may be passed and layer C is **pending** — live-release is **not** approved. Do **not** retune theta/z. Do **not** substitute lab fixtures as live proof. Do **not** add a fourth process or raise the debit cap.

## Resting take-profit live fill — locked decision

**A live resting-TP fill is not required for layer C** of this first release.

**Why:** the campaign YAML keeps `take_profit.style: resting_gtd`, but a TP fill needs both a confirmed entry and a favorable move inside a 5m window. Three processes and $15 cannot force that event. Requiring it would force budget extension or theta retune, both forbidden.

**What is required instead (retained resting-TP scope):**

1. Offline R1 F2/F13 (maker-SELL attribution + cancel-then-FAK) — layer B.
2. If a live process confirms an entry: a resting TP **POST** must be observed (`RESTING_TAKE_PROFIT_POSTED`) **or** host flatten/SL must complete the position under R1. A POST-only TP with leftover live SELL at process end is **failed** (R1 drain).
3. If the TP **does** fill inside the three processes: score “live resting TP fill” `passed` using attributed SELL evidence (upgrade). If it does not: that row stays **pending** and **visible**. That pending TP-fill row does **not** by itself fail layer C; a missing confirmed round trip does.

**Remaining limitation:** live maker-SELL **fill** attribution may be unproven on this sha. Offline fixtures carry that behavior. Do not claim live maker-SELL fills passed unless they occurred.

## Capability classification (update after campaign)

Copy this table into the `--out-dir` notes and tick **one** of passed / failed / pending / excluded per row for **this sha**.

| Capability | Layer | Required for C? | Class | Notes |
|------------|-------|-----------------|-------|--------|
| Offline pytest + validate + R4 eval packs | B | yes (before live) | | must be **passed** before live |
| R5 reproducibility fields on a live report | C | yes | | |
| One-shot start/inspect/stop while runtime active (I1) | C | yes | | |
| Honest no-entry **without** a session | C | opportunistic if it occurs | | not submitted-unfilled |
| Honest submitted-unfilled (`COMPLETED_NO_FILL` proof) | C | yes **when a submission ends unfilled**; else N/A | | not every POST |
| Live filled round trip (confirmed BUY **and** confirmed exit flatten/SL/TP fill) | C | **yes** | | pending after 3 processes ⇒ **C not approved**; no extra process |
| Position completed after live entry (resting TP POST then R1 drain, **or** host flatten/SL) | C | yes **if** a live entry filled; else pending with the round-trip row | | not “TP POST on every fill” |
| Live resting TP **fill** attributed SELL | C | **no** (pending allowed; see decision above) | | |
| Live SL FAK on this sha | C | **excluded** | | prior 7/8 proof; not this sha’s required bar |
| Maker **BUY** ask70 YAML | C | **excluded** | | not the release YAML |
| R1 drain-before-close (offline F17 / F-INT-*) | B | yes | | live I2 if sitting order |
| Prior-session inspect / no sqlite delete | C | yes if leftover exists; else N/A passed via I1 | | |
| R2 cutoff (no BUY inside flatten) | B | yes (D6/D10); live opportunistic | | |
| R3 ewma reconnect | B | yes (V*); live pending unless disconnect observed | | |
| F-L2 L2 / feature_probe freshness | — | **excluded** (deferred) | | |
| `--continue` multi-window | — | **excluded** (R6 deferred) | | |
| q_edge / perp / other families | — | **excluded** | | |

Failed = contradictory live report (e.g. MANUAL after claiming no-fill complete; duplicate BUY; deleted state; TP left live after drain).

Pending required-for-C rows at the end of 3 processes ⇒ **layer C not passed**.

## Process budget example

1. Process 1: no session, I1 interrupt → counts 1/3, $0 posted.
2. Process 2: entry POST then no-fill or flat → counts 2/3, ≤$5.
3. Process 3: retry or another window → 3/3 **stop**. If there is still no confirmed entry+exit, layer C stays **pending** (offline validated only).

If process 1 is MANUAL with leftover, **do not** start process 2 until R1 recover on the **same** sqlite; that recover run **is** process 2.

## Campaign log

Per process, copy from report only: sha, dirty, both config hashes, outcome, mutation_attempts, observed_fills, observed_fills_complete, prior_session_risk, reconciliation_complete, residual (or null), errors. Stop reason. Session present? yes/no.

## Acceptance of the campaign design (layer A — this planning pass)

1. Layers A/B/C are distinct.
2. ≤3 processes including retries, ≤$15 intended, one-shot only; no auto-extend.
3. No-entry without session ≠ submitted-unfilled ≠ filled round trip.
4. Filled round trip (confirmed entry **and** exit) is **required** for layer C; three dry runs leave C pending.
5. After a live fill, complete the position via resting TP POST + R1 drain **or** host flatten/SL (one rule). Live resting-TP **fill** stays optional and visible as pending.
6. I1 runs while the runtime is active.
7. History table matches 7/8 SL proof + unresolved maker fill.
8. Every required capability has exactly one class after the campaign.

## Exclusions

R6; F-L2; raising $5; `feature_probe` `emit_entry: true`; claiming prior ask70 fills as this sha’s z_gap BUY→EXIT; deleting sqlite; fourth live process because evidence is pending.
