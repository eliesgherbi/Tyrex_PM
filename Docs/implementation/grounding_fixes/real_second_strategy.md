# G8 — A real second strategy (not the dummy)

**Status:** Implemented (Option A). `z_gap_tiny_live.yaml` may include `protection:`; q-edge is still a guide contract only.

**Phase:** Grounding fixes  
**Depends on:** G1 (clean finish). Optional protection on z_gap can ship without G4–G7.  
**q-edge** depends on G7.

## Goal

Stop spending the next month only on **ask70** (buy anything at 70¢ to test seatbelts). Put the seatbelt on a **real idea**, or build a third idea — without forking the host.

## Two options (pick one)

### Option A — Preferred first: z_gap + optional protection

z_gap already has thesis / rich / time exits. Protection is **orthogonal** (`Docs/latest/protection.md`: z_gap does not require a `protection:` block).

Work:

1. Allow `protection:` on `config/runs/z_gap_tiny_live.yaml` (optional).
2. Same arm-on-fill / bid-mark / precedence as ask70 (already in `TradingRuntime`).
3. Do **not** put SL/TP math inside `strategies/z_gap/`.
4. Tiny-live: z_gap entry (PTB + model) + e.g. reactive SL. After G1, leftover must not MANUAL.

This proves the overlay is not ask70-only. No new connectors.

### Option B — Later: q-edge as a real plugin

`Q_EDGE_INPUT_CONTRACT` is a **guide only** (`facts/mappings.py`). It is not in `strategies/registry.py`. Starting it today fails on G7 adapters.

Work **after G7**:

1. Package `strategies/q_edge/` — plugin, schema, driver, reasons.  
2. Register in `ensure_strategies_registered()`.  
3. `run_config` stays kind-via-registry (no new `if strategy_kind`).  
4. Emit `EnterIntent` TAKER and/or MAKER (G6 if you want maker).  
5. YAML under `config/runs/`.  
6. Tests: eligibility, intents, unknown kind rejected.

Do **not** implement q-edge economics in this grounding phase unless Option A is done and G7 is PASS. The contract is the scalability test; the model is a separate research task.

## Current code (reuse)

- `StrategyPlugin` + `InputContract` + `EvaluationScheduler`
- `IntentLike` (add Cancel only via G4)
- `Docs/latest/extending.md` checklist
- ask70 stays as the **harness**; do not delete it

## Out of scope

- Rewriting `TradingRuntime` per strategy
- Starting Binance “for all strategies”
- Hold-to-resolution redeem automation
- Raising the $5 cap
- Changing z_gap theta/z/tau to “make q-edge”

## Acceptance (Option A)

1. z_gap YAML with `protection:` loads; without it still loads.  
2. ask70 still requires protection.  
3. Unit: z_gap HOLD + overlay can emit `PROTECTION_*` without z_gap emitting EXIT.  
4. Optional operator tiny-live after G1.

## Acceptance (Option B)

1. `kind: q_edge` runs only if G7 adapters in the contract exist.  
2. Missing L2 still fail closed.  
3. Host has no new `strategy_kind ==` branches.
