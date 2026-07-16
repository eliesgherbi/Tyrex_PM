# D2 — PTB Source Precedence, Locking, and Mismatch

**Status:** COMPLETED

## Policy precedence

```text
1. Valid live boundary capture (RTDS Chainlink, lag ≤ 5000ms) → live_boundary
2. Valid sidecar/log boundary capture when live unavailable → log_boundary
3. No usable K → block enforce
```

Rules:

- `observed` / `observed_from_log` require non-null K.
- Boundary lag > 5000ms → `late` (debugging only; not usable for enforce).
- Once usable K is selected, it is **locked** for the window.
- Live vs log both usable → compare; difference > **0.5 bps** → mismatch, block enforce.
- Attestation `K_reference` (preflight) also compared when present.

## Implementation

| Component | Path |
|-----------|------|
| Selection policy | `src/tyrex_pm/strategies/z_gap/ptb_policy.py` |
| Persisted lock | `src/tyrex_pm/state/z_gap_ptb_store.py` → `var/state/z_gap_ptb_lock.json` |
| Signal store lock | `src/tyrex_pm/state/signal_state_store.py` (`lock_price_to_beat`) |
| Runtime wiring | `src/tyrex_pm/runtime/signal_feed_runtime.py` (`_apply_ptb_policy_to_store`) |

## Facts

| Fact type | When |
|-----------|------|
| `z_gap_ptb_source_selected` | Usable source chosen |
| `z_gap_ptb_source_mismatch` | Live/log/reference mismatch |
| `z_gap_ptb_locked` | K locked for window |

Required payload fields: `market_id`, `event_start_ts`, `selected_source`, `selected_k`, `live_k`, `log_k`, `difference_bps`, `live_boundary_lag_ms`, `log_boundary_lag_ms`, `usable`, `locked`, `block_reason`.

## Tests

`tests/test_z_gap_ptb_policy.py` — live precedence, log fallback, agreement, mismatch block, late cannot replace usable, lock violation, malformed sidecar isolation.

Sidecar malformed lines: `derive_ptb_from_chainlink_log_ex` + `tests/test_price_to_beat_tracker.py::test_derive_from_log_skips_blank_and_malformed_rows`.
