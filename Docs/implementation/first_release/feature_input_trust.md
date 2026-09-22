# R3 — Feature validity (first-release live inputs)

**Status:** Implemented for first-release live-supported z_gap inputs (2026-09-20). **F-L2 remains deferred.** BUY-only `FinalExecutionGate.features_ready` evaluates bound `ewma_vol` snapshots; empty graphs (ask70) are a no-op. Trade-tape freshness uses `market.maximum_book_age_ms`. Book `data_age_ms` is monotonic apply age. Reconnect invalidates EWMA. SELL/flatten do not require EWMA.
**Depends on:** nothing (parallel with R5).
**Reuse:** `IndicatorGraph`, `EwmaVolatilityEstimator` / `VolatilitySnapshot.ready`, `BookStore.capture_pair` age, Binance **trade** `connection_generation` (`adapters/binance/ws_adapter.py`). Do not start L2 for z_gap.
**Bounded scope:** consume-time freshness + reconnect invalidation for inputs required by live `z_gap`. Not an L2/imbalance policy. Not a ban on loading other registered plugins.

## Supported live-release scope (consistent with overview)

**First-release live-validated:** `config/runs/z_gap_tiny_live.yaml` → plugin `z_gap` → `Z_GAP_INPUT_CONTRACT` / `indicator_specs(config)` plus host books, clock, account, PTB/Chainlink already fail-closed.

**Registered and technically configurable, not live-validated here:** `ask70`, `feature_probe`, catalog YAMLs. They may `--validate-config` and lab-replay. This task does **not** refuse those configs at load. It also does **not** claim they are proven live.

**Explicitly not first-release live-supported** (producers may remain in `IndicatorGraph._KNOWN_NAMES` for lab):

`imbalance`, `ofi`, `trade_imbalance`, `realized_vol`, `microprice_gap`, `momentum`, `basis`, anything on `binance.perp.*`, `binance.spot.l2` / `.mid`.

First-release **live** z_gap does not start L2 (`LIVE_ADAPTERS` only if the **bound plugin contract** requires it). Do not add `maximum_l2_age_ms`. Do not fail load because a non-z_gap plugin lists `imbalance`.

### Follow-up F-L2 — L2 / `feature_probe` freshness (deferred)

The earlier L2 / `feature_probe` freshness issue is **deferred, not resolved**. Depth adapter still stamps `ts_event=datetime.now(timezone.utc)` (receipt, not exchange event). There is no consume-time L2 age gate and no reconnect invalidation for the L2 book used by imbalance/OFI.

**F-L2** is a named follow-up outside this release: monotonic L2 age, generation invalidation, and live `feature_probe` support. Do not implement it in R3. Do not mark it passed in R7. Do not imply R3 “covers feature_probe” because the plugin is registered.

## Live-support rule (this task enforces)

A fact or indicator is **first-release live-supported** if and only if:

1. It is in the **transitive required closure** of the registered plugin used by `config/runs/z_gap_tiny_live.yaml`, **and**
2. `runtime/composition.py` has a live adapter for its source, **and**
3. This task defines consume-time freshness + reconnect invalidation (or an existing equivalent is proven).

**Live-supported (this task enforces):**

| Input | Source | Existing | Gap |
|-------|--------|----------|-----|
| Polymarket books | `polymarket.books` | `capture_pair` sets `data_age_ms = now_wall - last_apply_ts`; final gate `maximum_book_age_ms=2000` | `mark_stale_if_aged` (**monotonic**) is **never called** (only defined in `book_store.py`). Age uses wall `datetime.now` vs `last_apply_ts`, not receive vs source. Disconnect: `connection_health` on the view, but stale last book can still look “young” if wall clocks stall. |
| `ewma_vol` | `binance.spot.trades` | `VolatilitySnapshot.ready` after `min_samples_s`; jump guard | Reconnect does **not** invalidate the estimator. Last snapshot stays `ready=True` with `last_update_ts` from the **old** connection. Age is not recomputed at `evaluate()` if no new trades. Trade adapter **has** `connection_generation`; graph/ewma does not consume it. |
| Clock, account, PTB/Chainlink | existing policies | `max_clock_uncertainty_ms`, `snapshot_max_age_s`, `max_ptb_lag_ms`, basis | **Out of this task** (already fail-closed). Do not rebuild. |

## Current behavior (verified)

- Books: `data_age_ms` is computed at `capture_pair` from **apply wall time**. Source timestamp is `source_ts=last_ts_event`; receipt is `receive_ts`. Gate uses `data_age_ms` only. If **no new messages**, capture still recomputes wall age (good). If the operator clock jumps backward, wall age can shrink (bad). Monotonic `last_apply_mono_ns` exists and is unused at the gate. `mark_stale_if_aged` unused.
- EWMA: `graph.snapshot()` returns the last `VolatilitySnapshot` with a frozen `last_update_ts`. `feature_probe`-style “key present” is not z_gap’s bug; z_gap uses `vol.ready`. After Binance trade WS reconnect (`ws_adapter.py` generation++), estimator state is **kept**. Eligibility can return on **stale** σ without `min_samples_s` on the new generation.
- Depth adapter (`depth_adapter.py`) stamps receipt time. **F-L2**, not this task.

## Clocks (locked)

| Clock | Use |
|-------|-----|
| **Monotonic apply/receipt** (`time.monotonic_ns`) | Freshness age at **consume** (evaluate and final gate). Immune to wall rewind. |
| **Corrected UTC** (`TimeAuthority.now_corrected_utc`) | Tau / PTB (unchanged, R2). Not L2/book age. |
| **Source event time** (`ts_event` / `source_ts`) | Ordering inside EWMA / PTB; **not** the stale threshold by itself (a delayed packet can have a fresh source ts). |
| **Host wall receive** (`ts_received`) | Operator correlation in reports only. |

**Consume-time age** for a book leg:

```text
age_ms = (now_mono_ns - last_apply_mono_ns) / 1e6
```

If `last_apply_mono_ns` is missing → not fresh (fail closed). Recompute on every `capture_pair` / gate call even when no new WS message.

**Consume-time age** for ewma_vol:

```text
age_ms = (now_mono_ns - last_trade_apply_mono_ns) / 1e6
```

Store mono on `IndicatorGraph.on_trade`. `VolatilitySnapshot.ready` becomes false if age > `market.maximum_book_age_ms` **or** a dedicated `maximum_trade_age_ms`. **Locked:** reuse `market.maximum_book_age_ms` (2000) as the consume-time max age for the spot-trade tape used by ewma_vol. Do not add a new YAML key in this task.

Distinguish in the snapshot/evidence: `source_ts`, `receipt_age_ms` (mono), `ready`, `reason`.

## Reconnect / old generation

Binance trades already increment `connection_generation` per WS session. On generation change (health `reconnecting` / new `connected`):

1. `IndicatorGraph.invalidate_source(binance.spot.trades)`: replace ewma instance (or `empty_volatility_snapshot(reject_reason="source_invalidated")`), `ready=False`.
2. Ignore `on_trade` whose generation `<` current (stale socket).
3. Eligibility returns only after `min_samples_s` warmup **on the new generation** and consume-time age OK.

Books: existing `connection_epoch` on `BookView`. Reject updates whose epoch is older than the live feed’s epoch (if the store does not already). On disconnect, `connection_health` not LIVE → `entry_executable` already false (`trading_runtime._market_data_ready`). Keep. Additionally mark books not READY until a snapshot on the **new** epoch (do not treat the pre-disconnect book as fresh solely because wall age < 2 s).

## Invalid during prepare → no entry; exits stay free

Hot path: plan → sign → capture book → **final gate** → authorize → POST (`architecture.md`).

At gate time, BUY must fail if:

- Book consume-time age > `maximum_book_age_ms` or sync not READY or connection not LIVE (already mostly true; use **mono** age).
- Contract requires `ewma_vol` (plugin `indicator_specs` non-empty for that input): graph snapshot at gate, that instance `ready=False` or trade tape age stale → `FEATURE_NOT_READY` / `EWMA_NOT_READY` (stable code), `COMPLETED_NO_DISPATCH`, no POST.

Implement the gate callback from the **bound plugin contract / graph snapshot**, not `if strategy_kind == "z_gap"`. ask70 with an empty graph: callback is a no-op / true so existing tests stay valid.

Do not re-run `evaluate()` at the gate. Do not require L2.

**SELL / protective flatten:** do **not** require ewma_vol. Stale entry inputs must not block held-position exits. Books for SELL still need a capturable view (existing). If Binance disconnects mid-sign: invalidate → gate fails BUY. Flatten/SL still allowed.

## Smallest change

1. `capture_pair` / gate: `data_age_ms` from **monotonic** apply; call `mark_stale_if_aged` on the 1 Hz tick (or inside capture).
2. Graph: last-trade mono + generation; `invalidate_source` on trade reconnect health in `market_data_runtime.py` (existing `_on_binance_health`).
3. `FinalExecutionGate` optional `features_ready: Callable[[], bool | str]` used **only for BUY** when the bound plugin’s live-supported indicators are required.
4. Tests as below. Docs: `strategy_inputs.md` live-supported vs catalogued vs F-L2 when landing.

## Fixture scenarios

Inject `monotonic_ns` / FakeClock. Do not stub `ready=True` on the snapshot under test.

| ID | Events | Expected |
|----|--------|----------|
| V1 | Two books 100 ms apart, capture 100 ms later | `data_age_ms` ~100 (mono); gate BUY OK if < 2000 |
| V2 | **No new book** for 3000 ms, then capture | age ≥ 2000; gate `book_stale`; no POST |
| V3 | Wall clock jumps backward, mono advances 3000 ms | still stale (mono wins) |
| V4 | Book `source_ts` old but just received (if distinguishable) | freshness uses **receipt/apply mono**, not source_ts alone |
| V5 | EWMA warmed, then trade WS generation+1, no new trades | `ready=False`, z_gap `strategy_inputs_eligible=false` |
| V6 | After V5, enough new-generation trades for `min_samples_s` and age OK | eligible can return |
| V7 | Trade payload with old generation after reconnect | ignored; ready stays false until V6 |
| V8 | BUY prepare, then V2 or V5 before gate | `COMPLETED_NO_DISPATCH`; mutations 0 |
| V9 | SELL with ewma not ready, books OK | SELL **not** blocked by EWMA |
| V10 | ask70 graph empty | gate features callback true / no-op; ask70 tests unchanged; load of `feature_probe` catalog still succeeds |

Reuse `tests/unit/indicators/test_ewma_graph.py` and `test_final_execution_gate.py`.

## Failure / recovery

Disconnect: invalidate immediately (no grace that keeps last σ live). Recovery = new generation + warmup + age. Jump guard remains estimator-local (existing).

## Acceptance

1. Age recomputed at consume with monotonic receipt/apply, including silence.
2. Source ts vs receipt age documented and tested (V4).
3. Disconnect invalidates; old-generation updates dropped.
4. Stale/unready required inputs block **entry** at evaluate **and** at BUY gate; they do not block SELL.
5. Live-support rule is the z_gap contract; L2/ofi/`feature_probe` live freshness is **F-L2**, not silently claimed done.
6. Non-z_gap registered plugins remain loadable.

## Exclusions

Perp adapters; making `feature_probe` live; implementing F-L2; new YAML age keys; changing `min_samples_s` economics; blocking ask70/feature_probe at load.
