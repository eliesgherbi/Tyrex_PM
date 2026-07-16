# Phase 3 — ExecutionPlanner

**Program:** [README.md](README.md) · **Prev:** [phase_2_market_state_store.md](phase_2_market_state_store.md) · **Next:** [phase_3_5_fill_finality_helper.md](phase_3_5_fill_finality_helper.md)

> **Procedural now, event-ready later.** A pure `plan(...)` method; no bus.

---

## 1. Goal

Centralize **"how to trade"** in a first-class `ExecutionPlanner` that runs **after** risk approves an intent and **before** the OMS submits. Strategies stop deciding execution style; the planner does, using `MarketStateStore` (Phase 2) and config.

```text
Intent
  → RiskEngine pre-check        (approve the strategy intent)
  → ExecutionPlanner.plan(...)  (choose style, price, size, slippage guard)
  → final planned-order validation (re-check the concrete order)
  → SingleWriterOMS
```

---

## 2. Why this phase exists

Today `OrderStyle` is chosen by strategies/YAML (`guru_follow/sizing.py` → `GTC`; `sell_test`/`tp_sl_test`/`allocation_test` configs set styles). `execution/slippage.py` and `execution/liquidity_guard.py` are advisory stubs. The review flagged this as the **highest-leverage gap**: execution quality and emergency exits depend on per-strategy config instead of live book state.

A planner also closes a safety hole: it must **not** be able to change price/size after risk approval without a second validation. Hence the two-pass design.

---

## 3. Current state

```text
core/enums.py                → OrderStyle: GTC | FOK | FAK   (no GTD, no post-only)
execution/order_builder.py   → to_place_request(ap, market_info) ; floor-quantizes price to tick
execution/slippage.py        → clamp_price() only (advisory)
execution/liquidity_guard.py → empty ("Minimal liquidity checks — expand post-parity.")
execution/router.py          → venue routing stub
runtime/pipeline.py          → process_intent_work_unit: risk → OMS directly (no planner step)
strategies/*                 → set order_style on intents from config
```

Risk gate sequence (`risk/engine.py`) already runs notional → deployment → capital → inventory → venue-min-size on the **intent**. The planner must not duplicate these; it produces a concrete order that the final validation re-checks.

---

## 4. Target state

```text
execution/planner.py
  ExecutionPlanner.plan(approved_intent, ctx) -> ExecutionPlan

execution/models.py            (LOCKED — not core/models.py)
  ExecutionPlan dataclass (see §8)

risk/planned_order.py          (LOCKED — dedicated validator, NOT evaluate_intent re-entry)
  validate_planned_order(plan, ctx, risk_config) -> RiskDecision

runtime/pipeline.py::process_intent_work_unit becomes:
  decision = evaluate_intent(intent, ctx, ...)              # pre-check: approves the INTENT
  if not decision.approved: emit risk fact; return
  plan = planner.plan(decision.approved_intent, ctx)        # how to trade (preserves client_order_id)
  emit execution_plan fact
  final = validate_planned_order(plan, ctx, risk_config)    # final: validates the concrete PLAN
  if not final.approved: emit risk fact (phase="planned"); return
  oms.submit(plan)                                          # OMS receives a VALIDATED ExecutionPlan
```

### Two-pass risk design (locked)

```text
Pre-check approves the intent.
Planner creates ExecutionPlan (reusing the pre-check's client_order_id).
Final validation validates the ExecutionPlan.
OMS only receives a validated ExecutionPlan.
```

**Why a dedicated validator and not `evaluate_intent()` again:** `evaluate_intent()` approves an `Intent` and **mints a fresh `ClientOrderId`** while attaching an `ApprovedIntent`. Re-entering it for a planned order would (a) generate a *second* client order id, breaking correlation, and (b) re-run intent-shaped logic against an object that now has final style/size/price/planner-reason/book-evidence. Instead:

- `risk/planned_order.py::validate_planned_order(plan, ctx, risk_config)` re-checks **only the gates sensitive to the planner's concrete output**: notional, deployment caps, capital (BUY), inventory (SELL), venue min-size, and readiness/drift if the snapshot is stale.
- It **does not** mint a new `ClientOrderId`. The planner **preserves** `decision.approved_intent.client_order_id` on the `ExecutionPlan`.
- It returns a `RiskDecision` whose evidence is emitted as a `risk_decision` fact tagged `phase: "planned"`.

Planner rules (initial, minimal):

```text
passive entry            → GTC at strategy limit price
normal entry             → GTC unless config overrides
urgent / protection exit → FAK at worst_acceptable_price (from market state + max_slippage)
stale or missing book
  for urgent/protection  → fail closed (deny) unless explicit safe fallback configured
```

---

## 5. Non-goals

- **GTD and post-only stay out of Phase 3 (locked).** Phase 3 supports only the existing `OrderStyle` values: `GTC`, `FAK`, `FOK`. GTD and post-only are documented as **future `OrderStyle` extensions**, not Phase 3 work — adding them now would expand scope and distract from the goal of centralizing execution decisions.
- No order slicing / iceberg / cancel-replace algos.
- No multi-venue routing (`router.py` stays a stub).
- No moving tick-quantization or venue-min-size out of their current homes — the planner cooperates with `order_builder` + `risk/venue_min_size`, it does not reimplement them.
- The final validator does **not** mint a new `ClientOrderId` and does **not** duplicate the full `evaluate_intent` sequence.
- No event bus.

---

## 6. Files likely to change

```text
src/tyrex_pm/runtime/pipeline.py             # insert planner + final validation into process_intent_work_unit
src/tyrex_pm/execution/order_builder.py      # consume ExecutionPlan (price/size/style) instead of raw intent
src/tyrex_pm/execution/slippage.py           # real worst-acceptable-price / slippage helpers used by planner
src/tyrex_pm/execution/liquidity_guard.py    # real book-depth sanity used by planner (or fold into planner)
src/tyrex_pm/runtime/coordinator.py          # expose planner + market_state to pipeline
src/tyrex_pm/runtime/config.py               # ExecutionPlannerConfig + planner-enabled⇒market-data-enabled cross-check
src/tyrex_pm/strategies/*                     # stop hard-setting emergency styles; pass urgency hint instead
src/tyrex_pm/core/reason_codes.py            # planner reason codes (see §8)
Docs/modules/execution/README.md             # document planner + two-pass risk
Docs/Architecture.md                          # spine now includes planner (already framed in Phase 0)
Docs/reporting_fact_model.md                  # execution_plan fact + risk_decision phase tag
```

---

## 7. New files likely to be added

```text
src/tyrex_pm/execution/planner.py
src/tyrex_pm/execution/models.py        # ExecutionPlan (LOCKED here — not core/models.py)
src/tyrex_pm/risk/planned_order.py      # validate_planned_order(plan, ctx, risk_config)
tests/test_execution_planner.py
tests/test_validate_planned_order.py
```

---

## 8. Data model changes

**Type placement (locked):**

| Type | Home | Rationale |
|------|------|-----------|
| `ExecutionPlan` | `execution/models.py` | Venue-shaped execution detail; keep `core/` small and stable. |
| `MarketBookSnapshot` | `state/market_store.py` (Phase 2) | Book state lives with its store. |
| `Signal` / `StrategyResult` | `signals/base.py` / `strategies/base.py` (Phase 1) | Strategy I/O contract. |

> Only promote a type into `core/models.py` later if a second module truly needs a venue-agnostic abstract version.

```python
# execution/models.py
@dataclass(frozen=True)
class ExecutionPlan:
    intent_id: IntentId
    client_order_id: ClientOrderId        # PRESERVED from the pre-check ApprovedIntent; planner never re-mints
    token_id: TokenId
    side: Side
    final_size: Decimal
    final_limit_price: Decimal
    order_style: OrderStyle               # GTC | FOK | FAK only (no GTD / post-only in Phase 3)
    urgency: str                          # "passive" | "normal" | "urgent"
    max_slippage: Decimal | None
    worst_acceptable_price: Decimal | None
    planner_reason: str                   # stable code, e.g. "passive_entry_gtc", "urgent_exit_fak"
    market_snapshot_ref: str | None       # token + ts of the book used (for audit)
```

`Intent` variants gain an optional `urgency` hint (default `"normal"`) so strategy/protection can express intent **without** choosing the style. Add planner reason constants to `core/reason_codes.py` (e.g. `PLAN_PASSIVE_ENTRY_GTC`, `PLAN_URGENT_EXIT_FAK`, `PLAN_STALE_BOOK_DENY`).

**Client-order-id rule (locked):** the planner copies `decision.approved_intent.client_order_id` onto the `ExecutionPlan` unchanged. `validate_planned_order` approves/denies the concrete plan but never mints a new id. This keeps a single correlation id across `intent_created` → `execution_plan` → `risk_decision(planned)` → `oms_submit`.

Decimal everywhere; ids via `core/ids`.

---

## 9. Config changes

```yaml
# config/runtime/default.yaml
execution:
  planner:
    enabled: true
    default_entry_style: GTC
    urgent_exit_style: FAK
    max_slippage_default: "0.02"      # 2% worst-case from reference price
    require_fresh_book_for_urgent: true
    stale_book_fallback: deny          # deny | use_limit  (deny = fail closed)
```

`runtime/config.py`: typed `ExecutionPlannerConfig`. When `enabled: false`, the pipeline falls back to current behavior (intent style passes straight through) so Phase 3 can be dark-launched.

### Market-data activation contract (locked)

Phase 2 ships market data **disabled**. Phase 3 owns turning it on and the rule for when fresh book data is mandatory:

| Plan path | Fresh book required? | Behavior if stale/missing |
|-----------|:--:|---------------------------|
| Passive / normal **entry** | No | Use the strategy limit price (the book is advisory). |
| Urgent / protection **exit** | **Yes** | `stale_book_fallback: deny` ⇒ deny (`PLAN_STALE_BOOK_DENY`). `use_limit` ⇒ fall back to the supplied limit only if explicitly configured. |

**Config cross-check (enforced at load in Phase 3):** if `execution.planner.enabled=true` then `market_data.enabled` must also be `true`. Otherwise config load fails closed — this removes the confusing "planner on, market data off" state. Document the dependency in `CONFIG_MODEL.md`.

---

## 10. Fact/reporting changes

Add `FACT_TYPE_EXECUTION_PLAN = "execution_plan"` in `reporting/schema_v2.py`. Payload (join key: `correlation_id` + `client_order_id`):

```json
{
  "intent_id": "...",
  "client_order_id": "...",
  "token_id": "...",
  "side": "SELL",
  "final_size": "100.000000",
  "final_limit_price": "0.52",
  "order_style": "FAK",
  "urgency": "urgent",
  "max_slippage": "0.02",
  "worst_acceptable_price": "0.52",
  "planner_reason": "urgent_exit_fak",
  "market_snapshot_ref": "0xtoken@2026-06-25T08:00:00Z"
}
```

The final planned-order validation reuses the existing `risk_decision` fact but tags `phase: "planned"` so audits can distinguish pre-check vs final check. `oms_submit` should carry the `planner_reason` for traceability.

---

## 11. Tests to add

```text
tests/test_execution_planner.py
  test_passive_entry_plans_gtc
  test_normal_entry_plans_gtc
  test_urgent_exit_plans_fak
  test_urgent_exit_worst_price_from_book
  test_stale_book_blocks_urgent_exit
  test_missing_book_blocks_urgent_exit
  test_passive_entry_works_without_fresh_book     # entries do not require book
  test_planned_order_revalidated_before_oms
  test_planner_changes_do_not_skip_risk           # price/size change forces final validation
  test_planner_preserves_client_order_id          # no second COID minted
  test_planner_fact_emitted
  test_planner_disabled_falls_back_to_intent_style
  test_planner_enabled_requires_market_data_enabled  # config cross-check fails closed
  test_no_gtd_or_post_only_style_accepted         # OrderStyle stays GTC/FAK/FOK

tests/test_validate_planned_order.py
  test_validate_planned_order_rechecks_notional
  test_validate_planned_order_rechecks_deployment_caps
  test_validate_planned_order_rechecks_capital_for_buy
  test_validate_planned_order_rechecks_inventory_for_sell
  test_validate_planned_order_rechecks_venue_min_size
  test_validate_planned_order_does_not_mint_new_client_order_id
  test_validate_planned_order_denies_worsened_price
  test_validate_planned_order_emits_risk_decision_phase_planned
```

---

## 12. Acceptance criteria

- OMS receives a validated `ExecutionPlan`, never raw strategy assumptions (when planner enabled).
- Strategies no longer decide emergency/urgent execution style; they pass an `urgency` hint.
- Final validation runs via the **dedicated `validate_planned_order`** (not `evaluate_intent` re-entry); a planner-changed price/size cannot reach OMS unvalidated.
- The `client_order_id` from the pre-check approval is preserved end-to-end; no second id is minted.
- `execution_plan` fact is emitted; the final `risk_decision` is tagged `phase: "planned"`.
- Stale/missing book for an urgent exit fails closed unless `stale_book_fallback: use_limit`; passive entries work without a fresh book.
- Config load fails closed if `planner.enabled=true` while `market_data.enabled=false`.
- `OrderStyle` remains GTC/FAK/FOK; GTD/post-only are not accepted.
- With `planner.enabled=false`, behavior matches pre-Phase-3.

---

## 13. Migration risks

- **Double risk logic:** planner accidentally re-implementing caps/capital. Mitigate: planner only chooses style/price/size shaping; `validate_planned_order` re-runs **only** the size/price-sensitive gates (notional, deployment, capital, inventory, venue-min-size) by calling the **same per-policy modules** `evaluate_intent` uses — it shares the policy functions, not the orchestrator, so logic is not duplicated.
- **COID drift:** the gravest correctness risk. If the validator re-enters `evaluate_intent` it mints a new `ClientOrderId` and breaks correlation. Mitigate with the locked dedicated-validator design + `test_planner_preserves_client_order_id` and `test_validate_planned_order_does_not_mint_new_client_order_id`.
- **Price worsening:** planner could compute a worst price worse than the strategy limit. Final validation + `order_builder` floor-quantize must keep effective price no worse than intended; assert in `test_validate_planned_order_denies_worsened_price`.
- **Behavior change for guru:** guru BUYs currently GTC at limit — planner must produce the same for `passive/normal` entries. Golden-compare `oms_submit`.
- **Stale-book deadlocks** for protection exits if book never freshens. Phase 4 must handle the deny path operationally (alert), not silently drop the exit.

---

## 14. Rollback strategy

- Feature flag `execution.planner.enabled`. Setting `false` restores direct intent→OMS path.
- Land in two commits: (a) planner + facts behind flag default-off; (b) flip default-on after guru/sell_test golden compares pass. Roll back commit (b) to disable.

---

## 15. Dependencies on previous phases

- **Phase 2** (`MarketStateStore`) — planner needs book state for urgent-exit pricing and staleness.
- **Phase 1** — planner is invoked inside the generic `process_intent_work_unit`, used by all strategies.

Provides for: **Phase 4** (protection exits get correct FAK worst-price execution via the planner) and **Phase 6** (optional emergency exit uses the planner). **Phase 3.5** (fill finality) is independent of the planner and can proceed in parallel after Phase 1.

---

## 16. Event-ready design notes

`ExecutionPlanner.plan(approved_intent, ctx) -> ExecutionPlan` is a pure function of `(approved_intent, ctx.market_state, config)` with no I/O or store mutation. A future event bus could call it on an `IntentApproved` event unchanged. `validate_planned_order(plan, ctx, risk_config) -> RiskDecision` is likewise a pure validation call. No bus, queue, or subscription introduced.

---

## Implementation status

**Status: implemented.**

**Implementation summary.** `execution/planner.py::ExecutionPlanner.plan(approved, market_state, now)` converts a pre-check `ApprovedIntent` into an `ExecutionPlan` (`execution/models.py`). Rules: passive/normal entry → GTC at the strategy limit; urgent/protection exit → FAK at a worst-acceptable price from `estimate_fill_price` (falls back to best bid/ask); urgent exit with stale/missing book → deny (`PLANNER_STALE_BOOK` / `PLANNER_MISSING_BOOK` / `PLANNER_NO_MARKET_DATA`) unless `allow_urgent_exit_fallback` and an intent limit exist. The plan preserves the pre-check `client_order_id`. `risk/planned_order.py::validate_planned_order` re-checks notional, deployment caps, capital (BUY), inventory (SELL), and venue-min-size **without** re-entering `evaluate_intent` or minting a new client order id, and denies planner price-worsening for non-urgent intents. `runtime/pipeline.py` inserts the planner between risk pre-check and OMS when `execution.planner.enabled`, emitting `execution_plan` then a `risk_decision` with `{"phase":"planned"}` and tagging `oms_submit` with `planner_reason`.

**Files changed.** `execution/planner.py` (new), `execution/models.py` (new), `risk/planned_order.py` (new), `core/models.py` (`urgency` on intents + `URGENCY_*`), `core/reason_codes.py` (planner codes), `reporting/schema_v2.py` (`FACT_TYPE_EXECUTION_PLAN`), `runtime/config.py` (`ExecutionConfig`/`ExecutionPlannerConfig` + planner⇒market_data cross-check), `runtime/pipeline.py`.

**Tests added.** `tests/test_execution_planner.py`, `tests/test_validate_planned_order.py`.

**Known limitations.** Only GTC and FAK are produced; GTD, post-only, slicing, iceberg, and cancel/replace remain future work. Disabled by default (`execution.planner.enabled=false`); enabling requires `market_data.enabled=true`. **`coord.market_state` is not populated by `app.py`** — entry planner works without a book; urgent/protection exits need store wiring (see live validation matrix).

**How to run tests.** `python -m pytest tests/test_execution_planner.py tests/test_validate_planned_order.py`

**How to run a safe harness (CLI, entry path).** Shadow scenario overlay — **verified CLI-runnable:**

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/simple_signal_test.yaml \
  --scenario shadow_planner_simple_signal_test \
  --run-name simple_strat_planner_shadow
```

Inspect `execution_plan` + `risk_decision` with `"phase":"planned"` in `facts.jsonl`. Urgent/stale deny: unit tests only until `MarketStateStore` is wired on the coordinator.

**Verification label:** `shadow-runnable` (entry) · `unit-tested` (urgent/stale) — see [live_validation_matrix.md](live_validation_matrix.md).
