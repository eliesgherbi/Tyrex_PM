# G3 — Stop-loss live proof

**Status:** Implemented (operator evidence). ask70 tiny-live now has `trailing.enabled: false`, SL 0.55, resting GTD TP 0.85.

**Phase:** Grounding fixes  
**Depends on:** G1 (so a fired SL can finish without MANUAL dust)  
**Depends on (soft):** G2 if entries keep dying on 38 s sign  
**Code change:** none required unless a live SL run finds a new defect

## Goal

See **one** real fill where the held bid falls to the stop (0.55 in the current ask70 YAML) and the overlay sells, the same way we already saw take-profit and trailing.

This is **operator evidence**, not a feature.

## What is already implemented

- `ProtectionSpec.stop_loss.style` = `stop_market_fak` only.
- Trigger: `mark <= stop_loss` **before** trailing and TP (`src/tyrex_pm/protection/triggers.py`).
- Runtime arms after confirmed fill; book ticks call `on_public_fact` even if ask70 is on HOLD.
- Emits `FlattenIntent` / `ExitIntent` with `PROTECTION_SL`.
- Same FAK sell spine as TP.

Live 2026-08-24: trail fired (`…090845Z`); TP fired at bid 0.86 (`…100203Z`). **No run printed `PROTECTION_SL`.** Price went the wrong way for a stop.

## Venue facts

- SL is not a venue “stop order.” Polymarket has GTC/GTD/FOK/FAK only. We **watch the bid** and send a FAK sell.
- On `itode` up/down markets, the FAK may sit **250 ms** in a delay window before match. During that window it cannot be cancelled. For a stop that is acceptable (we want the sell).
- After G1, leftover below `min_order_size` must not turn a good SL into MANUAL.

## Operator procedure

1. Ship G1 first. Do not treat another MANUAL-on-dust SL as “SL broken.”
2. Keep `config/runs/ask70_protection_tiny_live.yaml`:
   - `trailing.enabled: false` (so SL is not stolen by a 5¢ trail)
   - `stop_loss.absolute: "0.55"`
   - `take_profit.absolute: "0.85"` (if TP hits first, that run is **not** an SL proof; log and retry)
3. `--live`, $5 cap, same allowances as other tiny-lives.
4. Wait for a fill, then a **down** print to ≤ 0.55 on the **held** token bid.
5. Preserve `run_summary.json`. Required fields:
   - `PROTECTION_ARMED` with `stop_loss_trigger`
   - `PROTECTION_TRIGGERED` `reason_code=PROTECTION_SL`, `layer=stop_loss`
   - at least one EXIT POST
   - terminal after G1: `COMPLETED_FLAT` or `COMPLETED_FLAT_WITH_DUST` (not `exit_retry_budget_exhausted` for crumbs)

## If SL does not fire when bid ≤ 0.55

Then it is a **bug**, investigate before more product work:

- Armed? (`PROTECTION_ARMED` after fill)
- Mark source is held **bid** (`protection.mark: bid`)
- Books DESYNC → trigger skipped (`book_desync` / missing bid) — same as ask70 `SKIP_NO_BOOK`
- `exit_requested_reason` already set (another plane won)
- Precedence: kill / mandatory flatten before SL

Fix in `protection/` + tests; do not encode SL inside ask70.

## Out of scope

- Resting stop orders (venue has none)
- Changing 0.55 without an operator YAML edit
- Claiming SL is live-proven from unit tests alone

## Acceptance

One operator report with `PROTECTION_SL` + exit POST + G1-legal terminal. Checklist stays in this file; do not invent an admission JSON ceremony.
