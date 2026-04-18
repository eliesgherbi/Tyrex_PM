# Developer guide

**Hub:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Modules:** [modules/README.md](modules/README.md) · **Setup:** [DEVELOPMENT.md](DEVELOPMENT.md)

How code is organized, what each layer is allowed to do, and how to extend the system without breaking the contracts.

---

## 1. Ownership boundaries

| Layer | May import | Must not import |
|-------|------------|-----------------|
| `strategies/` | `signals/`, `core/`, `runtime.config` (typed dataclasses only) | `venue/*`, HTTP/WS clients, `state/*` (read holdings via the value passed in) |
| `risk/` | `core/`, `runtime.config`, `risk/*` | `venue/*`, `strategies/*`, `execution/*` |
| `execution/` | `core/`, `state/*`, `venue/polymarket/*` (live OMS only) | `strategies/*`, `risk/*` |
| `state/` | `core/` | `venue/*`, `risk/*`, `strategies/*` |
| `ingestion/` | `core/`, `state/*` (single-writer per loop), `venue/polymarket/*` | `strategies/*`, `risk/*` |
| `venue/polymarket/` | HTTP/WS libs, `core/` DTOs | `risk/`, `strategies/`, `state/` (the adapter returns DTOs; `state` is mutated by callers) |
| `runtime/` | everything else (this is the wiring layer) | — |
| `reporting/` | `core/` | nothing else (sinks must be side-effect free of business logic) |

The pyramid: **`runtime` wires; `risk` decides; `state` remembers; `venue` speaks; `core` defines the language.**

---

## 2. Code conventions

### 2.1 Decimal arithmetic

Money, sizes, prices, allowances — always `decimal.Decimal`. Never `float`. The risk evidence formatter (`risk/evidence_format.py::s_usd`) quantizes to 6 decimals for fact emission; **don't** quantize in business logic itself.

### 2.2 Time

```python
from tyrex_pm.core.time import utc_now, monotonic_s
```

Never `datetime.utcnow()` or `time.monotonic()` directly — those bypass the centralized clock and break tests that fake time.

### 2.3 Ids

```python
from tyrex_pm.core.ids import ClientOrderId, VenueOrderId, IntentId, RunId, TokenId
```

These are `NewType`-style wrappers around `str` (see `core/ids.py`). Always wrap raw strings before passing them across module boundaries; never `str(some_id)` in business logic except at the reporting / log boundary.

### 2.4 Reason codes

Every risk denial, strategy skip, and pipeline reject **must** carry a stable code from `core/reason_codes.py`. Add new ones there with a docstring explaining when they are emitted; tests grep by code so don't change spellings lightly.

### 2.5 Facts

Every fact type is declared in `reporting/schema_v2.py`. To add a new fact:

1. Add `FACT_TYPE_<NAME> = "<snake_name>"` in `schema_v2.py`.
2. Decide what payload fields are stable join keys vs operator evidence.
3. Decide whether the fact should be deduped by signature (like `reconcile` and `wallet_sync`); if so, the producer must compute the signature deterministically and skip when it equals the last one.
4. Update `reporting_fact_model.md`.

### 2.6 Async style

- Background loops live under `runtime/live_supervisor.py`; they all take a `stop: asyncio.Event` and use `asyncio.wait_for(stop.wait(), timeout=...)` instead of `asyncio.sleep` so shutdown is prompt.
- HTTP calls go through `httpx.AsyncClient` constructed once per loop.
- Sync `py-clob-client` calls are wrapped in `asyncio.to_thread` (`venue.polymarket.clob_bridge.PyClobBridge`).

---

## 3. The contract for `Strategy` implementations

```python
class Strategy:                               # de facto interface; see strategies/base.py
    def on_guru_signal(
        self,
        sig: GuruCopySignal,
        holdings: dict[TokenId, Decimal],
    ) -> tuple[list[Intent], str | None, dict[str, str] | None]:
        ...
```

Returns:

- a list of `Intent`s (empty when filtered out),
- an optional **skip reason code** (constant in `core/reason_codes.py`),
- optional sizing **metadata** merged into the `intent_created` fact (e.g. `{"sizing_mode": "static"}`).

A strategy must **not**:

- read venue state directly (use `holdings` argument; that's all you get for live correctness),
- emit log lines that duplicate what facts will already record,
- mutate any store (the pipeline owns lifecycle calls).

---

## 4. Extension recipes

### 4.1 Add a new risk policy

1. Add `risk/<topic>.py` with a function returning `(ok: bool, reason: str | None, evidence: dict)` or a result dataclass like `evaluate_capital_buy`.
2. Wire it into `risk/engine.py::evaluate_intent` at the right point in the gate sequence (see [Architecture.md §7](Architecture.md#7-the-riskengine-gate-sequence)). New gates that depend on size or price must run **after** `pretrade.apply_notional_min_max` and **before** `venue_min_size.evaluate_venue_min_size`.
3. Expose any operator knobs in `runtime/config.py` and `config/risk/default.yaml`.
4. Add a `tests/test_risk_<topic>.py` golden test.
5. Document the reason code(s) in `core/reason_codes.py` and the policy in [modules/risk/README.md](modules/risk/README.md).

### 4.2 Add a new strategy

1. Create `strategies/<name>/` with `strategy.py`, `filters.py`, `sizing.py` (and `exits.py` if SELL behavior differs).
2. Wire it in `runtime/app.py::cmd_run` (or factor a registry — current code is hand-wired to `GuruFollowStrategy`).
3. Add a strategy YAML under `config/strategies/<name>.yaml` and a scenario under `config/scenarios/`.
4. Add a `tests/test_<name>_strategy_*.py` golden test that exercises filter rejects + sizing math.

### 4.3 Add a new venue

Drop a sibling package under `venue/<name>/` mirroring `venue/polymarket/` (REST clients, WS handlers, normalizers, auth, env helpers). Replace `clob_bridge.PyClobBridge` with the new venue's bridge in `LiveOMS`. The `OMSBackend` Protocol in `execution/adapters.py` is intentionally tiny so any `submit / cancel` backend plugs in.

### 4.4 Add a new fact

See §2.5 above. Adding a fact is cheaper than adding a log line — prefer facts whenever an operator might want to grep, count, or correlate.

---

## 5. Testing patterns to copy

| Pattern | Example |
|---------|---------|
| Golden risk-engine deny → exact reason code + evidence shape | `tests/test_risk_engine.py`, `tests/test_venue_min_size.py` |
| Reconcile state machine — provisional / adoption / tombstone | `tests/test_reconcile_store.py`, `tests/test_venue_adoption_reconcile.py`, `tests/test_inverse_race_tombstone.py` |
| Pipeline end-to-end — facts.jsonl assertions | `tests/test_shadow_e2e.py`, `tests/test_pipeline_dedup_and_wallet_sync.py` |
| Venue/REST normalizer — fixture-driven | `tests/test_data_api_normalize.py`, `tests/test_clob_env_aliases.py` |
| Auto-redirect run dir to `tmp_path` | `tests/test_live_attest_unit.py::_redirect_runs_dir_to_tmp` |

When a bug is fixed, add a regression test that would have caught it. The dedup-broken-by-timestamps fix in `_wallet_sync_signature` is a textbook example: the regression is `test_wallet_sync_signature_ignores_refresh_timestamps` + `test_wallet_sync_dedups_across_refresh_ticks`.

---

## 6. Pull-request checklist

- [ ] All new public functions / dataclasses have a docstring explaining **why** the function exists, not just what it does.
- [ ] Every new risk denial / strategy skip / pipeline reject has a constant in `core/reason_codes.py`.
- [ ] New facts are declared in `reporting/schema_v2.py` and listed in `reporting_fact_model.md`.
- [ ] Money/sizes use `Decimal`; USD evidence uses `s_usd()` from `risk/evidence_format.py`.
- [ ] Tests pass (`pytest`); new behavior has at least one golden test.
- [ ] No new top-level scripts; no `print(...)` for operator output (use facts or `logging`).
- [ ] No imports from `risk/` or `strategies/` into `venue/`, `state/`, or each other (see §1).
- [ ] If you touched configuration: updated [CONFIG_MODEL.md](CONFIG_MODEL.md) and the relevant YAML defaults.
- [ ] If you touched the runtime / live wiring: updated [OPERATIONS.md](OPERATIONS.md) and/or [LIVE_ARCHITECTURE.md](LIVE_ARCHITECTURE.md).
