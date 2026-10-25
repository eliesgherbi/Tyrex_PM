# Tyrex v1: framework consolidation plan

Status: implemented 24–25 October 2026 (steps 0–7); outcome, re-scoping and open items in
[STATUS.md](STATUS.md). Each step ended green (unit + integration tests) and was committed on its own.

Source: the review of 24 October (replay engines, `trading_runtime.py` anatomy, data-to-execution
pipeline across strategies and markets). File references are to `src/tyrex_pm/` unless stated.

## What v1 is

One strategy contract, one pipeline, one replay engine.

1. **One strategy contract.** Every strategy is a `StrategyPlugin` plus a driver built on
   `BaseDriver`. It reads only `DecisionContext`, its declared indicators and the shared models.
   The host never inspects strategy internals (`getattr`, `driver.state`, config paths).
2. **One pipeline, three clocks.** Live, paper and replay run the same host components. Only the
   source (live feeds, warehouse, fixture), the clock (wall, recorded) and the venue (Polymarket,
   simulated) differ.
3. **One simulated venue, one book rule, one ledger of results.** Paper and replay fill with the
   same model. A replay's economics are computed once.
4. **Market families are configuration.** Window length, asset symbol, reference feeds and outcome
   labels come from one family definition; BTC 15-minute or ETH 5-minute need no code change.
   Multi-outcome and windowless markets are out of v1 scope; the interfaces must not block them.
5. **Research is strategy-agnostic.** Workspace forms, replay, results and cookbook work for every
   registered strategy whose plugin declares replay support.

### Acceptance tests (v1 is done when all pass)

| Id | Test | Where |
|---|---|---|
| V1-a | Every registered strategy runs `run_window` on the recorded reference window and produces decisions without error | `tests/integration/test_v1_all_strategies_replay.py` |
| V1-b | Paper venue and simulated venue give identical fills for the same order and book | `tests/unit/execution/test_fill_parity.py` |
| V1-c | A test-only strategy package registered from the test (no edit outside its folder) runs in replay and through `tyrex-pm eval` | `tests/integration/test_v1_new_strategy.py` |
| V1-d | A BTC 15-minute family defined in configuration is accepted by the runtime family registry, the warehouse profile check and the replay loader | `tests/unit/runtime/test_market_family_config.py` |
| V1-e | z_gap and ask70 appear as available in `strategy.capabilities`, validate and preview in the workspace API | `tests/unit/research/test_workspace_any_strategy.py` |
| V1-p | settlement_value decisions and economics on the saved reference jobs are unchanged (digest parity) | `tools/v1_parity_check.py`, run at the end of steps 1-6 |

## Step 0: secure and clean the repository

**Goal:** all current work in git, no dead code, nothing that misleads a reader.

Commits (on `rest_project`, one per area):
1. data warehouse and recorder (parser v2, subscription horizon, closing phases, winners-only sessions);
2. replay engine and scenarios (qualification v2, `venue_top_v1`, `friction_v3`, `friction_stress_v2`);
3. workspace backend (`research/workflows/`, `research/ihm.py`, cookbook recipes);
4. frontend (`frontend/`, with the mockup components it uses moved into `frontend/src/design/`;
   `prototypes/` sources kept as the approved design reference, `node_modules`/`dist` ignored);
5. runtime and execution work already present (managed exit, trading integrity, depth snapshot,
   reconcile, trading recording);
6. tests and docs.

Removals (git history keeps everything; tag `archive/pre-v1-2026-10-24` first):

| Item | Reason |
|---|---|
| `old/` | 840 archived files, excluded from packaging, imported by nothing |
| `scratchpad/`, `var_baseline_pytest.txt`, `config/runs/Untitled` | leftovers; the baseline file reports a test layout that no longer exists |
| `scripts/r6b_*`, `r7a1_*`, `r7c_*`, `r7e_*`, `r7f_*`, `r8_*` | import deleted modules |
| `planning/` (+ its tests), `signals/`, `indicators/momentum.py`, `core/execution_events.py`, `market_data/settlement_store.py`, `adapters/polymarket/ws_adapter.py` | not imported by code; superseded by `execution/planner.py`, the indicator graph, the execution reducer, the settlement ledger and `market_data/book_feed.py` |
| empty package folders (`risk/`, `portfolio/`, `lifecycle/`, `runtime/yaml_config/`, `strategies/framework_validation/`, `tools/n1_audit/`, `tools/n5_shadow/`), root `tyrex_pm.egg-info/`, root `__pycache__/` | only stale bytecode |

Tests: full unit + integration suite; `python -c "import tyrex_pm..."` for every package.
Done when: `git status` is clean apart from `var/`, the suite passes, `ruff` passes on `src/` and `tests/`.

## Step 1: one strategy contract

**Problem:** hosts reach strategy behaviour by `getattr` (`on_entry_outcome`, `on_exit_outcome`,
`on_hold_committed`, `restore_window_usage`, `value_exit_refusal`, `configure_ptb`, `state`);
replay calls two of them unguarded, so only settlement_value works there. The trigger is free
text (z_gap rejects `"replay"` and `"observe"`). Three hosts build `DecisionContext` differently.
Replay reads strategy config paths (`entry.tau_min_s`, `sizing.max_debit_usdc`).

**Interfaces**

```python
# strategies/triggers.py
class EvaluationTrigger(str, Enum):
    FEED = "feed"; TIMER = "timer"; REPLAY = "replay"; OBSERVE = "observe"

# strategies/protocol.py  (the declared contract; hosts type against it)
class StrategyDriver(Protocol):
    strategy_id; target_notional; time_authority; config
    def on_start(self, context: StrategyContext) -> None
    def on_stop(self, reason: str) -> None
    def evaluate(*, market_snapshot, causation_id, correlation_id, context, trigger, features) -> StrategyEvaluation
    def on_entry_outcome(self, outcome: EntryOutcome) -> None
    def on_exit_outcome(self, outcome: ExitOutcome) -> None
    def on_hold_committed(self, window_id: str) -> None
    def on_reference_sealed(self, sealed) -> None          # replaces configure_ptb
    def restore_window_usage(self, usage: Mapping) -> None
    def value_exit_refusal(self, context, request) -> str | None
    def window_usage(self) -> Mapping[str, Any]           # replaces host reads of driver.state
    def persistence_slice(self) -> dict; def restore_persistence_slice(self, data) -> None

# strategies/base.py
class BaseDriver:  # no-op defaults for every optional method + shared helpers
    def eligibility(self, **facts) -> EligibilityFacts   # the boilerplate each driver repeats

# strategies/registry.py: StrategyPlugin gains
    max_entry_debit: Callable[[Any], Decimal] | None = None   # default: target notional
    replay_supported: bool = True
```

`runtime/decision_records.decision_context(...)` becomes the single builder: it gains the
capability fields live sets today (`entry_allowed/exit_allowed` with blockers, `kill_switch_active`,
`unknown_inventory`, `monotonic_ns`, lifecycle view). Live, observation and replay all call it.

**Files:** `strategies/{protocol,base,triggers,registry}.py`; the four drivers (inherit `BaseDriver`;
z_gap accepts every trigger and moves `configure_ptb` to `on_reference_sealed`; settlement_value
implements `window_usage`); `runtime/trading_runtime.py` (direct calls, shared builder; remove
`getattr` on drivers), `runtime/market_runtime.py` (`on_reference_sealed`), `runtime/observation.py`,
`runtime/decision_records.py`, `research/strategy_replay/run.py` (cutoff from
`plugin.entry_tau_bounds`, debit from `plugin.max_entry_debit`, `requires_protection` honoured).

**Tests:** `tests/unit/strategies/test_driver_contract.py` (every registered plugin builds a driver
that satisfies the protocol and accepts every trigger); decision-context builder parity test
(live fields == builder fields); existing runtime tests unchanged; V1-a (all strategies replay).
**Migration:** none for data. Strategy behaviour unchanged; V1-p must hold.

## Step 2: split `trading_runtime.py`

**Problem:** one 4,149-line class with ~85 fields and ~12 responsibilities; replay cannot reuse its
pieces.

**Approach:** two passes so behaviour never changes.
- **2a, pure moves.** Each responsibility becomes a module in `runtime/host/` holding a mixin;
  `TradingRuntime` composes them. Tests that monkeypatch `trading_runtime` names keep working
  through re-exports. Undeclared attributes become dataclass fields; `getattr(self, ...)` removed.
- **2b, explicit collaborators** for what replay must share: `DecisionHost` (context + recording),
  `OutcomeBuilder` (entry/exit outcome construction from execution state), `SettlementLifecycle`
  (hold commit/detach, residual hold, `hold_to_resolution` vs `managed_exit` behind a
  `LifecyclePolicy`), `RunModePolicy` (single window vs continuous, multi-attempt).

| Module (`runtime/host/`) | From lines (today) |
|---|---|
| `composition.py` (+ `runtime/composition.py`) | 304-573, 3913-4149 |
| `readiness.py` | 780-1130 |
| `metadata_warmer.py` | 621-778 |
| `window_binding.py` | 1131-1296, 2748-2917 |
| `decisions.py` | 1406-1739 |
| `intents.py` | 1741-1780, 2032-2541 |
| `protection.py` | 1782-2031 |
| `settlement_lifecycle.py` | 1297-1404, 2543-2650, parts of 2748-2917 |
| `deadlines.py` | 2651-2747 |
| `startup.py` | 2918-3419 |
| `reporting.py` | 3646-3910 |
| `trading_runtime.py` (host) | 3420-3645 + market-data callbacks |

**Tests:** the whole runtime suite unchanged; a size guard test (`trading_runtime.py` < 700 lines,
no module in `runtime/host/` > 700). **Migration:** none.

## Step 3: one replay engine

**Problem:** A (`lab/evaluate_replay.py`), B (`research/replay/engine.py`), C
(`research/strategy_replay/run.py`) and the paper venue each simulate a part; P&L is computed in
four places; replay and live rebuild books differently.

**Target**

```
Source (warehouse | live capture | fixture) -> StrategyFeed -> MarketSessionRuntime
  -> DecisionHost.decision_context -> driver.evaluate -> planner + dispatch_check
  -> SimulatedVenue (fill model) -> OutcomeBuilder -> driver.on_*_outcome -> Engine ledger
```

**Interfaces**
- `execution/fill_model.py`: `walk_book(levels, side, *, shares|spend, participation, consumed)`,
  `taker_fee(...)` — the single fill and fee rule, used by `research/replay/engine.py` (virtual time)
  and `execution/paper.py` (wall clock).
- `research/strategy_replay/sources.py`: `WarehouseSource`, `CaptureSource`, `FixtureSource`
  (converts `lab_evaluate_v1` packs to feed rows). `tyrex-pm eval` runs through `run_window`
  with `FixtureSource`; `lab/evaluate_replay.py` keeps only the pack loader and CLI glue.
- `StrategyFeed` routes Binance L2 depth (feature_probe and depth indicators get their inputs).
- `run_window(plugin, config, ...)`: strategy-kind agnostic; record fields from
  `plugin.decision_record_schema`; config identity from `plugin.load_config` + canonical digest.
- Book policy: one setting (`Scenario.book_reconstruction`); `ReplayExecutionPolicy` loses its copy.
  One crossed-book predicate `market_data/reconcile.is_crossed`, chosen after measuring locked books
  in the recordings (live uses `>`, the warehouse `>=`).
- Economics: `results.economics` reads the engine ledger instead of recomputing; `sequence.py` too.

**Removed:** `Engine.run` scripted mode and `tyrex-research replay` (scripted intents) unless a test
proves a user; `workflows/replay_run.py` fake agent; unused `Scenario` fields (`seed`, `tie_policy`,
`intents_file`, `label_policy_file`, `quality_predicate`; still accepted on load for old snapshots).

**Tests:** V1-a, V1-b, V1-c; the lab tests rewritten on `FixtureSource` with the same expected
decisions; `test_settlement_value_recorded_window.py`, `test_replay_v3_mechanics.py` unchanged.

**Migration of saved results**
- Experiments gain `engine: "replay_v2"`; older ones read as `replay_v1`. The duplicate guard
  includes the engine, so a saved experiment can be re-run under v2.
- Results of v1 jobs stay readable; the Results page labels them "made with the pre-v1 engine".
- Interrupted v1 jobs: resume refused with "the replay engine changed; start a new run" (the
  source fingerprint check already refuses; the message is made explicit).
- Parity gate V1-p: `python tools/v1_parity_check.py 01777c96afdd42018eddf95ceab82383
  55815caba25b48929fc7b624d2e68d1e` (settlement_value, friction_v3, 32 units each, run on 24
  October with the current engine). Decision digests and economics must match, except for
  differences documented as intended (for example the crossed-book rule). Jobs from 23 October
  already differ in decision digest only (entries, fills, P&L, qualification and K equal),
  because the engine's decision records changed later that day; they are not parity references.

### Step 3 as implemented (24 October)

- **Shared fill model** `execution/fill_model.py` (`walk_shares`, `walk_spend`, `taker_fee`,
  `bought_shares`): the replay engine and the paper venue both use it. V1-b proves identical
  fills, fees and cash on the same book (holdings agree to 18 decimals; the engine computes at
  a higher decimal precision). Paper's fill-model label is now `paper_live_book_taker_v2`
  (fees rounded to 0.00001 as in replay; share orders no longer floored).
- **One book rule.** The scenario sets it; `ReplayExecutionPolicy.book_reconstruction` only
  overrides (captured-run reconstruction) and records the rule used. The worker no longer maps
  it. One predicate `market_data.reconcile.is_crossed` (bid >= ask invalid) for live and
  recorded books: under `venue_top_v1`, one hour of recorded books (6.1 million checks) had no
  locked and no crossed book, so the stricter rule changes nothing measured.
- **Binance depth in replay.** `load_window(spot_depth=True)` rebuilds the depth state at the
  window start from the last recorded snapshot and the updates since (seed row
  `replay_seed`), then streams the recorded rows; `StrategyFeed` passes it to the indicator
  graph with the live snapshot type when the contract starts the L2 adapter. Cost: about
  3 minutes per window (snapshots are rare: 7 in 5 hours). feature_probe now observes features.
- **One P&L.** The window economics are derived from the engine's own fill ledger and agree
  with the engine's settled books (checked in V1-a).
- **Engine version.** `research/strategy_replay/engine_version.py`: new experiments record
  `engine: replay_v2` in their identity; older ones read as `replay_v1` and the Results page
  says so.
- **Re-scoped:** the fixture lab (`tyrex-pm eval`) stays a strategy *decision harness* (no
  execution) instead of becoming a replay source: its packs are synthetic and would have to be
  rewritten as recording-like data to pass live readiness rules, with no information gained.
  It now builds its context with the shared builder. The engine's scripted mode stays: it is
  the simulator's own unit-test harness. Unused `Scenario` fields stay because saved
  experiment snapshots carry them.

## Step 4: features and shared models

**Problem:** strategies compute derived values themselves; readiness is hard-coded to `ewma_vol`;
`trade_imbalance` never computes live; probability, book walk, leg selection and episode logic
are written once per strategy.

**Interfaces**
- `IndicatorSpec.gates_entry: bool` (readiness from the spec, not the name).
- New graph indicators: `evaluated_bid_trend`, `spot_path`; `momentum` gets a real definition or is
  removed from the graph.
- Host-provided time: `DecisionContext.tau_s` and `window_start/window_end` (strategies stop
  computing tau).
- `models/` package: `binary_fair_value`, endpoint and TWAP probability bands, `walk_asks/walk_bids`
  with fees, `select_leg`.
- `strategies/episode.py`: framework episode state (entry lineage, retry, re-entry, hold), generalized
  from `settlement_value/state.py`; ask70, feature_probe and z_gap use it for lineage.
- Live `on_trade` passes quantity and aggressor.

**Tests:** model unit tests moved with the code; indicator readiness tests; trade-imbalance live
parity test; V1-p.

## Step 5: market families drive markets

**Problem:** BTC, 300 s and UP/DOWN are hard-coded in the runtime, the warehouse and replay;
`MarketFamily.window_duration_s` is never read.

**Interfaces**
- `runtime/market_family.MarketFamily`: `asset`, `window_duration_s`, `slug_pattern`,
  `reference_symbols` (Chainlink stream, Binance symbol), `outcomes` (labels in order).
- Families defined in `config/markets/*.yaml` (`btc_updown_5m` shipped; `btc_updown_15m`,
  `eth_updown_5m` as examples, disabled until recorded).
- Runtime: `market_data_runtime` uses the family for boundaries (`// 300` removed), discovery and
  symbols; the z_gap import and defaults leave the host.
- Warehouse: `families/btc_5m.py` becomes `RecurringUpDown(family)`; `config.validate` accepts any
  registered family; supervisor and stream URLs take the family's symbols.
- Replay loader: outcome labels from the family.

**Tests:** V1-d; existing BTC 5-minute tests unchanged. **Migration:** recorded collections keep
their profile; `btc_updown_5m` stays the default family id.

## Step 6: strategy-agnostic research

**Problem:** the workspace and notebooks are built around settlement_value.

**Interfaces**
- Plugin: `parameter_schema(config) -> list[Field]` (key, type, default, choices, label, unit, group),
  with a generic default derived from the loaded default config; settlement_value moves its labels
  and groups into its package.
- `strategy.capabilities` returns every plugin with `replay_supported`, each with its fields.
- `replay.validate/preview`, the worker and results use `plugin.load_config`, `plugin.kind` and the
  record schema; `config_hash` is generic.
- Frontend: strategy chooser in Replay setup; forms and result columns from the capabilities.
- `strategy_lab`: experiment helpers call `run_window` through the plugin; settlement-specific
  analysis stays in notebooks.

**Tests:** V1-e; workflow tests parametrized over strategies; browser check.
**Migration:** saved configurations without `strategy_kind` read as `settlement_value`.

## Step 7: documentation

- `Docs/latest/architecture.md`: the v1 pipeline (diagram above), host components, where each
  concept lives.
- `Docs/latest/extending.md`: add a strategy (plugin, BaseDriver, contract, indicators, tests), add a
  market family.
- `Docs/latest/research_workspace.md`: running the workspace, recording, sets, cookbook, replay,
  reading results.
- Fix `README.md`, `Docs/README.md`, `running.md` (commands), `data_warehouse.md`; `v_1_goal/STATUS.md`
  decision log; superseded plans marked historical.
