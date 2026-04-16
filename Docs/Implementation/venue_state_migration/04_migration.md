# Migration plan (Steps 2–5)

Each step has **deliverables**, **rollback**, **go/no-go evidence**, and **decision point**. **Soak durations are full length**; **calendar compression** is by **parallel engineering** during soaks, not shorter soaks.

**Total calendar target: 4–5 weeks** (overlapping Step 2 soak + Step 3 build; overlapping Step 4 soak + Step 5 prep; exact overlap is team capacity).

---

## Step 2 — Reconciliation disable (config only)

### Deliverables

Set **`position_reconciliation_enabled: false`** everywhere live configs currently enable reconciliation.

| File | Current | Target |
|------|---------|--------|
| `config/scenarios/base/live_polymarket_live.yaml` | `true` (~line 61) | `false` |
| `config/scenarios/base/live_polymarket_shadow.yaml` | `true` (~line 62) | `false` |
| `config/scenarios/position_reconciliation_validation/live_polymarket_live.yaml` | `true` | `false` |
| `config/scenarios/position_reconciliation_validation/live_polymarket_shadow.yaml` | `true` | `false` |

**Optional:** `position_reconciliation_enabled: false` in `layer_a_follow/live_polymarket.yaml` / `wallet_sync_live` for clarity (loader default is already `False`, `loaders.py` line 945).

### Parallel work (calendar compression)

During the **full 1-week Step 2 soak**, engineering **in parallel** implements Step 3 (VenueState + flag-default-false + flag-aware `state_readers`) on a branch, with **no production deploy** of Tier A behavior until Step 3 merges **after** Step 2 go/no-go (or merges to main with flag still **false** — production unchanged).

### Remains active after disable

| Component | Behavior |
|-----------|----------|
| `WalletSyncActor` | `_sync_cycle` continues; instrument discovery; `wallet_sync` facts. |
| Reconciliation | Dormant when `position_reconciliation_enabled` is false (`wallet_sync.py` ~471–477). |

### Observable change

- No `position_reconciliation` facts (`facts_v1.py` ~179–188).
- No `event=position_reconciliation_*` / `event=synthetic_close_begin` from `wallet_sync.py`.

### Soak period

**1 week** (full length) live with reconciliation off.

### Rollback

Revert YAML to `position_reconciliation_enabled: true` (only if team decides recon must return — **discouraged** after Step 3 ships).

### Go / no-go to Step 3 merge

| Proceed? | Evidence |
|----------|----------|
| **Go** | Soak clean; Step 3 branch ready; merge Step 3 with **`venue_state_reads_enabled` default `false`**. |
| **No-go** | Production incident attributed to recon off — hold; do not enable Tier A flag until resolved. |

---

## Step 3 — VenueState + feature flag (production behavior unchanged for Tier A)

### Deliverables

- **`src/tyrex_pm/runtime/venue_state.py`** per `03_venue_state_design.md` (cost basis math applies when flag **true** in Step 4; `VenueState` still stores raw sizes).
- **WalletSync →** `apply_positions_and_orders_rows` after successful HTTP (`wallet_sync.py` ~305–513).
- **CLOB cash poll** on `VenueState` (default **10 s**, floor **3 s**).
- **`RuntimeSettings` + loaders:** `venue_state_reads_enabled` (default **`false`**), `venue_state_*` keys (`03_venue_state_design.md`).
- **`state_readers.py`:** Tier A code paths **branch** on `venue_state_reads_enabled` — **false** → existing `Cache` / `Portfolio` behavior (cited in `02_architecture.md`); **true** → `VenueState`.
- **`guru_compose.py`:** Wire `VenueState`, pass flag + `VenueState` into readers / deployment budget / layer A / capital as designed.
- **Readiness:** Implement **two gates** — `wallet_sync_first_sync_complete` **and** `venue_state_cash_ready` (`03_venue_state_design.md`).
- **Facts:** Register **`venue_state`** (unified schema) and **`venue_state_missing_mark`** in `facts_v1.py` (`06_observability.md`).

### Production behavior at end of Step 3

- **`venue_state_reads_enabled: false`** (default) in all production YAML — **Tier A identical to pre-VenueState** (cache/portfolio).
- **`VenueState`** still runs, facts emit, gates measurable — validates wiring **before** flag flip.

### Soak / validation for Step 3

**Short validation window** (not a substitute for Step 4 soak): staging or canary with flag **false**, confirm no regressions, `venue_state` heartbeats present, both gates eventually **true**.

### Rollback

Revert Step 3 merge (remove VenueState) **only if** critical breakage with flag **false** — should be rare if flag paths are correct.

### Go / no-go to Step 4

| Proceed? | Evidence |
|----------|----------|
| **Go** | Tests pass (`05_tests.md`); CI green; ops comfortable with observability. |
| **No-go** | Gates never achieve `venue_state_cash_ready`, deadlock, or rate-limit storms — fix before flag flip. |

---

## Step 4 — Enable Tier A (`venue_state_reads_enabled: true`)

### Deliverables

**Single production change (primary):** Set **`venue_state_reads_enabled: true`** in target live YAML(s).

### Behavior

- **Instant Tier A switch** — all flag-aware `state_readers` / deployment / layer A / bot_sell / capital Tier A paths read **`VenueState`**.
- **Filled deployment** uses **venue size × mark** with **0.5** fallback + **`venue_state_missing_mark`** (`03_venue_state_design.md`).

### Rollback

**Instant:** set **`venue_state_reads_enabled: false`** in YAML and reload/redeploy — Tier A returns to cache/portfolio **without** redeploying Step 3 code removal.

**Note:** The flag is a **migration tool only** and is **removed in Step 5** — not a permanent dual-mode architecture (`02_architecture.md`).

### Soak period

**2 weeks** (full length) live with flag **true**; monitor `venue_state`, `venue_state_missing_mark`, caps vs manual checks.

### Go / no-go to Step 5

| Proceed? | Evidence |
|----------|----------|
| **Go** | Soak clean; no systematic cap errors; team authorizes deletion PR. |
| **No-go** | Flip flag **false**; fix; repeat soak. |

---

## Step 5 — Deletion: reconciliation + migration flag

**Precondition:** Step 4 **2-week** soak complete and sign-off.

### Deletion order

1. **Code — reconciliation:** Remove `_reconciliation_pass`, `_apply_reconciliation_actions`, `_send_synthetic_close`, `_build_cache_position_map`, races B/C/E from `wallet_sync.py` (~545–920+); trim `WalletSyncConfig` / `WalletSyncResult` / `guru_compose.py` recon wiring (~375–380).
2. **Code — migration flag:** Remove **`venue_state_reads_enabled`** branches from `state_readers.py`, `deployment_budget.py`, `guru_compose.py`, etc. **Tier A always** uses **`VenueState`** (no cache/portfolio Tier A path).
3. **Tests:** Delete or shrink `tests/unit/test_position_reconciliation.py`; update flag tests removed.
4. **Facts schema:** Remove `position_reconciliation` from `facts_v1.py`.
5. **Loader / `RuntimeSettings`:** Remove `position_reconciliation_*`, `reconcile_venue_has_more`, `recently_reconciled_ttl_seconds`; remove **`venue_state_reads_enabled`**.
6. **Config YAML last:** Remove obsolete recon keys and **`venue_state_reads_enabled`** from all scenarios — **last**, so rollback is config-only during the tail of the program.

### Archive

- `docs/implementation/venue_sync_truth/position_reconciliation/` → **`docs/implementation/venue_sync_truth/_archive/position_reconciliation/`** (or equivalent).

### Rollback

Step 5 is **not** rollback-friendly without git revert — **only** execute after Step 4 soak sign-off.

### Go / no-go (program complete)

| Proceed? | Evidence |
|----------|----------|
| **Go** | CI green; no `position_reconciliation` / `venue_state_reads_enabled` references; `WalletSyncActor` + `VenueState` operational. |

---

## Program answers (authorization checklist)

| Question | Answer |
|----------|--------|
| **What code ships when?** | **Step 2:** YAML only. **Step 3:** `venue_state.py`, wallet sync feed, flag-aware `state_readers`, compose, facts, gates, loaders — **flag default false**. **Step 4:** YAML **`venue_state_reads_enabled: true`**. **Step 5:** Delete recon + delete flag branches + remove keys (order above). |
| **What is deleted when?** | Reconciliation implementation **Step 5**; **`venue_state_reads_enabled`** **Step 5** after stable Tier A soak. |
| **Soaks** | Step 2: **1 week**; Step 4: **2 weeks** — **full length**, not shortened. |
| **Rollback** | Step 4: **flag false**; Step 5: **git revert** if needed. |
| **Operator signals** | **`venue_state`** (unified `status`), **`venue_state_missing_mark`**, existing **`wallet_sync`**; logs in `06_observability.md`. |
| **Migration complete evidence** | No recon facts; no `venue_state_reads_enabled` in repo; Tier A only via `VenueState`; two-gate readiness green. |

---

## Reviewability

| Step | Review focus |
|------|--------------|
| 1 | `01_inventory.md` — **frozen**. |
| 2 | Config diff only. |
| 3 | Code + **default false** — Tier A behavior proof via tests with flag toggled. |
| 4 | Single YAML flip + soak plan. |
| 5 | Deletion diff + schema cleanup. |
