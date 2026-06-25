# Phase 5 — Portfolio foundation

**Program:** [README.md](README.md) · **Prev:** [phase_4_protection_engine.md](phase_4_protection_engine.md) · **Next:** [phase_6_hard_kill_switch.md](phase_6_hard_kill_switch.md)

> **Procedural now, event-ready later.** Read-only projections; no bus. The fill-finality helper this builds on shipped in [Phase 3.5](phase_3_5_fill_finality_helper.md).

---

## 1. Goal

Lay a **minimal** `portfolio/` foundation: a read-only positions projection and a per-owner attribution scaffold, built on the fill-finality rules from Phase 3.5. **No full PnL engine** — that remains deferred.

```text
portfolio/
  positions_view.py    # read-only projection over WalletStore + AllocationLedger
  attribution.py       # per-owner attribution scaffold (no realized/unrealized PnL math yet)
  # pnl.py             # LATER — explicitly out of scope
```

---

## 2. Why this phase exists

The fill-finality helper (Phase 3.5) defines *which* trade statuses count. This phase provides the first **read-side views** over the resulting truth, so operators and future PnL code have a consistent projection to build on. It is deliberately separated from the helper so that protection (Phase 4) could land without waiting on portfolio work.

> You do not need full portfolio before protection. The split (helper in 3.5, views in 5) reflects exactly that.

---

## 3. Current state

```text
state/fill_state.py            → classify(status) + finality predicates (shipped in Phase 3.5)
state/wallet_store.py          → positions, balances, allowances (venue truth)
state/allocation_ledger.py     → per-owner token qty + exit reservations (ownership truth)
runtime/coordinator.py         → holdings(), marks_for_risk()
```

There is no single read-only object that projects "what each owner holds and what it is worth at current marks". Risk reads stores directly; reporting reads facts. That is fine for execution but thin for attribution.

---

## 4. Target state

```text
portfolio/positions_view.py
  PositionsView(wallet, allocation_ledger, market_state=None)
    by_owner() -> dict[owner_id, dict[token_id, qty]]
    venue_total(token_id) -> Decimal
    owner_qty(owner_id, token_id) -> Decimal
    mark_value(token_id) -> Decimal | None        # uses MarketStateStore mid when fresh
    # pure projection; never a writer

portfolio/attribution.py
  Attribution(positions_view, fill_records)
    realized_fills_by_owner(owner_id) -> list[...]  # scaffold; CONFIRMED-only per finality rules
    # no PnL math yet — structure only
```

`PositionsView` is a **pure projection**: it never mutates `WalletStore`, `AllocationLedger`, or any store. Attribution counts only fills that `state/fill_state.counts_for_realized_pnl(status)` accepts (CONFIRMED).

---

## 5. Non-goals

- **No PnL engine** (`pnl.py` is deferred).
- No new writers — `portfolio/` is read-only.
- No change to fill-finality rules (those are locked in Phase 3.5).
- No new venue calls.
- No event bus.
- Not a prerequisite for Phase 6 (hard kill does not need portfolio).

---

## 6. Files likely to change

```text
src/tyrex_pm/runtime/coordinator.py      # optionally expose a PositionsView accessor
Docs/modules/README.md                   # add portfolio module entry
Docs/reporting_fact_model.md             # (optional) attribution evidence fields
```

---

## 7. New files likely to be added

```text
src/tyrex_pm/portfolio/__init__.py
src/tyrex_pm/portfolio/positions_view.py
src/tyrex_pm/portfolio/attribution.py
Docs/modules/portfolio/README.md
tests/test_portfolio_positions_view.py
tests/test_portfolio_attribution.py
```

---

## 8. Data model changes

No `core` changes. Portfolio types are read-only dataclasses local to `portfolio/`:

```python
@dataclass(frozen=True)
class OwnerPosition:
    owner_id: str
    token_id: TokenId
    qty: Decimal
    mark: Decimal | None
    mark_value: Decimal | None
```

Decimal everywhere; reuse `state/fill_state.FillFinality` from Phase 3.5 — do not redefine finality here.

---

## 9. Config changes

Optional `portfolio.enabled` flag if the views run as a background projection; default off. No other config.

---

## 10. Fact/reporting changes

No new high-volume facts. Optionally enrich `allocation_ledger` facts with an attribution reference. A dedicated portfolio/PnL fact is deferred with the PnL engine.

---

## 11. Tests to add

```text
tests/test_portfolio_positions_view.py
  test_positions_view_matches_wallet_store
  test_positions_view_is_read_only            # asserts no store mutation
  test_owner_qty_matches_allocation_ledger
  test_mark_value_uses_fresh_market_state
  test_mark_value_none_when_book_stale

tests/test_portfolio_attribution.py
  test_attribution_counts_confirmed_only      # honors Phase 3.5 finality
  test_attribution_ignores_matched_and_failed
  test_attribution_per_owner_scaffold
```

---

## 12. Acceptance criteria

- `portfolio/positions_view` is a read-only projection consistent with `WalletStore` + `AllocationLedger`.
- It never writes to any store.
- Mark values use `MarketStateStore` mid when fresh and return `None` when stale.
- Attribution counts only CONFIRMED fills per the Phase 3.5 finality rules; MATCHED/RETRYING/FAILED excluded.
- No PnL math is introduced (scaffold only).

---

## 13. Migration risks

- **Double counting** vs `WalletStore`. Keep it a pure projection, never a second writer; `test_positions_view_is_read_only`.
- **Stale marks** inflating attribution values. Gate mark usage on `MarketStateStore.is_stale`.
- **Scope creep into PnL.** Resist — `pnl.py` stays out until a dedicated later effort.

---

## 14. Rollback strategy

`portfolio/` is additive and read-only and flag-gated. Rollback = remove the projection start-up or revert the commit; zero impact on execution, risk, OMS, or reconcile.

---

## 15. Dependencies on previous phases

- **Phase 3.5** — fill-finality helper (attribution honors it). **Hard prerequisite.**
- **Phase 2** — `MarketStateStore` for mark values (optional; views degrade to qty-only without it).
- **Phase 4** — protection exits are among the flows attribution will eventually observe (not strictly required).

Provides for: a future `pnl.py` and operator attribution dashboards (out of scope here).

---

## 16. Event-ready design notes

`PositionsView` and `Attribution` are pull-based projections built from stores + the pure finality helper. An event-driven version would subscribe to the same underlying records without changing the projection logic. No bus added.
