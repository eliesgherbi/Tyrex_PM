# Adding a strategy

A strategy integration supplies only strategy-specific behavior:

1. Immutable configuration and schema validation under `strategies/<name>/`.
2. Pure indicators, valuations, and entry/exit policy.
3. A driver that converts the framework `DecisionContext` into a strategy snapshot and returns generic `EnterIntent`, `ExitIntent`, or `FlattenIntent`.
4. Strategy state that can be serialized and restored without venue objects.
5. Unit tests for model mathematics, gate precedence, intent semantics, and state restoration.

The strategy must not import the Polymarket SDK, submit orders, read account state, persist execution evidence, or infer fills. Those responsibilities remain in the shared execution framework.

To compose another strategy, generalize the market-runtime driver port and select the driver from the single run config. Do not copy `TradingRuntime`, `ExecutionLifecycle`, the gateway, or the reducer.
