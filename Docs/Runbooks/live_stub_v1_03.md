# Live node smoke — v1.03 waiver

Milestone v1.03 defers a full **`TradingNode`** connect unless you explicitly opt in.

## Default (CI / fresh clone)

- `tyrex_pm.runtime.live_stub.maybe_run_live_node_smoke()` returns **`skipped`** when `TYREX_LIVE_NODE_SMOKE` is unset.
- This satisfies v1.03 when combined with **strategy log line** unit tests and package layout review.

## Optional operator smoke

1. Complete **v1.00** auth (`.env` / `POLYMARKET_*`).
2. Set `TYREX_LIVE_NODE_SMOKE=1`.
3. Follow NautilusTrader **`examples/live/polymarket/`** to build a real node when you are ready; this repo does not duplicate that script in v1.03.

## References

- [Nautilus Polymarket integration](https://nautilustrader.io/docs/nightly/integrations/polymarket/)
