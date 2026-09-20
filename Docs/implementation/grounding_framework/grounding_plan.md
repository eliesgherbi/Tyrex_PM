# Grounding the strategy seam — implementation plan

**Status:** F1–F5 implemented (2026-09-13). G3 live SL proof remains operator evidence and is **orthogonal** — it does not block this sequence.  
**Date:** 2026-09-11  
**Phase:** Framework seam (F1–F5)  
**Predecessor:** [grounding_fixes/](../grounding_fixes/README.md) (G1–G8)

This phase does **not** add venue features, raise the $5 cap, implement q_edge economics, or build perp adapters. It makes the existing pattern actually reusable: a new strategy should declare facts/indicators, implement `evaluate()`, emit intents, and leave `TradingRuntime` untouched.

Operator and developer API remains [`Docs/latest/`](../../latest/). This folder is the build sequence.

## Locked rules for every task

1. Implement **F1 → F2 → F3 → F4 → F5** in that order. F4 depends on F2+F3. F5 depends on F3 (F2 if the lab replays indicators).
2. Reuse `TradingRuntime` / `MarketSessionRuntime` / `EvaluationScheduler`. Do not add a second host, Observe/Shadow runtime, or Nautilus.
3. No `if strategy_kind == ...` in `run_config.py`, `market_data_runtime.py`, `trading_runtime.py`, or `market_runtime.py`.
4. Do not change z_gap `theta` / `z` / `tau` economics. Migrating z_gap **ingest** onto `IndicatorGraph` must preserve σ numerics (same `SigmaConfig` fields as YAML).
5. Do not register `q_edge`. Do not start Binance perp/funding adapters. They stay in `UNIMPLEMENTED_ADAPTERS`.
6. Real venue mutations stay operator `--live` only. This phase’s tests are fixtures/unit.
7. `DecisionSnapshot` stays books + market (+ existing reference/freshness fields). Strategy-private snapshots stay inside `strategies/<name>/`.
8. ask70 remains the seatbelt harness. Do not delete it or fold protection math into it.

## What is already true (do not rebuild)

- One YAML + `--live` + $5 cap; plugins `z_gap` and `ask70`.
- `InputContract` with `required` / `optional` / `indicators` / `evaluate_on`; composition starts only the source-fact closure.
- `IndicatorGraph` + `FeatureBundle` exist and have unit tests over a generic `DepthStore`. After F2 the host constructs one graph per `MarketSessionRuntime` and passes a `FeatureBundle` into `evaluate()`.
- Spot L2 adapter + `DepthStore` on `MarketSessionRuntime.spot_depth_store` (G7). Perp/funding fail-closed.
- Maker GTC is POSTed (`MAKER_LIMIT_POSTED`). Planner still has a stale doc line.
- `prepare_aligned_eval()` already branches on `input_contract.requires(ptb.sealed)` for ask70 vs z_gap books-only eval.
- Protection overlay is independent of `evaluate()`.

---

## Code review (current leaks)

Read these before implementing. They are the defects this phase closes, not a rewrite list.

### Docs vs code

| Location | Says | Code |
|----------|------|------|
| `Docs/README.md` maturity table | Maker GTC “Planned only (`MAKER_LIMIT_PLANNED`)”; “q_edge / Binance L2 / perp … not built” | `trading_runtime.py` records `MAKER_LIMIT_POSTED`. `LIVE_ADAPTERS` includes `binance.spot.l2`. Perp still unimplemented. |
| `Docs/latest/architecture.md` planner bullet | `MAKER` → GTC limit, **not POSTed live** | False after G6. |
| `Docs/latest/architecture.md` composition bullet | “catalogued-but-unimplemented (Binance L2 / perp / funding)” | L2 is live; perp/funding are not. |
| `Docs/latest/strategy_inputs.md` source table | `binance.spot.l2` “no adapter”; `binance.spot.mid` “unimplemented adapter” | Adapter + derived mid exist. Perp rows are still correct. |
| `Docs/implementation/grounding_fixes/maker_orders.md` / `extra_market_data.md` | Historical “current code” sections | Leave as evidence; do not treat as API. |

### Feature path is declared, not run

- `Z_GAP_INPUT_CONTRACT.indicators = (IndicatorSpec(name="ewma_vol", source=binance.spot.trades),)` — **no SigmaConfig on the spec**.
- `IndicatorGraph.__init__` constructs `EwmaVolatilityEstimator(SigmaConfig())` **defaults**, ignoring z_gap YAML `volatility.*`.
- `MarketSessionRuntime.ingest_binance` duck-types `getattr(binding, "ingest_volatility")` and `seed_volatility`. ask70 has neither; z_gap `ZGapDriver` owns a private estimator.
- `market_data_runtime.py` starts `BinanceDepthWsAdapter` into `runtime.spot_depth_store` and may wake eval on `binance.spot.l2` / `.mid`. It never calls `IndicatorGraph.on_depth`.
- `Grep IndicatorGraph` under `src/tyrex_pm/runtime/` is empty. Only `tests/unit/indicators/test_depth_indicators.py` constructs it.

### `evaluate()` is z_gap-shaped

`StrategyDriver` / both live drivers / `TradingRuntime.on_evaluation`:

```text
evaluate(..., trigger, settlement_reference, settlement_reference_fresh)
```

`settlement_reference` is `ready.dyn.chainlink_raw`. ask70 `del`s those kwargs. z_gap `assemble_zgap_decision_snapshot` consumes them. A third plugin inherits a Chainlink-shaped door.

`DecisionSnapshot` already has `reference`, books, quotes, freshness. Chainlink/PTB for z_gap should be assembled **inside** the plugin from snapshot + session facts the host already used to decide `prepare_aligned_eval` was ok — not as extra evaluate kwargs. If a generic “sealed PTB / last Chainlink” bag is required, put it on `DecisionContext` or a small frozen `ReferenceFacts` that is empty when the contract does not require `ptb.sealed` / `chainlink.twap`.

### Naming leftover

`market_data_runtime.py` module docstring still says “Live public-data runtime for the Z-Gap strategy.” `ZGapMarketRuntime = MarketSessionRuntime` is a compatibility alias — do not add new `ZGap*` names.

### What a third plugin would hit today

1. No `FeatureBundle` in `evaluate()` → must copy `ingest_volatility` or read `spot_depth_store` privately.
2. Must accept and ignore `settlement_reference*`.
3. Docs would tell the author maker POST and L2 are missing.

That is why F1–F3 precede F4.

---

## Task order

| ID | Task | Depends | Outcome |
|----|------|---------|---------|
| F1 | Docs match maker POST + spot L2 | — | **Implemented** — latest docs describe the host that exists |
| F2 | Host-owned `IndicatorGraph`; `FeatureBundle` into `evaluate()` | F1 | **Implemented** — plugins subscribe; they do not ingest tapes |
| F3 | Generic `evaluate()` kwargs; private snapshots stay in plugins | F2 | **Implemented** — host is not z_gap-shaped |
| F4 | Third plugin `feature_probe` (not a research model) | F2, F3 | **Implemented** — proof the seam works |
| F5 | Fixture `evaluate()` lab, no `--live` | F3 (F2 if indicators replayed) | **Implemented** — model iteration without venue mutation |

---

## F1 — Align docs with maker POST + spot L2

**Status: Implemented (2026-09-11).**

### Goal

Anyone reading `Docs/latest/` (and the package README maturity table) sees the **current** host: maker GTC is POSTed; Binance spot L2 + derived mid are live adapters; perp/funding remain fail-closed; `q_edge` is still a guide contract only.

### Why this is first

F2–F4 will be designed from these pages. Stale “maker planned-only / L2 missing” causes a second ingest path or a skipped L2 contract.

### Implementation guide

Edit only documentation. No code.

**Must change (API):**

1. `Docs/README.md` maturity rows:
   - Maker GTC live POST → Implemented (`MAKER_LIMIT_POSTED`).
   - Split L2 vs perp: spot L2 implemented; perp/funding catalogued, fail-closed; `q_edge` not registered.
2. `Docs/latest/architecture.md`:
   - Planner bullet: `TAKER` → FAK market buy; `MAKER` → post-only GTC **POSTed**; session stays working until fill / cancel / expire (`MAKER_LIMIT_POSTED`). Match [execution_lifecycle.md](../../latest/execution_lifecycle.md) and [extending.md](../../latest/extending.md).
   - Composition bullet: unimplemented set is **perp trades / L2 / funding only**, matching `runtime/composition.py` `UNIMPLEMENTED_ADAPTERS`.
3. `Docs/latest/strategy_inputs.md` source table:
   - `binance.spot.l2` → Yes (`adapters/binance/depth_adapter.py`).
   - `binance.spot.mid` → derived from L2, live when L2 is started.
   - Perp rows stay “no adapter”.
4. `Docs/latest/README.md` “What is live today” — one line that spot L2 is available to contracts that name it (neither shipped plugin does).

**Must not change:**

- `Docs/implementation/grounding_fixes/*.md` historical “Current code” sections (evidence).
- z_gap/ask70 YAML economics.
- `Docs/latest/config_guide.md` unless a listed field is wrong (it should already match loaders).

**Acceptance:**

- The four latest/README files above do not say maker is plan-only or that spot L2 has no adapter.
- `composition.py` `LIVE_ADAPTERS` / `UNIMPLEMENTED_ADAPTERS` and the docs tables are the same set.

---

## F2 — Wire `IndicatorGraph`; pass `FeatureBundle` into `evaluate()`

**Status: Implemented (2026-09-11).** Pre-implementation leak notes below are the defects this task closed.

### Goal

The host owns **one** `IndicatorGraph` per `MarketSessionRuntime`, constructed from the plugin contract’s indicator specs (with parameters). On every relevant ingest it updates the graph. On every `evaluate()` it passes an immutable `FeatureBundle`. Strategies **do not** own EWMA/L2 ingest. `getattr(..., "ingest_volatility")` disappears.

### Why

z_gap already *declares* `ewma_vol` and then ignores the graph. A third L2/OFI strategy would copy `ZGapDriver.volatility`. After three plugins the catalog is decorative.

### Design constraints (read before coding)

**Parameterization gap (must close, not ignore).**  
`IndicatorSpec` today has `name`, `source`, `extra_sources`, `horizons_ms`, `levels` — **no** `half_life_s` / `sigma_floor`. `IndicatorGraph` uses `SigmaConfig()` defaults. z_gap YAML `parameters.volatility` is applied only inside `create_z_gap_driver()`. If you “just construct `IndicatorGraph(contract.indicators)`”, z_gap σ silently diverges from YAML.

Required shape:

- Extend `IndicatorSpec` with an explicit frozen parameter bag, e.g. `params: tuple[tuple[str, str], ...]` (string pairs, JSON-free, hashable) **or** a small typed `params: Mapping` frozen at spec construction. Unknown keys for a known `name` fail.
- For `ewma_vol`, params are exactly `SigmaConfig` fields the YAML already owns: `half_life_s`, `min_samples_s`, `jump_threshold_sigma`, `sample_interval_s`, `tau_floor_s`, `sigma_floor`.
- The **plugin** builds the spec(s) from loaded config so YAML remains the author. The **host** does not read `ZGapVolatilityConfig`. Static `Z_GAP_INPUT_CONTRACT` in `facts/mappings.py` may keep a parameterless example spec for catalog tests; the **registered plugin** must supply parameterized specs after `load_config` (e.g. `StrategyPlugin.indicator_specs(config) -> tuple[IndicatorSpec, ...]` defaulting to `input_contract.indicators`). Prefer this over mutating the frozen `InputContract` on the plugin object.
- `IndicatorGraph` constructs `EwmaVolatilityEstimator(SigmaConfig(**from spec.params))`. No silent default when the spec omitted required ewma keys — fail closed.

**Ingest ≠ evaluate (unchanged).**  
Trades/depth always update the graph. `EvaluationScheduler` still decides whether `evaluate()` runs. A Binance trade must not wake ask70. An L2 update must not wake z_gap unless the contract `evaluate_on` says so.

**ask70.** `indicators=()`. Graph may be empty. Bundle is empty. ask70 does not start Binance. Do not start L2 “because the graph exists.”

**z_gap migration.** After the graph is live:

- Delete `ZGapDriver.ingest_volatility` / `seed_volatility` / `self.volatility` as the live ingest path.
- `MarketSessionRuntime.ingest_binance` calls `graph.on_trade(source=binance.spot.trades, ...)` instead of duck-typing the driver.
- Window promotion seeding (`seed_volatility` from `ptb_engine.binance_history`) becomes `graph` seed/on_trade over that history so ACTIVE/PREPARED_NEXT still do not reset σ. The graph must live on the **session runtime**, not only on one driver instance, **or** each window driver receives the same graph reference. Prefer **one graph on `MarketSessionRuntime`**, passed into `evaluate()`; window drivers stay stateless w.r.t. tapes.
- `assemble_zgap_decision_snapshot` still receives a `VolatilitySnapshot`. The z_gap **driver** maps `FeatureBundle` → `VolatilitySnapshot` (adapter in the plugin). Do not make `assemble_*` import `FeatureBundle` keys as a host concern.

**L2.** If `starts_adapter(contract, binance.spot.l2)`, `BinanceDepthWsAdapter.on_update` must call `graph.on_depth(snapshot, source=binance.spot.l2)` in addition to (or instead of only) writing `spot_depth_store`. Keep `DepthStore` if the graph already holds one — do not run two unsynchronized books. Prefer the graph’s `DepthStore` as the single L2 store; `spot_depth_store` can alias it.

### Implementation guide

1. **`indicators/spec.py`** — add params; `instance_id` must include params so two ewma specs cannot collide.
2. **`indicators/graph.py`** — apply params per `name`; fail on unknown `name` or missing ewma keys. Keep `on_depth` / `on_trade` as the only mutators.
3. **`strategies/registry.py`** — optional `indicator_specs(config) -> tuple[IndicatorSpec, ...]`.
4. **`strategies/z_gap/plugin.py`** — build `ewma_vol` spec from `config.volatility`.
5. **`runtime/market_runtime.py`** — construct graph in `__init__` / first bind from plugin specs; `ingest_binance` → `graph.on_trade`; remove `ingest_volatility` duck typing; seed from binance history into the graph on new window.
6. **`runtime/market_data_runtime.py`** — L2 callback → `graph.on_depth`; fix the module docstring (not z_gap-only).
7. **`strategies/protocol.py` + both drivers + `TradingRuntime.on_evaluation`** — add `features: FeatureBundle` argument (empty bundle if no specs). **Do not yet remove** `settlement_reference*` (that is F3) unless you land F2+F3 in one PR — allowed if F3 is in the same change set; otherwise keep kwargs to avoid a broken ask70/z_gap in between.
8. **Tests:**
   - Graph ewma uses spec params, not `SigmaConfig()` defaults (`tests/unit/indicators/`).
   - z_gap driver evaluate with a bundle σ matches previous `ingest_volatility` snapshot (golden or property: same ticks → same `ready` / `sigma` within float tolerance).
   - ask70 contract still starts no Binance; empty bundle.
   - `ingest_binance` does not call a method on ask70 driver.
   - L2 `on_depth` updates imbalance spec; a trade-only spec is unchanged.

### Out of scope

- Perp graph sources.
- Changing z_gap thresholds.
- q_edge indicator list.
- Making `assemble_zgap_decision_snapshot` host-owned.

### Acceptance

1. `grep ingest_volatility src/tyrex_pm` is empty (tests may keep a name only inside z_gap historical comments — prefer delete).
2. Live eval path passes `FeatureBundle`; z_gap σ still driven by YAML volatility fields.
3. ask70 live composition notes still include `adapter=binance.spot.l2 skipped`.
4. Indicator unit tests still do not import Polymarket `BookSnapshot`.

---

## F3 — Generic `evaluate()`; private snapshots stay in the plugin

**Status: Implemented (2026-09-13).** Pre-implementation notes below are the defects this task closed. Chainlink/PTB prints ride on `DecisionContext.references` (`ReferenceFacts`); `evaluate()` has no `settlement_reference*` kwargs.

### Goal

The host calls one driver shape for every strategy:

```text
evaluate(
    market_snapshot: DecisionSnapshot,  # books, quotes, freshness, optional reference
    context: DecisionContext,           # position, caps, notional, now
    features: FeatureBundle,            # subscribed indicators only
    trigger: str,                       # fact id or "timer" / "feed"
    causation_id, correlation_id,
) -> StrategyEvaluation
```

No `settlement_reference` / `settlement_reference_fresh` on the protocol. z_gap continues to build `ZGapDecisionSnapshot` **inside** `strategies/z_gap/` (existing `assemble.py`). ask70 continues to read books from `DecisionSnapshot` and ignore an empty bundle.

### Why

F2 without F3 still ships a Chainlink-shaped door. The next plugin copies ask70’s `del settlement_reference`.

### Implementation guide

1. **Where Chainlink/PTB go.** Prefer, in order:
   - Already on `DecisionSnapshot.reference` / session that `prepare_aligned_eval` used; z_gap driver reads `self.ptb` / `ptb_store` it already holds from `configure_ptb`.
   - If `on_evaluation` must pass last Chainlink without z_gap methods: add an optional frozen `ReferenceFacts` on `DecisionContext` (or a new immutable arg `references: ReferenceFacts | None = None` with default empty) containing only fields any PTB strategy needs: last Chainlink raw, freshness, sealed K if present. **Host fills this from `AlignedEvalReady` without inspecting `strategy_kind`.** If the contract does not require `chainlink.twap` / `ptb.sealed`, leave fields `None`. z_gap assemble maps that into `settlement_ref` internally.
   - Forbidden: `if self.config.strategy_kind == "z_gap"` when stuffing kwargs.

2. **`strategies/protocol.py`** — drop settlement kwargs; require `features`.

3. **`Ask70Driver.evaluate`** — stop `del`ing settlement kwargs; unused `features` is fine.

4. **`ZGapDriver.evaluate`** — `vol` from `features` (F2 mapping); `settlement_ref` from `self.ptb` / `ReferenceFacts` / snapshot; still call `assemble_zgap_decision_snapshot`.

5. **`TradingRuntime.on_evaluation`** — pass `features=market_runtime.indicator_graph.snapshot()` (or equivalent). Stop passing `ready.dyn.chainlink_raw` as evaluate kwargs. If `ReferenceFacts` is chosen, build it from `ready` for every plugin.

6. **Call sites / tests** — every `evaluate(` in `tests/` and runtime must match the new signature. No `**kwargs` swallow.

7. **Docs:** [extending.md](../../latest/extending.md) driver step lists the generic args. [architecture.md](../../latest/architecture.md) evaluate line stays “typed intent”; mention `FeatureBundle`. [config_guide.md](../../latest/config_guide.md) unchanged unless a YAML field moved (it should not).

### Acceptance

1. `grep settlement_reference src/tyrex_pm` is empty except possibly z_gap assemble’s **internal** `settlement_ref=` parameter (plugin-private).
2. ask70 tests pass without Chainlink.
3. z_gap unit tests that called `evaluate(..., settlement_reference=...)` are updated and still assert the same eligibility/intents on fixtures.
4. Host files have no `strategy_kind ==` for this path.

### Out of scope

- Rewriting z_gap valuations.
- Putting fair-value / z / theta on `DecisionSnapshot`.

---

## F4 — Third plugin `feature_probe` (not a research model)

**Status: Implemented (2026-09-13).** Pre-implementation notes below are the defects this task closed.

### Goal

Prove F2+F3 with a **third registered kind** that:

- Declares a contract the shipped plugins do not use (spot L2 + one generic indicator).
- Implements `evaluate()` only against `DecisionSnapshot` + `FeatureBundle` + `DecisionContext`.
- Emits standard intents (or an explicit non-entry action with bundle evidence).
- Adds **zero** lines to `run_config.py` / `trading_runtime.py` beyond registry import in `ensure_strategies_registered()`.

This is toast in the kitchen, not q_edge.

### Locked product choices

- **Kind name:** `feature_probe` (stable, not `q_edge`, not `ask70_l2`).
- **Does not** require Binance perp, funding, PTB, or Chainlink.
- **Default YAML does not live-enter.** `parameters.emit_entry: false` (required explicit bool). When false, the driver never returns `EnterIntent` — only SKIP/HOLD with evidence that named feature keys were present. When true, it may emit one taker `EnterIntent` like ask70 and **must** `requires_protection=True` (validate at plugin `validate_run_config`). Shipped catalog YAML keeps `emit_entry: false` so F4 needs no `--live`.
- **Contract (minimum):**
  - required: `polymarket.books`, `clock.sync`, `account.snapshot`, `binance.spot.l2`
  - indicators: `imbalance` on `binance.spot.l2`, `levels=(1,)`
  - `evaluate_on`: `OnFact(polymarket.books, coalesce_s=1)` and/or `OnFact(binance.spot.l2, coalesce_s=1)`
- **Eligibility:** `strategy_inputs_eligible` if books have asks **and** the imbalance key is in the bundle (or explicitly `None` with blocker `FEATURE_NOT_READY`). Host still reads `result.eligibility` only.

### Implementation guide

Follow [extending.md](../../latest/extending.md) literally:

1. Package `src/tyrex_pm/strategies/feature_probe/` — `config.py`, `schema.py`, `strategy.py`, `driver.py`, `plugin.py`, `reasons.py`. Unknown YAML keys fail.
2. Register in `ensure_strategies_registered()`.
3. YAML `config/runs/feature_probe_catalog.yaml` — host blocks copied from ask70 tiny-live **except** no `protection:` when `emit_entry: false`; `kind: feature_probe`; `risk` still $5 for later optional live. Validate with `--validate-config`.
4. Tests (`tests/unit/strategies/feature_probe/` + facts contract tests):
   - `kind: feature_probe` loads; unknown kind still fails.
   - Closure **starts** `binance.spot.l2` and **does not** start Chainlink / perp / spot trades unless you added trades (do not).
   - ask70 still starts no Binance.
   - `missing_live_adapters` is empty for this contract.
   - Driver with a bundle containing imbalance emits SKIP and evidence key; with empty bundle sets a blocker.
   - `emit_entry: true` without `protection:` fails load; with protection, evaluate can return `EnterIntent` + `TAKER` (unit, no POST).
5. Docs: [extending.md](../../latest/extending.md) live plugins list adds `feature_probe`. [strategy_inputs.md](../../latest/strategy_inputs.md) registered contracts section. [running.md](../../latest/running.md) catalog YAML as validate-only. [config_guide.md](../../latest/config_guide.md) a short `feature_probe` parameters subsection (`emit_entry` only, plus any tau/clock fields you reuse from ask70 if the host still requires `entry_tau_bounds` / `max_clock_uncertainty_ms` on the plugin — **it does**; expose explicit `tau_min_s`, `tau_max_s`, `max_clock_uncertainty_ms` like ask70 so the plugin is complete).

### Out of scope

- q_edge package or economics.
- Perp adapters.
- Making `feature_probe` the default CLI `--config`.
- Live tiny-live admission for this kind (optional later; not F4 acceptance).

### Acceptance

1. `registered_strategy_kinds()` is `ask70`, `feature_probe`, `z_gap`.
2. `q_edge` still absent; `Q_EDGE_INPUT_CONTRACT` still fail-closed on perp.
3. No new `strategy_kind ==` in host modules (`grep` in CI or a unit test that reads those files is unnecessary if review is careful; a test that `run_config.load` for three kinds is enough).
4. Catalog YAML `--validate-config` exits 0.

---

## F5 — Cheap strategy lab: `evaluate()` on fixtures, no `--live`

**Status: Implemented (2026-09-13).** Module: `src/tyrex_pm/lab/evaluate_replay.py`. CLI: `tyrex-pm eval --config … --fixture …` (refuses `--live`).

### Goal

A **deterministic, in-process** path that:

1. Builds `DecisionSnapshot` + `DecisionContext` + `FeatureBundle` from fixture files (and/or by replaying ticks through `IndicatorGraph`).
2. Calls `plugin.create_driver(...).evaluate(...)`.
3. Records `StrategyEvaluation` (action, reason, eligibility, intents) without `PolymarketAsyncGateway`, account REST, or `--live`.

This is not a backtester, not ShadowOMS, and not a second trading host.

### Why after F3

The lab should call the **same** `evaluate()` as production. Before F3 it would have to fake `settlement_reference`. Before F2 it cannot replay L2 indicators generically.

### What already exists (reuse)

- `adapters/polymarket/fixture_source.py` — JSON → dispatcher (books).
- `tests/fixtures/z_gap/`, `tests/fixtures/n2/` — golden ticks.
- Strategy unit tests already call `on_decision` / `evaluate` with hand-built snapshots.
- `FakeTimeAuthority` / `FakeClock`.

Missing: a **shared** helper that any plugin can use, plus one fixture pack for `feature_probe` and a z_gap golden eval that does not import the gateway.

### Implementation guide

1. **Module:** `src/tyrex_pm/lab/evaluate_replay.py` (name stable). Functions, not a runtime class that copies `TradingRuntime`.
   - Input: `StrategyPlugin`, loaded strategy config, a sequence of `ReplayStep` (time, optional `DecisionSnapshot`, optional depth snapshot, optional trade).
   - State: `IndicatorGraph` from `plugin.indicator_specs(config)`; apply depth/trades in order; on steps marked `evaluate=True`, call `driver.evaluate` with `graph.snapshot()`.
   - Output: `tuple[StrategyEvaluation, ...]`.
   - `DecisionContext` for the lab: `entry_allowed=True`, zero position, `target_notional` from the test — explicit, no account authority.
2. **Fixtures:** `tests/fixtures/lab/` — small JSON (do not import `old/tests/fixtures/recordings` as a dependency). One books-only sequence (ask70), one L2 sequence (`feature_probe`), one trades+books sequence (z_gap) **or** reuse `tests/fixtures/z_gap/fair_value_golden.json` if it already has enough to assemble a snapshot.
3. **Tests:** `tests/unit/lab/test_evaluate_replay.py`
   - ask70: book ask ≥ 0.70 → `EnterIntent` (or SKIP if tau/context says so — set tau via snapshot `event_end`).
   - `feature_probe`: imbalance appears in evidence; no `EnterIntent` when `emit_entry: false`.
   - z_gap: at least one SKIP/HOLD with `eligibility` populated; do **not** assert a live edge (economics stay unchanged). If golden fair-value already tests the model, the lab test only proves `evaluate()` is reachable without the gateway.
4. **CLI (optional in F5, do not block acceptance):** `tyrex-pm eval --config ... --fixture ...` that refuses `--live` and writes a JSON list of decisions. If omitted, pytest is enough. Do not add `--mode shadow`.

### Out of scope

- Fill simulation, fee fills, OMS.
- Replaying user-channel / account snapshots (unless a later task needs eligibility that depends on account — lab context should stub `entry_allowed`).
- Replacing tiny-live as the OMS proof (G3, maker, dust stay `--live`).

### Acceptance

1. `pytest tests/unit/lab -q` passes with no network and no `AsyncSecureClient`.
2. Lab code does not import `tyrex_pm.execution.polymarket.gateway`.
3. A contributor can add a strategy test by dropping JSON + calling `evaluate_replay` without copying `ZGapDriver.ingest_volatility`.

---

## After F5 (not this phase)

Only when a **registered** contract requires them:

- Binance perp trades / L2 / funding adapters (extend `LIVE_ADAPTERS`, same graph `on_depth`/`on_trade` sources).
- `q_edge` plugin economics (research task).
- Multi-window / multi-strategy sessions, redeem, raising the debit cap.

G3 (one ask70 `PROTECTION_SL` live print) can proceed in parallel; it does not modify F1–F5 files except config YAML already shipped.

---

## Suggested PRs

Keep reviewable. Do not dump F1–F5 in one commit unless F2+F3 must land together for a green tree.

| PR | Contents |
|----|----------|
| 1 | F1 docs only |
| 2 | F2 graph wiring + z_gap ingest migration + `features=` added (keep settlement kwargs if F3 is not included) |
| 3 | F3 signature cleanup + docs |
| 4 | F4 `feature_probe` + catalog YAML + tests |
| 5 | F5 lab module + fixtures + tests |

---

## Validation

```bash
python -m tyrex_pm.application.cli run --config config/runs/ask70_protection_tiny_live.yaml --validate-config
python -m tyrex_pm.application.cli run --config config/runs/z_gap_tiny_live.yaml --validate-config
python -m tyrex_pm.application.cli run --config config/runs/feature_probe_catalog.yaml --validate-config
pytest -q
```

No `--live` is required to accept this phase.
