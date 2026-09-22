# R4 — Z-Gap workflow fixtures

**Status:** Implemented (2026-09-20). **First-release required.** Four lab packs under `tests/fixtures/lab/z_gap_*.json` use shipped YAML economics. Connected test drives real `evaluate()` → lifecycle → attributed BUY → held exit `evaluate()` → SELL → flat. ZL1 keeps unresolved submission distinct from proven no-fill. `configure_ptb` remains optional.
**Depends on:** R1 (attribution, no-fill proof, uncertain POST, drain), R2 (effective cutoff in loaded YAML / evaluate tau / `entry_allowed`), R3 (ewma ready / invalidate).
**Reuse:** `tyrex_pm.lab.evaluate_replay`, `make_fixture_ptb` + duck-typed `configure_ptb`, `ExecutionLifecycle` + in-memory journal + fake gateway (`test_maker_entry.py` pattern). Lab must **not** import `polymarket.gateway`.
**Bounded scope:** prove the **actual** strategy path for `z_gap`. Not a backtester. Not a requirement that every plugin expose PTB setup.

## Current behavior (verified)

`evaluate_replay` (`lab/evaluate_replay.py`) creates a driver via `plugin.create_driver` then **never** calls `configure_ptb`. `ZGapDriver.ptb` stays `None`; `assemble_zgap_decision_snapshot` sets `K=None`. `DecisionContext.references` is **not** passed (defaults empty). Putting `sealed_k` only on `ReferenceFacts` would still leave `ptb_store` empty — `evaluate()` uses `self.ptb_store.observe(self.ptb)`, not `context.references.sealed_k` as K.

`_position` requires `context.lifecycle.instrument_id` **and** `position_quantity > 0`. Replay sets neither. Exit packs cannot emit strategy exits.

Current `z_gap_trades.json` + test only assert action ∈ SKIP/HOLD/WAIT/BLOCKED. That is not entry/no-entry/invalid/exit.

OMS tests are sliced (uncertain POST, G4, maker POST, dust). None consume an **actual** `EnterIntent` / `ExitIntent` returned from `ZGapDriver.evaluate()`.

`COMPLETED_NO_FILL` vs ambiguous: `entry_authoritatively_unfilled` vs `has_unresolved_mutation_attempt`. R4 tests must not treat timeout/ambiguous as no-fill (R1 F14).

`StrategyDriver` protocol has no `configure_ptb`. Only `ZGapDriver` defines it. `ask70` / `feature_probe` drivers do not. Lab must duck-type: call `configure_ptb` only if present **and** the fixture supplies PTB.

## Owning modules

| Layer | Module |
|-------|--------|
| Lab packs | `tests/fixtures/lab/z_gap_*.json`, `evaluate_replay.py` |
| PTB | `domain/polymarket/ptb.py` `make_fixture_ptb`; optional `driver.configure_ptb` |
| Position | `DecisionContext.lifecycle` + `position_quantity` + `position_cost_total` |
| Connected OMS | `tests/unit/execution/test_zgap_connected_workflow.py` (new) calling **real** `evaluate()` then `lifecycle.submit_entry` / `submit_exit` |

## Proposed behavior (smallest change)

Preserve this path only:

```text
fixture inputs → plugin.create_driver → (optional configure_ptb) → real evaluate()
  → typed intent → ExecutionLifecycle.submit_* → coordinator.submit → fake venue
  → attributed fill (R1) → confirmed position → evaluate() again → exit intent
  → confirmed terminal
```

1. Extend `lab_evaluate_v1` with optional `ptb` object and optional `position` / `lifecycle.instrument_id`. On load: `configure = getattr(driver, "configure_ptb", None)`; if callable **and** fixture has `ptb`, call it **before** evaluate steps. Fixtures without PTB, and plugins without the method, keep working (`feature_probe` / `ask70` replay).
2. Pass `lifecycle`, `position_quantity`, `position_cost_total`, `references` (Chainlink raw if present) into `DecisionContext`. Host-style `entry_allowed` must reflect R2 cutoff when the pack is used as a live-window analogue.
3. Four z_gap packs + tests with exact actions. Do not stub `EligibilityFacts(strategy_inputs_eligible=True)` in the connected test.
4. One **connected** test: evaluate ENTER → `lifecycle.submit_entry` → evaluate EXIT with held lifecycle → `submit_exit`.
5. Lifecycle cases that distinguish proven no-fill from unknown submission — fake gateway, not a bypassed reducer.

Do not mock `submit_entry` to return a phase. Do not open an OMS session with a hand-built intent **in the connected test**.

## Lab packs (locked names)

Warm ewma with enough `binance.spot.trades` steps for YAML `min_samples_s: 20` (and sample_interval 1 s) **or** seed via graph the same way production seeds — prefer enough timestamped trades in the JSON. Tau must satisfy R2 effective cutoff (event_end − step.ts ≥ cutoff; shipped 105 s unless the pack sets a stricter plugin tau_min) for **entry** packs.

| File | PTB | Position | Expect |
|------|-----|----------|--------|
| `z_gap_entry_eligible.json` | `configure_ptb` canonical K, books with a real edge under **current** YAML theta/z | flat, lifecycle None | `ENTER`, ≥1 `EnterIntent`, `strategy_inputs_eligible=true` |
| `z_gap_no_entry_eligible.json` | same PTB/vol/tau, books **without** edge | flat | SKIP/WAIT, eligible true, **0** intents |
| `z_gap_inputs_invalid.json` | omit PTB **or** empty books **or** insufficient trades (σ not ready) | flat | eligible false, named blockers, 0 enter |
| `z_gap_exit_held.json` | PTB configured | `position_quantity>0`, `lifecycle.instrument_id` = yes or no matching the held book, `position_cost_total` set | `ExitIntent` or `FlattenIntent`, not ENTER |

Do not weaken `z_gap_tiny_live.yaml` theta/z to make entry green. Vary S, K, and asks only.

Replace the smoke assertion in `test_evaluate_replay.py`; keep `test_lab_module_does_not_import_gateway`. Existing `feature_probe` / ask70 lab tests must still run without a PTB key.

## Connected evaluate → execution (required)

One pytest (asyncio) with fake gateway:

1. Build driver as lab does; `configure_ptb` via duck-type; graph trades; `evaluate()` → `EnterIntent`. If evaluate does not enter, **fail the fixture**, do not skip into OMS.
2. `lifecycle.open` + `submit_entry(intent)` with gateway POST accepted + REST/stream **owned** taker fill (R1 F3).
3. Rebuild `DecisionContext` with `_lifecycle_view(state, instrument_id=held)` and `position_quantity=state.confirmed_position_shares`.
4. `evaluate()` again on a fixture that must exit (rich/thesis/time — pick **time** with tau ≤ `flatten_before_event_end_s` **only if** that is still reachable **after** R2; if host flatten would fire first in live, use a **thesis/rich** book move instead so the **strategy** emits the intent).
5. `submit_exit(that intent)` → confirmed SELL → `COMPLETED_FLAT`.

Assert: BUY POST=1, SELL POST=1, intent `reason_code` from evaluate, no second BUY, eligibility was true on the entry step.

If time-exit cannot fire because host mandatory flatten is 90 s and strategy flatten is 20 s: **do not** pretend. Use realization/thesis on the held bid. The shipped `z_gap_exit_held.json` pack evaluates at τ=180 s and emits `MARKET_RICH_EXIT` (bid 0.99 vs model p≈0.97, richness ≥ `theta_rich` 0.02). That is reachable in production before `mandatory_exit_before_end_s`. Host flatten at 90 s remains a separate path.

## OMS fixtures (R1 semantics, z_gap intents)

May use a **recorded** `EnterIntent`/`FlattenIntent` with z_gap reason codes **plus** the connected test above. Additional rows:

| ID | Sequence | Outcome |
|----|----------|---------|
| ZL1 | Uncertain POST, REST no order, baseline balance, no trades, proof loop | `COMPLETED_NO_FILL` **only if** R1 four-part proof. Timeout / missing REST / ambiguous attempt → `MANUAL_INTERVENTION` or still `RECONCILING`, **never** labeled proven no-fill. |
| ZL2 | Uncertain POST, later owned fill | flatten that size; BUY=1 |
| ZL3 | Partial fill 40% | SELL legal remaining; BUY=1 |
| ZL4 | Sqlite restore `POSITION_OPEN` | flatten; same session_id; no new BUY |
| ZL5 | Maker TP SELL live (R1 F13) if protection posted | cancel+FAK; attributed SELL clip |

**Unknown vs proven no-fill (locked):** a missing venue response, `SubmissionFailed(ambiguous=True)`, or recon timeout is **unknown**. `COMPLETED_NO_FILL` requires the four-part proof. Tests must assert the phase, not only “no confirmed shares.”

## Failure cases

Lab missing PTB on a **z_gap entry** pack → invalid pack (not “no signal”). Connected test: if evaluate does not enter, the test **fails** (fixture wrong). Hand-built intents belong only to ZL* OMS rows, not to the connected proof.

## Acceptance

1. Four packs: enter / eligible skip / invalid / held exit; PTB via duck-typed `configure_ptb`.
2. Exit pack supplies lifecycle instrument + quantity.
3. Connected test: real z_gap `evaluate()` intent consumed by `ExecutionLifecycle`; eligibility checks not stubbed.
4. ZL1 distinguishes authoritative no-fill vs unresolved.
5. Non-z_gap lab replay still works without `configure_ptb`.
6. `pytest tests/unit/lab tests/unit/execution -q` without network; lab file grep still has no gateway import.

## Exclusions

Backtester; ask70 as the complete workflow; `feature_probe` live; weakening theta/z; bypassing reducer with “set phase COMPLETED_FLAT”; requiring `configure_ptb` on the `StrategyDriver` protocol.
