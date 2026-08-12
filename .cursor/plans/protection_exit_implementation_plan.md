---
name: Protection Exit Implementation
overview: Implement a strategy-agnostic three-plane exit system (strategy policy / risk-safety / protection overlay), extend intents for ProtectionSpec + cancel path, add reactive SL/TP/trailing first, then a non-profit ask70 harness strategy to validate live, then resting GTC TP + OCO.
todos:
  - id: p0-foundation
    content: "P0 Freeze three-plane model, docs, ProtectionSpec contracts, intent/reporting extensions, precedence, multi-strategy driver port"
    status: pending
  - id: p1-reactive-engine
    content: "P1 Pure trigger eval + ProtectionRegistry + runtime arm-on-fill + monitor emitting Exit/Flatten intents (reactive SL/TP/trail)"
    status: pending
  - id: p2-ask70-harness
    content: "P2 ask70 harness strategy + run YAML + unit tests (enter first leg ask>=0.70, attach ProtectionSpec)"
    status: pending
  - id: p3-live-validate
    content: "P3 Tiny-live validation run with ask70 + reactive brackets; fix dust/unknown-inventory blockers as found"
    status: pending
  - id: p4-resting-tp
    content: "P4 Resting GTC TP + CancelIntent consumer + soft OCO + TP watchdog"
    status: pending
  - id: p5-polish
    content: "P5 Dust policy, semantic dedupe/attempt_id, reporting fields, optional partial exits (later)"
    status: pending
isProject: false
---

# Protection / SL / TP / Trailing — Implementation Plan

## Verdict

The research plan (`.cursor/plans/sl_tp_protection_research_30645359.plan.md`) is directionally correct. This plan **keeps that architecture** and turns it into a build sequence:

1. Freeze the **three-plane exit model** and shared contracts.
2. Ship a **framework protection engine** that reuses today’s FAK exit lifecycle (reactive SL / reactive TP / trailing).
3. Add a **throwaway harness strategy** (`ask70`) that only exists to enter fast and validate brackets.
4. Only then add **resting GTC TP + CancelIntent OCO** (harder execution surface).

Do **not** implement SL/TP inside Z-Gap. Z-Gap keeps thesis/rich/time; protection is orthogonal and reusable.

---

## Locked architecture (foundation to freeze)

### Three planes (all emit the same intents)

```text
PLANE A — STRATEGY POLICY
  z_gap: thesis / rich / time
  ask70: none (entry only; exits via protection)
  → ExitIntent | FlattenIntent | (optional HoldToResolution later)

PLANE B — RISK / SAFETY (runtime today)
  kill, mandatory_exit_before_end_s, crash flatten, manual deadline
  → FlattenIntent | MANUAL

PLANE C — PROTECTION OVERLAY (new, framework-owned)
  SL / TP / trailing armed after confirmed fill
  → ExitIntent | FlattenIntent | CancelIntent
         │
         ▼
   existing planner → lifecycle FAK sell → reconcile flat
```

### Ownership (hybrid — confirmed)

| Concern | Owner |
|---------|--------|
| Whether brackets exist; thresholds; mark preference | Strategy / run YAML via `ProtectionSpec` |
| Arm after confirmed fill; peak; trigger eval; OCO | Framework `protection/` |
| Thesis / rich / time | Strategy only |
| Mandatory / crash / manual | Runtime (unchanged, outranks brackets) |
| Inventory / sellable shares / POST | Account + lifecycle + coordinator |

### Precedence (high → low)

1. Kill / emergency / unknown (safety)
2. Runtime mandatory flatten / manual deadline
3. Hard SL fire (protective flatten)
4. Strategy policy exits (cancel brackets first)
5. Trailing fire
6. TP (reactive fire or resting fill)
7. Hold

### Explicit non-goals for v1

- Profitable strategy design
- Hold-to-resolution completion
- Partial / scale-out exits (contract may allow later; harness uses full flat)
- Putting mark monitors inside each strategy tick as the only path
- Resurrecting `old/src/tyrex_pm/protection/` verbatim

---

## Gap check vs research plan (still true in code)

| Research claim | Current code | Action |
|----------------|--------------|--------|
| Intent spine ready | Yes — `ExitIntent` / `FlattenIntent` → FAK | Reuse |
| `CancelIntent` exists | Yes — unused by runtime | Wire in P4 |
| `LimitOrderSpec` GTC | Gateway can sign; planner/lifecycle don’t place resting TP | Wire in P4 |
| `ExitIntent` full-flat only | Enforced `target_flat=True` | Keep for P1–P3; revisit in P5 |
| No `protection/` live package | True | Create in P1 |
| Multi-strategy port | Runtime composed around Z-Gap | Generalize in P0/P2 |
| “Protection price” naming | Tick floor/ceiling, not SL/TP | Keep name; new reason codes `PROTECTION_*` |

Known live bugs to fix when they block validation (P3, not blockers for unit work):

- Session-owned inventory treated as `unknown_inventory` / `PRIOR_POSITION` → strategy exits blocked (protection must still work via runtime tick).
- Dust residual → `OPEN_EXPOSURE` / MANUAL with no dust policy.

---

## Harness strategy: `ask70` (development only)

**Purpose:** enter quickly on a liquid book so protection features can be exercised live. Not alpha.

### Behavior

```text
Flat + entry_allowed:
  Scan UP and DOWN best asks (or configured preferred order).
  First leg whose best_ask >= entry_ask_threshold (default 0.70)
    → EnterIntent(BUY that outcome, max_price ≈ ask + slip or fixed ceiling)
  Attach ProtectionSpec from run YAML (or intent evidence).

Active:
  Emit no economic exits (HOLD).
  Framework protection owns SL / TP / trailing.
  Runtime mandatory flatten remains the backstop.
```

### Suggested config (`config/runs/ask70_protection_tiny_live.yaml`)

```yaml
strategy:
  kind: ask70
  parameters:
    entry_ask_threshold: "0.70"
    leg_preference: first_hit   # or up_then_down
    max_price_pad: "0.02"       # allow fill above threshold
protection:
  mark: bid
  take_profit:
    style: reactive_fak         # P1; later resting_gtc
    absolute: "0.85"            # or pct_from_entry
  stop_loss:
    style: stop_market_fak
    absolute: "0.55"
  trailing:
    enabled: true
    activation: immediate       # or after_profit
    trail_abs: "0.05"           # preferred over % for 5m binaries
    mark: bid
risk:
  target_notional: "5"
  maximum_total_debit: "5"
lifecycle:
  mandatory_exit_before_end_s: 90
  manual_deadline_before_end_s: 45
  minimum_exit_price: "0.01"
```

Thresholds are **tunable for the test**, not sacred.

### Why this validates the system

| Feature | How ask70 proves it |
|---------|---------------------|
| Arm-on-fill | Enter once → registry ARMED with entry VWAP/qty from journal |
| TP | Bid reaches TP → ExitIntent → flat |
| SL | Bid breaches SL → FlattenIntent → flat |
| Trailing | Peak tracks up; pullback `trail_abs` → flatten |
| Safety outrank | Near window end, mandatory flatten still wins |
| Strategy-agnostic | Same engine later used by z_gap without rewrite |

---

## Contract extensions (P0)

### 1. `ProtectionSpec` (new, frozen dataclass)

Location: `src/tyrex_pm/protection/spec.py` (or `core/protection_spec.py` if we want core-only purity; prefer `protection/` package).

Conceptual fields:

- `take_profit`: optional `{absolute?, pct_from_entry?, style: reactive_fak|resting_gtc}`
- `stop_loss`: optional `{absolute?, pct_from_entry?, style: stop_market_fak, confirm_ms?}`
- `trailing`: optional `{enabled, activation, trail_abs?, trail_pct?, mark}`
- `mark_default`: `bid` (required default for reactive sells)
- `size_mode`: `full` only in P1–P3
- `dust_policy`: `reactive_only | skip_resting | ride` (defaults; enforce in P4/P5)

Resolution rule: absolute wins if both absolute and pct set (or reject at validate — pick one and document).

### 2. Attach point

Preferred for harness: **run-config defaults** keyed by strategy, armed by framework after fill (strategy does not have to embed spec on every intent).

Also allow `EnterIntent.evidence["protection"]` or a typed optional field later — start with run-config to avoid widening EnterIntent too early.

### 3. Intent / runtime plumbing

- Keep `ExitIntent` / `FlattenIntent` as the only sell desires in P1–P3.
- Add reason codes: `PROTECTION_SL`, `PROTECTION_TP`, `PROTECTION_TRAIL`, `PROTECTION_OCO_CANCEL`.
- Ensure `semantic_key` / `attempt_id` so a later protection exit is not swallowed after a prior HOLD-era key collision (fix dedupe for exit reasons).
- `CancelIntent`: no consumer until P4; define the API in P0 docs so P4 is not a redesign.

### 4. Multi-strategy composition

Today runtime is Z-Gap-shaped. P0/P2 must:

- Select driver from `strategy.kind` (`z_gap` | `ask70`).
- Keep `TradingRuntime` / lifecycle / gateway shared (per `Docs/latest/extending.md`).

---

## Package sketch

```text
src/tyrex_pm/protection/
  __init__.py
  spec.py           # ProtectionSpec + validation
  triggers.py       # pure: evaluate(mark, state, spec) → TriggerDecision
  state.py          # ArmedProtection / peak / thresholds / phase
  registry.py       # session_id → state; serialize for journal/recovery
  monitor.py        # on_book / on_tick → intents only (no SDK)

src/tyrex_pm/strategies/ask70/
  config.py, schema.py, strategy.py, driver.py, reasons.py
```

Pure trigger math must be unit-tested without runtime.

---

## Phased build

### P0 — Foundation (docs + contracts + ports)

**Deliverables**

- Short doc section in `Docs/latest/architecture.md` + `execution_lifecycle.md`: three planes, precedence, ProtectionSpec, naming note (“protection price” ≠ SL/TP).
- `ProtectionSpec` + YAML schema under run config (`protection:` block).
- Strategy kind registry hook (at least stub for `ask70`).
- Exit intent dedupe fix: include `reason_code` or require `attempt_id` for protection exits.
- Unit tests: spec validation, precedence table (doc + code constants).

**Exit criteria:** specs parse; no behavior change to z_gap live path yet.

---

### P1 — Reactive protection engine (SL + TP + trailing)

**Deliverables**

- Pure `triggers.py`:
  - SL: `mark <= stop` → fire
  - TP reactive: `mark >= tp` → fire
  - Trail: update peak on mark; fire when `peak - mark >= trail_abs` (or pct)
- `registry` + arming on **confirmed exposure** (entry VWAP / confirmed shares from lifecycle/journal — not strategy guess).
- `TradingRuntime` integration:
  - After arm: each async tick / evaluation with book bid → `monitor.evaluate` → enqueue intents.
  - Protection does **not** require model_ready.
  - On fire: emit `FlattenIntent` (SL/trail) or `ExitIntent` (TP); set reason codes.
- Soft invariants without resting orders yet: size = full confirmed sellable; one working exit at a time (reuse `_EXIT_BUSY` / lifecycle).
- Unit tests: trigger matrix, arm/disarm, trail peak updates, mandatory flatten still outranks (ordering test).

**Exit criteria:** fake/sim tests show SL, TP, trail each produce intents and lifecycle reaches flat (or MANUAL on injected reject).

**Note:** Trailing in P1 is intentional — it is still reactive FAK, no new order type. Resting TP stays P4.

---

### P2 — `ask70` harness strategy

**Deliverables**

- `strategies/ask70/` per extending guide (config, schema, driver, tests).
- Entry: first leg with `best_ask >= 0.70` (deterministic tie-break documented).
- No strategy exits.
- Run YAML `config/runs/ask70_protection_tiny_live.yaml`.
- Wire `strategy.kind: ask70` in composition/CLI path.
- Unit tests: entry selection, no-entry below threshold, ProtectionSpec loaded from run config.

**Exit criteria:** observe/shadow or fake-book test enters once and arms protection.

---

### P3 — Tiny-live validation (feature proof)

**Deliverables**

- Operator runbook snippet (commands, expected journal fields).
- One tiny-live run with wide brackets so *some* path fires (or force SL by tight stop).
- Fix only blockers found in that run:
  - dust / invalid maker amount
  - unknown_inventory blocking if it also blocks protection arming
  - sellable race on exit retry

**Exit criteria:** report shows `PROTECTION_*` reason (or mandatory flatten if brackets missed) and session terminal flat or documented MANUAL dust.

---

### P4 — Resting GTC TP + CancelIntent OCO

**Deliverables**

- Planner/lifecycle role for **working resting sell** (GTC), distinct from FAK exit retries.
- Place TP after arm when `style=resting_gtc` and size ≥ venue min (~5 shares); else fall back to reactive.
- `CancelIntent` consumed before SL/trail/strategy exit / mandatory flatten.
- Watchdog: missing TP → re-place or escalate; unexpected fill → disarm + reconcile.
- Persist resting order id in journal for crash recovery.

**Exit criteria:** unit + fake tests for cancel-before-sell; no double-sell.

---

### P5 — Polish (after MVP works)

- Dust policy (skip / reactive-only / ride).
- Optional partial exits (`ExitIntent` beyond full flat) — **only if** needed; not required for ask70.
- Richer reporting: trigger mark, gap-through, slip, peak, dust-unprotected.
- Optional: z_gap can attach ProtectionSpec alongside thesis (does not replace thesis).

---

## Mapping research phases → this plan

| Research roadmap | This plan |
|------------------|-----------|
| Reactive brackets | **P1** |
| Resting GTC TP + OCO | **P4** |
| Trailing SM | **P1** (reactive trail); refine in P4/P5 if needed |
| Partial exits | **P5** (deferred) |
| Sports policies | Out of scope |
| + Foundation freeze | **P0** (new, required) |
| + Dev harness strategy | **P2–P3** (new, required for validation) |

---

## Test strategy

| Layer | What |
|-------|------|
| Unit | ProtectionSpec validate; trigger SL/TP/trail; trail peak; precedence vs mandatory |
| Unit | ask70 entry gates; no strategy exit intents |
| Contract | Runtime arms on confirmed fill; emits intents; lifecycle flatten |
| Tiny-live | ask70 + reactive brackets on $5 debit |

Do not require full suite green only on z_gap — add targeted protection/ask70 tests and keep existing suite passing.

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Mid mark false triggers | Default mark=`bid` for all reactive sells |
| GTC min size → naked position | P1 reactive-only; P4 dust fallback to reactive |
| Double sell (TP fill + SL) | Single exit authority; cancel-before-sell in P4; busy phase in P1 |
| Crash mid-bracket | Persist armed state + resting ids; recovery flatten already exists |
| ask70 never enters (ask never ≥0.70) | Lower threshold in YAML for test windows; or `entry_ask_threshold: "0.40"` for dry runs |
| Intent dedupe drops second exit | P0 attempt_id / reason in semantic_key |
| Scope creep into z_gap rewrite | ask70 is the only consumer until P5 optional attach |

---

## Recommended build order (summary)

```text
P0 Foundation (contracts, docs, multi-strategy hook, dedupe)
 → P1 Reactive protection (SL + TP + trail on bid)
 → P2 ask70 harness + YAML
 → P3 Tiny-live prove
 → P4 Resting TP + CancelIntent OCO
 → P5 Dust / reporting / optional partials / optional z_gap attach
```

**First vertical slice that proves value:** P0 + P1 + P2 + P3 with `take_profit.style=reactive_fak`, hard SL, and `trail_abs`.

---

## Acceptance for “done enough to trust the foundation”

1. Docs state three-plane model and precedence.
2. `ProtectionSpec` is the only way brackets are declared.
3. Framework arms/monitors/fires; ask70 only enters.
4. SL, TP, and trailing each have unit proof + at least one live or high-fidelity path.
5. Existing z_gap path unchanged except shared hooks (no forced brackets).
6. Mandatory flatten still outranks brackets.

---

## Out of scope until explicitly requested

- Implementing code in this chat without a go-ahead
- Replacing z_gap as default live strategy
- Native venue OCO (does not exist)
- Opposite-token “hedge close”
