# `strategies/`

Composition layer that turns a `GuruCopySignal` (or other signal) into one or more `Intent`s. Production strategies: `guru_follow`, `sell_test`, `allocation_test`.

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
