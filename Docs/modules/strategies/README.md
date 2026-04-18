# `strategies/`

Composition layer that turns a `GuruCopySignal` (or other signal) into one or more `Intent`s. Currently one strategy: `guru_follow`.

## `guru_follow/`

| File | Purpose |
|------|---------|
| `strategy.py` | `GuruFollowStrategy.on_guru_signal(sig, holdings) -> (intents, skip_reason, sizing_meta)` — the single entry point |
| `filters.py` | `apply_filters(sig, cfg)` — token allowlist, min notional, conviction, market tradeability gate (when wired) |
| `sizing.py` | `build_enter_intent(sig, cfg)` — static USD or proportional `copy_scale * conviction(score)` |
| `exits.py` | `maybe_exit_intent(sig, cfg, holdings)` — proportional vs full-position exit + dust suppression |

## Contract

```python
def on_guru_signal(
    sig: GuruCopySignal,
    holdings: dict[TokenId, Decimal],
) -> tuple[list[Intent], str | None, dict[str, str] | None]:
    ...
```

- Returns `(intents, skip_reason, sizing_meta)`.
- `skip_reason`, when set, is a constant from `core/reason_codes.py` (e.g. `GURU_BELOW_MIN_NOTIONAL`, `GURU_NO_BOT_INVENTORY`, `GURU_PRICE_REQUIRED`).
- `sizing_meta` is merged into the `intent_created` fact for operator audit (e.g. `{"sizing_mode": "static"}`).

## Boundaries (enforced by code review, not the type system)

A strategy MUST NOT:

- Read from `WalletStore` / `OrderStore` directly. The pipeline passes `holdings` precisely so the strategy stays venue-agnostic.
- Submit orders, cancel orders, or talk to the venue.
- Emit logs that duplicate what facts will already record.
- Carry side-effectful state across calls (the strategy itself is essentially a pure transformation).

## Adding a new strategy

See [../../developer_guide.md §4.2](../../developer_guide.md#42-add-a-new-strategy). At minimum:

1. New `strategies/<name>/` package with `strategy.py`, `filters.py`, `sizing.py`, optional `exits.py`.
2. Strategy YAML under `config/strategies/<name>.yaml` and a scenario under `config/scenarios/`.
3. Wire it into `runtime/app.py::cmd_run` (the current code instantiates `GuruFollowStrategy` directly; promote that to a registry when a second strategy lands).
4. At least one golden test under `tests/test_<name>_strategy_*.py`.
