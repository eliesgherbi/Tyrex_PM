# Layer A — Rule 1: Exit interpretation (`exit_filter`, `full_exit`)

**Status:** planning only.

**Rule kind:** **Interpretation** (not a significance gate). It defines **how exit quantity is derived** before risk, not whether the guru “is significant.”

---

## 1. Purpose

When the guru **SELLs** on an allowlisted outcome, control **follower exit sizing**:

| Mode | Behavior |
|------|----------|
| **`mirror_guru`** (default) | Same as today: follower SELL qty from **`SizingPolicy.size(sig, branch="exit")`** — proportional to **`guru_size_raw × copy_scale`** (exit branch ignores conviction overlay per `src/tyrex_pm/signal/sizing.py`). |
| **`full_exit`** | Follower SELL qty = **entire follower long position** on that outcome **`token_id`** (from injected **`LayerAContext`**), then **risk** and execution quantize as usual. |

**`full_exit`** is an **exit interpretation mode**: it **replaces** guru-proportional exit sizing for quantity derivation **only**. It does **not** bypass risk, capital gate, or deployment caps.

---

## 2. Applicability

- **`branch == "exit"`** only (`msg.side == "SELL"` in `CopyStrategy._on_guru_trade`).
- Runs **after** token allowlist gating (same order as `00_general_plan.md` §6).

---

## 3. Config (v1)

```yaml
filters:
  exit_filter:
    enabled: false
    exit_method: mirror_guru   # mirror_guru | full_exit
```

| Field | Validation |
|--------|------------|
| `enabled` | bool; default `false` if `filters` or block omitted |
| `exit_method` | Required when `enabled: true`. Allowed: `full_exit` only for non-mirror semantics in v1. When `enabled: false`, loader treats mode as **`mirror_guru`** (field may be omitted). |
| Unknown `exit_method` | **Loader error** |

**Note:** With `enabled: false`, **`mirror_guru`** is implicit — no YAML change for current operators.

---

## 4. Frozen failure behavior for `full_exit`

**Injected dependency:** **`LayerAContext.follower_long_qty_for_outcome_token(token_id: str) -> float | None`** (exact method name implementation-defined; semantics below).

**Interpretation:** return value is **net long position quantity** in **outcome-token / instrument terms** consistent with **`OrderIntent.quantity`** + **`NautilusGuruExecutionPort`** (implementation must verify sign convention once).

When **`exit_method == full_exit`** and **`enabled: true`**:

| Condition | Outcome |
|-----------|---------|
| `token_id` missing / empty | **Deny** — should not occur after token gate; if it does: `LAYER_A_EXIT_FULL_DENIED_INVALID_TOKEN` |
| Context returns **`None`** (instrument **unresolved**, wallet read not possible, or **explicit** unreadable) | **Deny** — `LAYER_A_EXIT_FULL_DENIED_UNRESOLVED` |
| Context raises or runtime signals **read error** | **Deny** — `LAYER_A_EXIT_FULL_DENIED_UNREADABLE` (log exception detail at WARN **once** per signal, not full stack at INFO) |
| Context returns **`0.0`** or **any value `<= 0`** | **Deny** — `LAYER_A_EXIT_FULL_DENIED_NO_POSITION` |

**Explicit prohibitions (v1):**

- **Do not** silently pass through with **qty 0** to sizing/risk.
- **Do not** silently **fall back** to mirror-guru sizing on failure.
- **Do not** embed **`Portfolio`** / **`Cache`** reads inside **`signal/layer_a`** or **`CopyStrategy`** — only **`LayerAContext`** implemented in **`runtime/`**.

On **deny:** final **`strategy_decision`** = skip; emit **`layer_a_filter`** for `exit_interpretation` with the **same** `reason_code`.

On **accept:** `metadata` **must** include at minimum:

```json
{
  "exit_qty_mode": "full_position",
  "follower_position_qty": <float>
}
```

(`follower_position_qty` included for observability; strategy still uses context or metadata consistently in one place — pick single source of truth in implementation to avoid drift.)

---

## 5. Success path interaction with sizing and risk

1. Orchestrator returns **`accept=True`** with **`exit_qty_mode: full_position`** and qty source in metadata.
2. **`CopyStrategy._handle_branch`**: **skip** **`SizingPolicy.size(..., branch="exit")`** for quantity; set **`qty = follower_position_qty`** from metadata or a single follow-up context read (prefer **one** read per signal — implementation choice: cache result in orchestrator outcome).
3. Build **`OrderIntent`** with `side=SELL`, `signal_kind=exit`, `quantity=qty`, `price_ref` from guru signal (unchanged).
4. **`ConfiguredRiskPolicy.evaluate`**: unchanged — may clip/deny.
5. Execution: unchanged.

**`mirror_guru`:** `metadata` should still record `exit_qty_mode: mirror_guru` for consistent **`layer_a_filter`** telemetry (optional but recommended).

---

## 6. Reason codes (`core/reason_codes.py`)

Add stable **`StrEnum`** / string constants (exact names may be prefixed `LAYER_A_` or `FILTER_` — align with maintainers; use consistently):

| Code | When |
|------|------|
| `LAYER_A_EXIT_FULL_DENIED_UNRESOLVED` | `None` from context (no position / instrument mapping / safe read) |
| `LAYER_A_EXIT_FULL_DENIED_UNREADABLE` | Exception or explicit read failure from runtime adapter |
| `LAYER_A_EXIT_FULL_DENIED_NO_POSITION` | Resolved qty `<= 0` |
| `LAYER_A_EXIT_FULL_DENIED_INVALID_TOKEN` | Defensive: missing token after gate |
| `LAYER_A_EXIT_INTERPRETATION_OK` | Pass (interpretation succeeded; optional — final `strategy_decision` may still use `GURU_EXIT_MIRROR` for accept if desired for continuity — **implementation:** pick one final accept code and document in CONFIG_MODEL) |

**Recommendation:** On **accept** `full_exit`, use **`LAYER_A_EXIT_INTERPRETATION_OK`** in **`layer_a_filter`**; **`strategy_decision`** may keep **`GURU_EXIT_MIRROR`** or add **`GURU_EXIT_FULL`** — **freeze at implementation** with one line in CONFIG_MODEL (minor).

---

## 7. Runtime wiring

1. **`Protocol` `LayerAContext`** in `signal/layer_a/types.py`.
2. **Implementation** in `src/tyrex_pm/runtime/layer_a_context.py`: use **`instrument_id_for_outcome_token`** (`state_readers.py`) + **`Portfolio`** / **`Cache`** net position for follower wallet — **same** venue semantics as risk readers where possible.
3. **`build_guru_trading_node`**: construct context, inject via **`CopyStrategy.set_layer_a_context(...)`** (or constructor arg through **`CopyStrategyConfig`** if Nautilus pattern allows).

---

## 8. Edge cases

| Case | Behavior |
|------|----------|
| Guru SELL size << follower position | **Expected** — full exit closes follower |
| Risk clips qty below full | **Allowed** — risk wins |
| Dynamic instrument not in cache at exit | Context returns **`None`** → **UNRESOLVED** deny |
| Short / negative net | v1: qty `<= 0` → **NO_POSITION** deny |

---

## 9. Logging

- **Deny:** `event=copy_skip component=copy_strategy correlation_id=... reason_code=LAYER_A_EXIT_FULL_DENIED_* detail=...`
- **Accept `full_exit`:** INFO includes `exit_qty_mode=full_position`, `follower_position_qty`, `guru_size_raw` (informational).

---

## 10. Reporting

- **`layer_a_filter`:** `filter_name=exit_interpretation`, `branch=exit`, `accept`, `reason_code`, `metadata` with `exit_qty_mode`, `follower_position_qty` when accept.
- **`strategy_decision`:** final skip/accept for the signal.

---

## 11. Implementation checklist

1. [ ] `ExitFilterSettings` dataclass + `load_strategy_settings` parse under `filters.exit_filter`.
2. [ ] `ReasonCode` entries (§6).
3. [ ] `ExitInterpretationFilter.evaluate(sig, branch, ctx)` → `LayerAOutcome`.
4. [ ] `LayerAContext` + `runtime/layer_a_context.py` + unit tests with mocked portfolio.
5. [ ] `guru_compose.py` inject context for live (and shadow: context returns **`None`** or **0** — **still** fail closed for `full_exit` if enabled; shadow operators should use `mirror_guru` or accept denies).
6. [ ] `CopyStrategy` exit branch: honor **`exit_qty_mode`** in metadata.
7. [ ] Facts emitter: `layer_a_filter` per step.
8. [ ] Integration test: mock context returns 100 → intent qty 100; `None` → deny, no submit.

---

## 12. Unit / integration tests (explicit)

| # | Test | Expect |
|---|------|--------|
| U1 | `enabled: false` | Pass interpretation with `mirror_guru`; sizing uses `SizingPolicy` |
| U2 | `full_exit`, context returns 50.0 | `accept`, qty 50 |
| U3 | `full_exit`, context `None` | deny `UNRESOLVED`, no `OrderIntent` |
| U4 | `full_exit`, context `0` | deny `NO_POSITION` |
| U5 | `full_exit`, context raises | deny `UNREADABLE` |
| U6 | Loader unknown `exit_method` | `ValueError` |
| I1 | End-to-end SELL + mock context | facts + skip/accept consistent |

---

## 13. Future extensions

- Partial exits, TWAP — only if explicitly added per **`Docs/Architecture.md`**.
