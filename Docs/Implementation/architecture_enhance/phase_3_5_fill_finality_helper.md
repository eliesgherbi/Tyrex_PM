# Phase 3.5 — Fill finality helper

**Program:** [README.md](README.md) · **Prev:** [phase_3_execution_planner.md](phase_3_execution_planner.md) · **Next:** [phase_4_protection_engine.md](phase_4_protection_engine.md)

> **Procedural now, event-ready later.** A pure finality helper; no bus. **Hard prerequisite for Phase 4.**

---

## 1. Goal

Ship the **small fill-finality helper only** — no portfolio, no PnL. Centralize trade-status semantics so that "is this BUY real enough to act on?" has one authoritative answer. This unblocks Phase 4: protection must register **only after ownership is final**, and that boundary is defined here.

Deliverables are intentionally narrow:

```text
state/fill_state.py
  FillFinality (enum)
  classify(status) -> FillFinality
  is_execution_evidence(status) -> bool
  is_position_final(status) -> bool
  is_allocation_final(status) -> bool
  releases_reservation(status) -> bool
  counts_for_realized_pnl(status) -> bool
```

Plus the finality table documented in `LIVE_ARCHITECTURE.md`.

---

## 2. Why this phase exists (and why before Phase 4)

The original roadmap put all of fill-finality + portfolio in a single late phase. That is misleading: **protection registration depends on knowing when a BUY is real**. If protection registers on submit-ack or MATCHED-only evidence, it can monitor a position that does not really exist yet and fire a stop against phantom inventory.

So the **helper** moves before Phase 4; the **portfolio foundation** stays after (Phase 5). You do not need full portfolio before protection — but you do need the finality boundary.

Locked rule:

> **Protection registers only after allocation/position ownership is final according to the fill-finality helper (`allocation_buy_applied`).**

Today the rules (MATCHED/MINED record evidence, CONFIRMED updates position/allocation) are **implicit and scattered** across `user_stream.py`, `reconcile.py`, `positions_sync.py`, `shadow_wallet.py`, and `allocation_exit_lifecycle.py`. This phase makes them one helper.

---

## 3. Current state

```text
core/models.py            → TradeFillRecord (status: MATCHED/MINED/CONFIRMED; positions on CONFIRMED)
ingestion/user_stream.py  → status in (MATCHED,MINED,CONFIRMED) recorded; CONFIRMED updates position
state/reconcile.py        → cumulative fill counts MATCHED/MINED/CONFIRMED for provisional repair
state/wallet_store.py     → record_user_ws_trade; trade_fill_records list
state/shadow_wallet.py    → shadow CONFIRMED → position
runtime/allocation_exit_lifecycle.py → applies allocation on status == CONFIRMED
runtime/allocation_runtime.py        → allocation buy/sell/reserve hooks
```

`RETRYING` and `FAILED` are **not** explicitly modeled in the bot's status set today (the Polymarket trade lifecycle lists them). They must be handled — never treated as final ownership.

---

## 4. Target state

```text
state/fill_state.py
  classify(status) -> FillFinality
  is_execution_evidence(status) -> bool      # MATCHED, MINED, CONFIRMED
  is_position_final(status) -> bool          # CONFIRMED
  is_allocation_final(status) -> bool        # CONFIRMED
  releases_reservation(status) -> bool       # CONFIRMED (applied) ; FAILED (release, no apply)
  counts_for_realized_pnl(status) -> bool    # CONFIRMED only
```

Documented finality table (single source of truth, mirrored in `LIVE_ARCHITECTURE.md`):

| Status | Execution evidence | Position-final | Allocation-final | Releases reservation | Realized PnL |
|--------|:--:|:--:|:--:|:--:|:--:|
| `MATCHED`   | yes | no  | no  | no  | no  |
| `MINED`     | yes | no  | no  | no  | no  |
| `CONFIRMED` | yes | yes | yes | yes (applied) | yes |
| `RETRYING`  | no  | no  | no  | no  | no  |
| `FAILED`    | no  | no  | no  | yes (release, no apply) | no |

Existing status call sites are refactored to route through the helper **without changing effective behavior** for MATCHED/MINED/CONFIRMED; only new explicit handling for RETRYING/FAILED is added.

---

## 5. Non-goals

- **No `portfolio/` package** — that is Phase 5. This phase is the helper only.
- No PnL engine.
- No change to **when** positions update (still CONFIRMED) — this phase documents and centralizes, it does not move the trigger.
- No new venue calls; consumes the existing `TradeFillRecord` stream.
- No event bus.

---

## 6. Files likely to change

```text
src/tyrex_pm/ingestion/user_stream.py              # use classify() for status handling; add RETRYING/FAILED
src/tyrex_pm/state/reconcile.py                    # fill-evidence checks via helper
src/tyrex_pm/runtime/allocation_exit_lifecycle.py  # apply/release via finality helper
src/tyrex_pm/runtime/allocation_runtime.py         # reservation release on FAILED via helper
src/tyrex_pm/core/models.py                        # TradeFillRecord status note: add RETRYING/FAILED
Docs/LIVE_ARCHITECTURE.md                          # finalize finality table (stub added in Phase 0)
Docs/reporting_fact_model.md                       # optional fill_finality field on existing facts
```

---

## 7. New files likely to be added

```text
src/tyrex_pm/state/fill_state.py
tests/test_fill_finality.py
```

> No `portfolio/` files here — see Phase 5.

---

## 8. Data model changes

```python
# state/fill_state.py
class FillFinality(str, Enum):
    PENDING_MATCH = "pending_match"   # MATCHED
    SETTLING      = "settling"        # MINED
    FINAL         = "final"           # CONFIRMED
    RETRYING      = "retrying"        # RETRYING
    FAILED        = "failed"          # FAILED
```

`TradeFillRecord.status` accepts the full set (`MATCHED|MINED|CONFIRMED|RETRYING|FAILED`). No change to position/allocation triggers beyond routing them through the helper. Unknown/unmapped statuses classify conservatively (treated as non-final, non-evidence) — fail closed.

---

## 9. Config changes

None.

---

## 10. Fact/reporting changes

Prefer **no new high-volume fact**. Enrich existing facts so audits see *why* a position/allocation did or did not update:

- Add an optional `fill_finality` field (derived from `classify()`) to `exit_lifecycle` and `allocation_ledger` facts.
- Do **not** emit a fact per MATCHED/MINED tick.

A dedicated `fill_finality` fact is only added later if a terminal-transition audit gap appears (deferred to Phase 5 if needed).

---

## 11. Tests to add

```text
tests/test_fill_finality.py
  test_matched_not_final_pnl
  test_matched_is_execution_evidence
  test_mined_is_evidence_not_final
  test_confirmed_updates_final_position
  test_confirmed_is_allocation_final
  test_failed_trade_does_not_apply_allocation
  test_failed_trade_releases_reservation
  test_retrying_is_not_final_anything
  test_partial_fill_finality_accounting
  test_unknown_status_fails_closed
  test_allocation_buy_applied_only_on_confirmed     # the boundary Phase 4 relies on
```

---

## 12. Acceptance criteria

- One helper (`state/fill_state.py`) defines MATCHED/MINED/CONFIRMED/RETRYING/FAILED behavior; all status call sites route through it.
- MATCHED is never treated as final ownership or final PnL.
- CONFIRMED is the final ownership state (unless code explicitly documents an exception).
- FAILED releases reservations without applying allocation; RETRYING applies nothing.
- The `allocation_buy_applied` boundary is well-defined and testable — this is what Phase 4 registers protection against.
- Existing MATCHED/MINED/CONFIRMED behavior is unchanged (golden-compared); only RETRYING/FAILED handling is newly explicit.

---

## 13. Migration risks

- **Behavior drift** when centralizing scattered checks. Mitigate: the helper must return exactly today's effective rules for MATCHED/MINED/CONFIRMED; only add RETRYING/FAILED. Golden-compare allocation outcomes before/after.
- **Reservation leaks** if FAILED handling is wrong (over-release or no-release). Test both BUY and exit reservations.
- **Shadow vs live divergence:** shadow instant fills must map to `FINAL` consistently so shadow protection (Phase 4) registers identically to live.

---

## 14. Rollback strategy

- The helper is additive and pure. Land it first (tested in isolation), then migrate call sites **one module at a time** so any regression is isolated and revertible per commit.
- Reverting the helper commit restores inline status checks; no runtime contract depends on the enum externally.

---

## 15. Dependencies on previous phases

- **Phase 1** (generic dispatch) — not strictly required by the helper itself, but the helper should be in place before Phase 4 consumes it. Can proceed in parallel with Phase 2/3.

Provides for: **Phase 4** (the `allocation_buy_applied` registration boundary) and **Phase 5** (portfolio foundation builds on the same finality rules).

---

## 16. Event-ready design notes

`classify(status)` and the `is_*` / `counts_for_*` predicates are pure functions of a status string — reusable by any caller, procedural or event-driven. A future bus delivering `TradeUpdate` events would call the same helper to decide side effects. No bus added.
