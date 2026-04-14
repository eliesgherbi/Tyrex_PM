# Collateral / capital unification — implementation plan

## 1. Objective

Establish **one explicit CapitalState contract** and **one ownership model** for collateral/allowance/balance visibility so risk, startup readiness, and reporting do **not** depend on duplicate HTTP paths or ambiguous freshness.

## 2. Scope

- **In:** USDC collateral / allowance used by Tyrex **capital gate** and observability; alignment with Polymarket adapter account updates.
- **Out:** Guru PnL, mark-to-market reporting (separate); deployment caps (still `NautilusDeploymentBudget`).

## 3. Clean ownership boundary

| Component | Owns |
|-----------|------|
| **Polymarket adapter** | `PolymarketExecutionClient._update_account_state` → `generate_account_state` (`adapters/polymarket/execution.py`) — **venue-reported** collateral into Nautilus account model |
| **Nautilus `Portfolio` / account** | Persisted framework account state consumed via `Portfolio.account(venue)` |
| **Tyrex** | **Single** `CapitalStateProvider` that reads **only** the agreed source(s) below; **no** second ad hoc `get_balance_allowance` for the same semantic in risk |

## 4. Framework / adapter capabilities already available

- Adapter pushes collateral balance into framework: `_update_account_state` using `get_balance_allowance(AssetType.COLLATERAL)` (`execution.py`).
- Tyrex today: `NautilusAccountSnapshotProvider.snapshot` → `Portfolio.account` (`tyrex_pm/runtime/state_readers.py`).
- Tyrex today: **parallel** `ClobAllowanceStateProvider` → direct `ClobClient.get_balance_allowance` (`state_readers.py`) used for risk/observability.

## 5. What Tyrex must add

1. **`CapitalState` DTO:** `free_collateral_usd`, `allowance_usd` (if still required for gate), `captured_at_utc`, `source` enum (`ADAPTER_ACCOUNT`, `EXPLICIT_REFRESH`), `stale_after_seconds`, `ok: bool`, `error: str | None`.
2. **`CapitalStateProvider` (single):**
   - **Primary:** Derive from **Nautilus portfolio account** snapshot (same venue as trading) **after** adapter has had opportunity to refresh (post-connect trade lifecycle also refreshes adapter in `execution.py` on finalized trades).
   - **Secondary refresh:** One **optional** explicit `QueryAccount`-style path **through Nautilus** if exposed, **not** duplicate py-clob in risk—if Tyrex must call py-clob, it lives **only** inside this provider as **implementation detail** with a **single** call site.
3. **Deprecate** direct `ClobAllowanceStateProvider` usage from `ConfiguredRiskPolicy` once provider is wired; keep adapter class only if needed for non-Nautilus tests, marked deprecated.

## 6. What Tyrex must not own

- Multiple competing “true” capital numbers in one risk evaluation.
- Strategy-level clob calls for allowance.

## 7. Required interfaces / contracts

```text
CapitalState (immutable snapshot)
CapitalStateProvider
  def snapshot(self, *, purpose: Literal["risk_gate", "observability"]) -> CapitalState
  def freshness_ok(self, state: CapitalState) -> bool
```

**Owner:** `tyrex_pm/runtime/capital/` (new) or extend `state_readers.py` with a single façade class.

## 8. Lifecycle behavior

- **Startup:** After exec connect, provider may return `UNKNOWN` until first adapter account event; **readiness** uses `freshness_ok` (see `startup_readiness.md`).
- **Live:** Risk gate reads provider each evaluation or uses cached snapshot with TTL from runtime YAML.
- **Shutdown:** Final `CapitalState` snapshot in manifest optional.

## 9. Module responsibilities

| Module | Responsibility |
|--------|----------------|
| **CapitalStateProvider** | Single read/merge policy |
| **Risk** | Consumes `CapitalState` only via provider |
| **Reporting** | Emits capital facts from same snapshot |
| **Adapter** | Continues to own HTTP to Polymarket for account **push** into Nautilus |

## 10. Dependencies on other plans

- **Before** `startup_readiness.md` (readiness precondition on capital freshness).
- **Feeds** `tradable_state_health.md` / `RiskStateView` composition.

## 11. Implementation steps

1. Audit all `get_balance_allowance` / `ClobAllowanceStateProvider` call sites (`grep`).
2. Implement `CapitalStateProvider` backed by `NautilusAccountSnapshotProvider` + documented stale rules.
3. Migrate `ConfiguredRiskPolicy` capital gate to provider.
4. Migrate reporting capital snapshots to same provider.
5. Deprecate duplicate paths; document in `CONFIG_MODEL.md` / migration note.

## 12. Tests / validation strategy

- Unit: provider with mocked `Portfolio.account` payloads.
- Contract: single call path per evaluation (assert no double clob in risk).

## 13. Observability / reporting needs

- Facts: include `capital_source`, `capital_stale`, `capital_age_ms`.
- Manifest: last capital snapshot summary.

## 14. Pre-coding decisions that must be frozen

1. **Stale threshold** seconds for capital (default proposal: `max(account_snapshot_age from risk, 30s)` — tune with ops).
2. Whether **allowance** remains a **separate** field or is folded into **account** object only—**must match** what adapter puts in `generate_account_state`.
3. **EOA vs proxy** signature type behavior unchanged—document funder vs signer.

## 15. Phase readiness and portfolio-account verification

### 15.1 Phase readiness (this doc)

| Workstream | Codable now? | Spike question | Spike exit criterion |
|------------|----------------|----------------|---------------------|
| `CapitalState` DTO + single provider + deprecation of duplicate risk paths | **Yes** | — | — |
| `freshness_ok` thresholds and stale reporting | **Yes** (defaults in §14) | — | — |
| **Production** gate trusting `Portfolio.account` alone for allowance + balance | **No** | Does adapter-filled `Portfolio.account` after refresh always expose **both** semantics the gate needs? | Documented yes → read only account; **no** → **one** supplemental refresh **inside** provider only; contract tests |

**Parallel work:** Phases 2–3 can code against **`CapitalState` stubs**; startup readiness **§8.2 capital-fresh** clause in **live** hardens after §15.1 row 3 exits. Program summary: [`README.md`](README.md) §9.1 Phase 1.

### 15.2 Residual note

Until §15.1 exits, treat capital precondition as **conservative**: if provider cannot prove freshness/shape, readiness remains **NOT_READY** (per [`startup_readiness.md`](startup_readiness.md) §8.2).
