# `strategies/`

Composition layer that turns a `Signal` into one or more `Intent`s. Strategies: `guru_follow`, `simple_signal_test` (non-guru architecture harness), `validation_harness` (Phase 4.5 operator validation tool), `paired_binary` (Phase 4.6 production paired-leg strategy — design only), `sell_test`, `allocation_test`, `tp_sl_test` (TP/SL regression harness).

## Generic contract (P1 architecture_enhance)

Every strategy implements the generic entry point, dispatched by `pipeline.process_signals`:

```python
def on_signal(self, signal: Signal, ctx: StrategyContext) -> StrategyResult:
    ...
```

- `StrategyContext` bundles read-only handles (`coord`, and `market_state` once Phase 2 is enabled).
- `StrategyResult` carries `intents`, an optional `skip_reason`, and optional `meta`.
- `GuruFollowStrategy.on_signal` delegates to the legacy `on_guru_signal` (kept for back-compat); `process_new_guru_signals` is a thin wrapper over `process_signals`.
- `simple_signal_test` is the canonical non-guru proof: a `SimpleSignal` produces an `EnterIntent` through the same pipeline. **CLI-safe:** `runtime/app.py` routes it through `runtime/fixture_signal_run.py::run_fixture_signals_once` — no guru polling, no `guru.wallet`, no Data API `/activity` requests.

## `simple_signal_test/` (non-guru reference harness)

| File | Purpose |
|------|---------|
| `strategy.py` | `SimpleSignalTestStrategy.on_signal` — maps a configured `SimpleSignal` to one `EnterIntent` |

Wired in `runtime/app.py` via the generic fixture runner (`fixture_signal_run.run_fixture_signals_once`). One-shot by default (`run_once: true` in config); respects shadow/live mode, `RiskEngine`, and `SingleWriterOMS` like every other strategy path. For architecture validation and controlled smoke tests only — not a production signal source.

## `validation_harness/` (Phase 4.5 live validation harness)

| File | Purpose |
|------|---------|
| `strategy.py` | `ValidationHarnessStrategy.on_signal` — maps `ValidationSignal` to `EnterIntent` or urgent `ExitIntent`; SELL clamps to owner allocation |

Wired in `runtime/app.py` via `validation_harness_run.run_validation_harness_once`. Configurable modes (`normal_entry`, `urgent_exit`, `stale_book_deny`, protection modes, `market_data_readonly`) exercise Phase 2–4 paths that `simple_signal_test` cannot reach. **Operator validation tool — not production trading.**

| Readiness | Modes |
|-----------|-------|
| unit-tested | all modes |
| shadow CLI | all modes |
| tiny-live-order | `normal_entry`, `urgent_exit` (with prior allocation) |
| live-read-only | `market_data_readonly` |

## `paired_binary/` (Phase 4.6 + Phase 2 WS + Phase 1 survival)

Production paired-leg strategy for BTC 5-minute Polymarket markets. Long-running loop via `runtime/paired_binary_run.py`.

| File | Purpose |
|------|---------|
| `strategy.py` | Entry facade; emits paired BUY intents |
| `state.py` | Explicit state machine + persistence (`var/state/paired_binary/<owner_id>.json`) |
| `monitor.py` | `PairedBinaryMonitor.tick` → stop/TP/timeout/survival enforce `IntentWorkUnit`s |
| `exit_engine.py` | Trigger pending, FAK retry, sellability gate |
| `entry_eval.py` | Pair cost / spread / quality gate |
| `facts.py` | Paired-binary + survival fact emitters |
| `market_timing.py` | Event-end clock diagnostics |

**Phase 2:** reads YES/NO books from WS-primary `MarketStateStore`; event-driven ticks when `survival.monitor_mode: ws_event`.

**Phase 1 (optional):** when `runtime.survival.enabled`, monitor delegates survivor phase to `survival/advisory.py` — hard floor (typically advisory), trailing (enforce in experiment scenarios), quality-reject retry, FAK order policy.

Wired: `runtime/app.py` → `paired_binary_run.run_paired_binary_loop`.

### Live truth sources and monitoring reliability

Entry completion uses **fill evidence only** (`entry_fill_lifecycle` + `entry_qty_reconcile`): CONFIRMED/MATCHED user-WS, OMS matched qty, or venue+ledger alignment after shadow fill. **Resting BUY on one leg does not activate** (`BOTH_ENTRY_PENDING` until both legs filled or timeout unwind).

**Sellability gate:** after both legs fill, state is `BOTH_LEGS_FILLED` until `venue_available_to_sell >= effective_qty` on **both** legs. Only then → `BOTH_LEGS_ACTIVE` + `paired_binary_monitor_started`. Emits `paired_binary_waiting_for_sellable_inventory` while waiting.

**Exit lifecycle:** trigger detected → `STOP_PENDING_*` / `TP_PENDING_*` / `TIMEOUT_PENDING` → OMS submit ack → `EXITING_*` → fill → `ONLY_*_ACTIVE` or `DONE`. Stops/targets use **pair-level percentage PnL** from entry fill prices (see `paired_binary_pnl_plan` fact).

### Cashflow-based realized PnL

Authoritative realized PnL is computed from matched cashflows stored on each leg at entry/exit match time:

```text
buy_cash_total  = yes_entry_cash + no_entry_cash
sell_cash_total = yes_exit_cash + no_exit_cash
pnl_total       = sell_cash_total - buy_cash_total
```

Average prices are display fields only (`avg_price = cash / qty`). Book bid, trigger price, limit price, and planned price are never used as authoritative realized PnL. Missing cashflows emit `paired_binary_realized_pnl_unavailable`; a separate `paired_binary_price_based_pnl_estimate` may be emitted for diagnostics only.

Polymarket UI cash values may differ from OMS facts due to display, rounding, fees, or net/gross accounting. The bot reports OMS cashflow PnL; wallet-balance-reconciled PnL may be added later.

**Strategy semantics:** dual loser-stop while both legs active; single winner take-profit on survivor after loser sold; timeout active in pending/exiting states too.

### Live validation parameters vs production parameters

Strategy YAML defaults (`pair_stop_loss_pct: 0.02`, `pair_take_profit_pct: 0.05`) target **≈ +3% of pair cost** at planned fills. Scenario `live_paired_binary_tiny.yaml` caps risk only — tune spreads and pair cost for live validation, not profitability proofs alone.

| Readiness | Status |
|-----------|--------|
| unit + integration tests | green |
| shadow CLI | validated |
| live WS-primary (Phase 2) | validated — `validate_paired_binary_phase2_live_run.py` |
| Phase 1 survival trailing enforce | live experiment scenarios only; defaults off |

## `tp_sl_test/` (TP/SL regression harness)

| File | Purpose |
|------|---------|
| `strategy.py` | One BUY → TP/SL monitor → allocation-aware `ExitIntent` on trigger |

Not production TP/SL — it is a regression harness. **Production TP/SL is the [`protection/`](../protection/README.md) overlay (Phase 4)**, which attaches by `owner_id`, registers only after `allocation_buy_applied`, and emits urgent `ExitIntent`s through the planner. `tp_sl_test` uses `owner_id = tp_sl_test` by default and reuses `sell_test/pricing.py` for SELL price at trigger time.

## `guru_follow/`

| File | Purpose |
|------|---------|
| `strategy.py` | `GuruFollowStrategy.on_guru_signal(sig, coord)` — filters, BUY sizing, guru-mirror SELL |
| `filters.py` | `apply_filters(sig, cfg)` — token allowlist, min notional, conviction |
| `sizing.py` | `build_enter_intent(sig, cfg)` — static USD or proportional `copy_scale * conviction(score)` |
| `exits.py` | `maybe_exit_intent(sig, cfg, coord)` — **allocation-aware** guru SELL (P5) |
| `scheduled_exit_demo.py` | Optional timed ExitIntent after BUY; clamps to `guru_follow` allocation |

## Contract

```python
def on_guru_signal(
    sig: GuruCopySignal,
    coord: RuntimeCoordinator,
) -> tuple[list[Intent], str | None, dict[str, Any] | None]:
    ...
```

- Returns `(intents, skip_reason, sizing_meta)`.
- `skip_reason`, when set, is a constant from `core/reason_codes.py` (e.g. `GURU_NO_ALLOCATED_INVENTORY`, `GURU_NO_BOT_INVENTORY`).
- Guru SELL `sizing_meta` may include `guru_exit_sizing` and `guru_exit_health` for pipeline fact emission.

## Guru SELL sizing (P5)

Always allocation-aware — no wallet-only path:

```
final_size = min(planned, allocated_available[guru_follow], available_to_sell)
```

- `full_bot_position` → planned = full **allocated** `guru_follow` position (not wallet-wide).
- `proportional_to_guru` → planned = guru-scaled size, then clamped.
- Zero allocation → no `ExitIntent`, reason `guru_no_allocated_inventory`.

## Boundaries

A strategy MUST NOT:

- Mutate `AllocationLedger`, `WalletStore`, or `OrderStore`.
- Submit orders, cancel orders, or talk to the venue.
- Bypass `RiskEngine` (strategies only emit `Intent`s).

A strategy MAY:

- Read allocation via `coord.allocation_ledger.get_available_allocated(owner_id, token_id)` for SELL sizing.
- Read venue inventory snapshots via `runtime/exit_lifecycle.inventory_snapshot(coord, token_id)`.

## Adding a new strategy

See [../../developer_guide.md §4.2](../../developer_guide.md#42-add-a-new-strategy).
