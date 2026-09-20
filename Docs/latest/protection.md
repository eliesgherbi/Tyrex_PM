# Position protection

Framework-owned overlay. Strategies declare thresholds in YAML; they do not evaluate SL/TP themselves and they do not POST protection orders.

## Three exit planes

Exits share the same intent → planner → lifecycle sell spine:

1. **Strategy policy** — thesis / rich / time (`z_gap`) or entry-only harness (`ask70`; `feature_probe` when `emit_entry: true`).
2. **Risk / safety** — kill switch, mandatory flatten, crash recovery, manual deadline.
3. **Protection overlay** — SL / reactive TP / trailing from an explicit `protection:` block.

Precedence (high → low): emergency/kill → mandatory flatten / manual deadline → hard SL → strategy exits → trailing → take-profit.

Gateway “protection price” (tick-adapted limit on a sell) is unrelated to `ProtectionSpec`.

## When it is required

`StrategyPlugin.requires_protection` may be a bool or a callable of loaded config (`plugin_requires_protection` in `strategies/registry.py`):

- `ask70` — **required**. Config load fails without a valid `protection:` block.
- `feature_probe` — required only when `emit_entry: true`. The shipped `feature_probe_catalog.yaml` sets `emit_entry: false` and omits `protection:`.
- `z_gap` — optional. The shipped `z_gap_tiny_live.yaml` includes a `protection:` block so the overlay can fire while z_gap HOLDs; the file still loads if that block is removed.

## Contract (`ProtectionSpec`)

Parsed by `src/tyrex_pm/protection/spec.py`. Unknown keys fail. No hidden threshold defaults.

| Field | Allowed today |
|-------|----------------|
| `mark` | `bid` (held-token best bid) |
| `size_mode` | `full` |
| `take_profit.style` | `reactive_fak`, `resting_gtc`, or `resting_gtd` |
| `take_profit` level | exactly one of `absolute` in `(0, 1)` or `pct_from_entry` > 0 |
| `stop_loss.style` | `stop_market_fak` |
| `stop_loss` level | exactly one of `absolute` in `(0, 1)` or `pct_from_entry` > 0 |
| `trailing.enabled` | bool |
| `trailing.activation` | `immediate` or `after_profit` |
| `trailing` distance | exactly one of `trail_abs` or `trail_pct` when enabled |
| `trailing.activation_profit_abs` | required only when `activation=after_profit` |
| `trailing.mark` | `bid` |

At least one of take-profit, stop-loss, or enabled trailing is required.

Example: `config/runs/ask70_protection_tiny_live.yaml`.

## Runtime behavior

1. After confirmed exposure, `TradingRuntime` arms `ArmedProtection` from the spec and fill mark.
2. Polymarket book updates call `on_public_fact("polymarket.books", ...)` independently of strategy evaluation. Protection can fire on a book tick even when the strategy scheduler does not wake.
3. Triggers emit `ExitIntent` or `FlattenIntent` with `PROTECTION_SL` / `PROTECTION_TP` / `PROTECTION_TRAIL` reason codes.
4. Reactive TP / SL / trailing submit bounded FAK sells. Resting TP POSTs a post-only GTC/GTD SELL without `ExitRequested` so SL can still fire. Soft OCO = cancel that id, then G1-sized FAK. GTD expiration must be ≥ 3 minutes in the future; otherwise the host falls back to `reactive_fak`.

Protection does not bypass kill switch, mandatory flatten, or the manual deadline.
