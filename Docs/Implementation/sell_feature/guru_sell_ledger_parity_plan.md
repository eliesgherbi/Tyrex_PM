# P5 — Ledger-aware guru SELL parity

**Status:** **Implemented** (2026-05-21). Shadow/unit tests in `tests/test_guru_exit_allocation_sizing.py`. **Live guru validation still required** before P6.  
**Prerequisite:** P4 allocation ledger + P4.1 resting SELL lifecycle + P4 auto-pricing validation (**complete**).  
**Green validation reference:** `allocation_test_live_auto_1` (Owner A BUY → allocation credited → Owner B blocked → Owner A auto-priced SELL → ledger clean).  
**Blocks before:** P6 TP/SL overlays (deferred until P5 is stable).

---

## Goal

Make **guru_follow** guru-mirror SELL exits size against the **`guru_follow` owner allocation**, not wallet-wide holdings. The allocation ledger is the single source of strategy ownership truth; guru SELL is always allocation-aware (no wallet-only mode).

---

## Required behavior

| # | Requirement |
|---|-------------|
| 1 | Guru SELL must read the allocation ledger for `owner_id = guru_follow`. |
| 2 | Planned SELL size must be clamped to `get_available_allocated(guru_follow, token_id)`. |
| 3 | Planned SELL size must also respect venue/wallet `available_to_sell` (same as `sell_test`, `scheduled_exit_demo`, `allocation_test`). |
| 4 | Final SELL still goes through `RiskEngine` / `check_inventory_sell` — **no bypass**. |
| 5 | If `allocated_available <= 0`, emit **no** `ExitIntent` / `ReduceIntent` and **no** OMS submit. |
| 6 | Facts must make the sizing/clamp decision visible to the operator. |
| 7 | Strategies must **not** mutate the ledger; runtime remains responsible for mutations. |
| 8 | All exits still flow `ExitIntent` / `ReduceIntent` → `process_intent_work_unit`. |

---

## Current code-path summary (pre-P5)

### A. Where guru SELL size is computed

`strategies/guru_follow/exits.py` → `maybe_exit_intent()`:

1. `bot_qty = holdings.get(token_id)` — wallet position qty passed from pipeline.
2. `guru_scaled = guru_size × copy_scale × conviction_multiplier`.
3. `sell_mode == "full_bot_position"` → `raw = bot_qty`; else `raw = min(guru_scaled, bot_qty)`.
4. Dust filter → `ExitIntent(size=raw)` or skip with `GURU_NO_BOT_INVENTORY` / `GURU_EXIT_BELOW_DUST`.

Called from `GuruFollowStrategy.on_guru_signal()` when `sig.trade.side == Side.SELL`.

### B. Wallet vs allocation today

| Path | Wallet sizing | Allocation clamp |
|------|---------------|------------------|
| Guru mirror SELL (`exits.py`) | Yes (`holdings`) | **No** |
| Scheduled exit demo | Yes (`inventory_snapshot`) | Yes (`clamp_planned_to_allocated`, owner=`guru_follow`) |
| `sell_test` | Yes | Yes (owner=`sell_test`) |
| `allocation_test` | Yes | Yes (per-owner) |

### C. `owner_id` resolution

`runtime/allocation_runtime.py` → `resolve_owner_id()`:

- Explicit `intent_extensions["allocation_owner_id"]` wins.
- `SellTestStrategy` / `source=sell_test_strategy` → `sell_test`.
- `source=scheduled_exit_demo` → `guru_follow`.
- **Default → `guru_follow`** (includes guru-mirror intents with no extension).

Constants in `runtime/allocation_ids.py`: `OWNER_GURU_FOLLOW = "guru_follow"`.

### D. Runtime ledger hooks (already wired)

`pipeline.process_intent_work_unit` for guru intents:

- BUY matched → `maybe_apply_allocation_buy` (credits `guru_follow`).
- SELL approved → `maybe_reserve_exit_allocation`.
- SELL matched / shadow fill → `maybe_apply_allocation_sell`.
- SELL resting → `maybe_note_allocation_exit_order_live`; P4.1 promotes via WS/reconcile.
- Wallet sync → `maybe_clamp_allocations_to_venue`.

Guru mirror SELL intents today reach these hooks **after** intent construction — but intent size may exceed `guru_follow` allocation.

### E. Existing allocation / exit facts

Documented in `Docs/reporting_fact_model.md` under `allocation_ledger`:

- `allocation_buy_applied`, `allocation_sell_applied`, `allocation_partial_fill_applied`
- `allocation_reserved`, `allocation_released`, `allocation_exit_order_live`
- `allocation_clamped` (venue reconciliation)

Exit lifecycle facts (`exit_lifecycle`) cover scheduled/demo/sell_test arming — not guru mirror sizing.

`allocation_test` emits `health` events for unauthorized-sell block visibility (`allocation_test_unauthorized_sell_attempt`, `allocation_test_unauthorized_sell_blocked`).

Guru path emits `strategy_skip` with reason codes (`guru_no_bot_inventory`, etc.) but **no allocation fields**.

---

## Proposed implementation approach

### 1. Extend guru exit sizing seam (minimal diff)

**Primary files:**

- `strategies/guru_follow/exits.py` — add allocation + venue clamp after existing wallet/guru math.
- `strategies/guru_follow/strategy.py` — pass `RuntimeCoordinator` (or a read-only sizing view) into exit sizing.
- `runtime/pipeline.py` — update `process_new_guru_signals` call site; attach sizing evidence to facts.

**Reuse (do not duplicate):**

- `clamp_planned_to_allocated(coord, owner_id=OWNER_GURU_FOLLOW, …)` from `runtime/allocation_runtime.py`.
- `inventory_snapshot(coord, token_id)` from `runtime/exit_lifecycle.py`.
- Existing `resolve_owner_id` default (`guru_follow`) — no change needed for guru mirror intents.

### 2. Sizing algorithm (mirror `allocation_test.build_owner_a_sell_work_unit`)

After computing `raw` from guru/wallet logic:

```
allocated_clamped = clamp_planned_to_allocated(coord, owner_id=guru_follow, planned=raw)
venue_avail       = inventory_snapshot(coord, token_id)["available_to_sell"]
final_size        = min(raw, allocated_clamped, venue_avail)
```

When `final_size <= 0`:

- Return skip with a **new reason code** (proposed: `guru_no_allocated_inventory`) when wallet qty > 0 but allocation is zero — distinct from `guru_no_bot_inventory`.
- Emit operator-visible sizing fact (see below).
- Pipeline writes `strategy_skip`; **no** `process_intent_work_unit` call.

When `final_size > 0` but `< raw`:

- Emit sizing/clamp fact.
- Include clamp evidence in `intent_fact_extensions` on the `IntentWorkUnit` so `intent_created` carries it.

The allocation ledger is always required; there is no wallet-only guru SELL fallback.

### 3. API shape for strategy read access

**Recommended:** extend `on_guru_signal(sig, holdings, *, coord: RuntimeCoordinator | None = None)`.

- Pipeline already has `coord`; pass it through.
- Strategy reads ledger via `coord.allocation_ledger` + existing helpers only — **no mutations**.
- Avoid importing ledger mutation APIs into strategy modules.

**Optional helper** (if exit math gets noisy):

```python
# runtime/allocation_runtime.py (read-only)
def plan_exit_size_against_allocation(
    coord, *, owner_id, token_id, planned, enabled: bool
) -> tuple[Decimal, dict[str, str]]  # (final_size, evidence_fields)
```

This wraps `clamp_planned_to_allocated` + `inventory_snapshot` and returns string evidence for facts. Not required if inline in `exits.py` stays readable.

### 4. Fact payloads (proposed)

#### A. Blocked — insufficient allocation (`health` or enriched `strategy_skip`)

Use `FACT_TYPE_HEALTH` with `event: guru_exit_allocation_blocked` (consistent with `allocation_test` health events):

```json
{
  "event": "guru_exit_allocation_blocked",
  "owner_id": "guru_follow",
  "token_id": "...",
  "planned_size": "10",
  "wallet_position_qty": "10",
  "allocated_available": "0",
  "available_to_sell": "10",
  "reason": "insufficient_allocation",
  "dedup_key": "..."
}
```

Also write `strategy_skip` with `reason: guru_no_allocated_inventory` for summarize/join compatibility.

#### B. Clamped — partial allocation (`health`)

```json
{
  "event": "guru_exit_allocation_clamped",
  "owner_id": "guru_follow",
  "token_id": "...",
  "guru_scaled_size": "10",
  "wallet_position_qty": "10",
  "allocated_available": "3",
  "available_to_sell": "10",
  "final_size": "3",
  "dedup_key": "..."
}
```

#### C. Successful intent — extensions on `intent_created`

```json
{
  "guru_exit_sizing": {
    "owner_id": "guru_follow",
    "planned_before_clamp": "10",
    "allocated_available": "3",
    "available_to_sell": "10",
    "final_size": "3"
  }
}
```

Document all three in `Docs/reporting_fact_model.md` §2.

#### D. New reason code

`core/reason_codes.py`: `GURU_NO_ALLOCATED_INVENTORY = "guru_no_allocated_inventory"`.

---

## Functions / classes to modify (implementation checklist)

| File | Symbol | Change |
|------|--------|--------|
| `strategies/guru_follow/exits.py` | `maybe_exit_intent` | Accept `coord`; apply allocation + venue clamp; return sizing evidence |
| `strategies/guru_follow/strategy.py` | `on_guru_signal` | Pass `coord` to exits; return sizing meta for pipeline |
| `runtime/pipeline.py` | `process_new_guru_signals` | Pass `coord` to strategy; emit health facts on block/clamp; attach extensions |
| `core/reason_codes.py` | `GURU_NO_ALLOCATED_INVENTORY` | New skip reason |
| `Docs/reporting_fact_model.md` | §2 | Document guru exit sizing events |
| `tests/test_guru_strategy_golden.py` | — | Update + new allocation cases |
| `tests/test_guru_exit_allocation_sizing.py` | — | **New** focused unit tests |

**Explicitly out of scope for P5:**

- Changes to `risk/engine.py` inventory gate (unless adding optional read-only evidence — not required).
- Changes to runtime ledger mutation hooks (already correct).
- TP/SL overlays (P6).

---

## Test plan (required before P5 is complete)

### Unit

| Test | Assert |
|------|--------|
| `test_guru_exit_wallet_qty_but_zero_allocation_blocked` | Wallet 10, allocated 0 → no intent, `guru_no_allocated_inventory`, health fact |
| `test_guru_exit_clamped_to_allocation` | Wallet 10, allocated 3, guru wants 10 → ExitIntent size 3, clamp fact |
| `test_guru_exit_respects_venue_available_to_sell` | Wallet 10, allocated 10, in-flight 8 → final size ≤ 2 |
| `test_guru_exit_always_uses_allocation` | Wallet qty > allocation → capped to allocation |
| `test_guru_exit_no_wallet_only_fallback` | Wallet qty > 0, allocation 0 → blocked, no OMS |
| `test_full_bot_position_uses_allocated_position_only` | full_bot_position uses allocated qty only |
| `test_guru_exit_full_bot_position_mode` | `full_bot_position` still capped by allocation when ledger on |

### Integration

| Test | Assert |
|------|--------|
| `test_guru_buy_then_sell_allocation_round_trip` | Shadow: guru BUY credits `guru_follow`, guru SELL debits to 0 |
| `test_guru_sell_with_foreign_allocation` | `sell_test` owns allocation; guru SELL blocked despite wallet qty |
| Extend `test_scheduled_exit_demo` if needed | Confirm demo path still clamps (regression) |

### Manual (post-implementation)

Live or shadow guru_follow run with allocation ledger enabled and a second owner (`sell_test` or manual ledger seed) to confirm guru SELL does not consume foreign allocation.

---

## Architecture risks and open questions

| Risk | Mitigation |
|------|------------|
| **Signature change** `on_guru_signal(holdings)` → `on_guru_signal(coord)` | Pipeline always passes `coord`; strategy reads allocation read-only |
| **Holdings vs allocation divergence** | Expected in multi-bot; P5 makes divergence safe. Document in OPERATIONS.md |
| **Guru SELL before BUY allocation applied** | Same race as sell_test arming; guru path is signal-driven not timer-driven — if allocation 0 at signal time, skip with fact (correct) |
| **`full_bot_position` mode** | Means full **allocated** guru_follow position, not full wallet — documented in `guru_follow.yaml` |
| **Fact duplication** | health + strategy_skip on block is intentional (operator visibility + summarize reason codes) |
| **Who emits facts?** | Prefer pipeline (has sink) over strategy direct writes — matches guru signal path today for `strategy_skip` |

### Resolved by design (no ambiguity)

- Runtime owns ledger mutation — unchanged.
- RiskEngine final gate — unchanged.
- No parallel SELL path — unchanged.
- Decimal arithmetic — unchanged.

---

## Validation reference (P4 green run)

`allocation_test_live_auto_1` demonstrated:

1. Owner A BUY matched → `allocation_buy_applied` credited Owner A.
2. Owner B attempted sell with `wallet_position_qty > 0` but `allocated_available = 0` → blocked, no OMS.
3. Owner A SELL auto-priced, reserved, submitted, matched → `allocation_sell_applied`, ledger clean.
4. No `LEDGER_MISMATCH`.

P5 applies the same Owner B block logic to the production guru mirror path with `owner_id = guru_follow`.

---

## Related documents

| Document | Role |
|----------|------|
| [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) | Master phase tracker |
| [allocation_test_strategy_plan.md](./allocation_test_strategy_plan.md) | P4 validation toy (reference for clamp patterns) |
| [Docs/reporting_fact_model.md](../../reporting_fact_model.md) | Fact payload catalog |
