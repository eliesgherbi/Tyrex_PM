# R8 — R7 closure and framework acceptance

**Verdict: R8 PASS — framework validation complete; ready for Z-Gap design**  
**Live execution this phase:** none.  
**Successful third live (prior):** [`r7_successful_live_acceptance.md`](r7_successful_live_acceptance.md).  
**Tests:** **373** passed (includes R8 terminal, floor-formula, architecture gates).

---

## 1. Successful-live reconciliation

| Check | Result |
|-------|--------|
| Run | `55fd9a76-743b-4fb8-835d-adcdbf0f517a` |
| BUY / SELL | both CONFIRMED (`ac2c4015-…` / `249274ae-…`) |
| Exit fingerprint | `0c830ec2fbf3f0a6` @ bid `0.50` |
| Entry limit reused | **false** |
| Residual | `0.000587` |
| Inventory terminal (R8) | **`FLAT_WITH_DUST`** |
| Lifecycle completed | **yes** |
| Mutations | 2 order submits; no on-chain cleanup |
| Ack set | exactly 4; untouched |
| Dust records | 3 distinct; cleanup `NONE` |
| Open orders | 0 |
| Read-only recon | `scripts/r8_readonly_recon.py` — all checks true |

### Realized P&L / fees

- BUY notional `4.83` @ `0.51`; SELL proceeds `4.735` @ `0.50`
- Estimated entry fee bound `0.16567`; venue `fee_rate_bps=0`
- Approx fee-inclusive P&L ≈ **−0.26** (safety evidence only)

---

## 2. Terminal semantics

| Conditional balance | Inventory terminal |
|---------------------|--------------------|
| exactly `0` | `FLAT` |
| `0 < bal < 0.01` | `FLAT_WITH_DUST` |
| `bal ≥ 0.01` after incomplete exit | `RESIDUAL_EXPOSURE` / `MANUAL_INTERVENTION` |

Separate: `lifecycle_completed` (execution path finished) vs `inventory_terminal` (flatness class).  
Do not use `FLAT` as shorthand for “no tradable exposure.”

Runtime: `TerminalOutcome.FLAT_WITH_DUST` in `r7b_live_once.py`.

---

## 3. Exit-floor formulas

Source of truth: `tyrex_pm.runtime.r7_lifecycle_policy` + `lifecycle_exit_plan.plan_lifecycle_fak_sell`.

### Shared steps (NORMAL and EMERGENCY)

```text
1. Require fresh book: age_ms ≤ BOOK_FRESHNESS_MAX_AGE_MS (2000)
2. Require bids present; optional spread ≤ MAX_EXIT_BOOK_SPREAD (0.20)
3. Walk bids descending for qty; worst = last consumed bid level
4. Require full depth if REQUIRE_FULL_BID_DEPTH
5. limit = tick_floor(worst)
6. Require limit ≥ absolute floor (0.01 for both urgencies)
7. Refuse if limit would reuse entry BUY ceiling above best bid
8. Submit marketable FAK SELL at limit (min acceptable price)
```

### NORMAL-only

```text
9. Require (best_bid − worst) ≤ MAX_EXIT_SLIPPAGE_FROM_TOUCH (0.05)
```

### EMERGENCY

- Activation: runtime escalates urgency after normal refusal / deadline pressure (bounded attempts).
- **Does not** apply step 9 (touch-slippage cap skipped).
- Absolute floor still `0.01`.
- Still requires fresh book, depth walk, fingerprint, qty ownership, tick rounding.
- Worst accepted price: **0.01** (emergency absolute floor).
- Worst dollar loss for a $5 fee-inclusive entry: up to ~**$5** of committed collateral if inventory is exited at 0.01 after an adverse fill (binary near 1.0 entry). Near mid-book ($5 @ ~0.51): emergency @ 0.01 ⇒ proceeds ≈ `qty×0.01` (~$0.09 for ~9.47 sh) → loss ≈ entry notional + fee − proceeds.

### Legacy REJECT vs active floors

| Legacy (REJECT) | Active R7E/R8 |
|-----------------|---------------|
| Emergency unwind `bid` / hardcoded `0.01` without depth | Depth-walk worst bid + absolute floor |
| No freshness / fingerprint gate | 2000 ms + fingerprint per attempt |
| No confirmed-qty ownership | `min(confirmed, balance, remaining)` |
| Strategy/touch seed as final price | Planner-owned price only |

**Conclusion:** identical absolute floors (`0.01`) do **not** make the active path equivalent to the rejected legacy unwind. Difference is proven by formula + `tests/test_r8_flat_with_dust_terminal.py` (NORMAL refuses deep slip; EMERGENCY allows depth-aware price ≥ 0.01).

**No economic risk-limit change in R8.** If a higher emergency floor is desired later, operator alternatives (stop for decision):

1. Keep `0.01` / `0.01` (current; venue min tick).
2. Raise emergency floor only (e.g. `0.05`) — reduces unwind flexibility.
3. Raise both floors — may increase `REFUSE_FLOOR` / manual intervention rate.

---

## 4. R7 acceptance matrix

| Objective | Evidence | Status |
|-----------|----------|--------|
| Guarded Polymarket mutation path | Operator `--execute-live` only; dry default; dirty worktree blocked | **PASS** |
| One bounded lifecycle | Single entry consumed; second entry denied | **PASS** |
| $5 fee-inclusive BUY cap | `max_collateral=4.99567` on success run | **PASS** |
| CONFIRMED settlement before inventory | MATCHED→MINED→CONFIRMED; sell after CONFIRMED | **PASS** (live1 exposed race) |
| Fresh side-correct exit planning | Fingerprint `0c830ec2…`; SELL@0.50; BUY limit unused | **PASS** (live2 exposed bug) |
| Partial / no-match reconciliation | FAK retry + residual registry; live2 no-match handled | **PASS** |
| Portfolio / residual persistence | Durable ack + multi-record residuals | **PASS** |
| Manual-intervention behavior | Live1/live2 required UI; live3 did not | **PASS** |
| Restart / external-action recovery | `FLAT_EXTERNAL_ACTION` path + registry | **PASS** |
| Account-wide safety gates | Ack=4; unexpected fifth not auto-acked; dust not targeted | **PASS** |
| Facts and reporting | report + facts + sha256 | **PASS** |

### Three live tests

| # | Run | Lesson | Fix |
|---|-----|--------|-----|
| 1 | `d632b631-…` | Settlement race | R7C CONFIRMED-only |
| 2 | `76e8470a-…` | Wrong SELL price | R7E bid-side planner |
| 3 | `55fd9a76-…` | Auto BUY+SELL success | R7F/R8 closure |

**Safety evidence:** yes (guards, settlement, side-correct exit, gates).  
**Profitability evidence:** no (≈ −$0.26 on success run; ReferenceMomentum is validation-only).

---

## 5. Framework module / contract matrix

Dependency flow verified:

```text
Adapters → Events → State → Indicators → Signals → Strategy
  → Intents → Risk → Execution plan → OMS → Execution events
  → Orders/Fills/Portfolio → Facts
```

Strategies must not own venue API, book reconstruction, risk auth, execution pricing, OMS state, portfolio truth, or persistence.

| Module | Responsibility | Authoritative state | Inputs | Outputs / events | Dependencies | Implemented | Validation | Limitations |
|--------|----------------|---------------------|--------|------------------|--------------|-------------|------------|-------------|
| Core contracts (`core/*`) | IDs, intents, commands, modes, snapshots | typed IDs / intent records | constructors | immutable value objects | stdlib | yes | unit tests | not a full domain DSL |
| Event dispatcher | in-process fan-out | subscription map | adapter events | handler calls | core | yes | observe/shadow hosts | no durable bus |
| Clocks / identifiers | time + correlation | clock provider | now | timestamps / UUIDs | core | yes | settlement fake clock | wall clock skew external |
| Market / reference adapters | Polymarket + Binance normalize | none (emit only) | WS/REST/fixtures | book/ref events | adapters | yes | fixture + live preflight | Binance ref-only |
| Instrument registry | binary YES/NO map | instrument records | discovery | InstrumentId | domain | yes | market tests | family-limited |
| Book / reference state | stores + freshness | BookStore / ref store | events | DecisionSnapshot | market_data | yes | snapshot tests | recovery edge cases |
| Indicators | reusable transforms | indicator buffers | snapshots | indicator values | market_data | yes | unit | sparse set |
| Signals | directional packaging | none | indicators | signal structs | signals | yes | momentum path | not strategy edge |
| Strategy interface | decide enter/exit need | strategy-private only | context | intents + evidence | strategies | yes | ReferenceMomentum | no Z-Gap |
| Intents | typed economic requests | intent ids | strategy | Enter/ExitIntent | core | yes | risk tests | |
| Risk | authorize / deny | dedup registry | intent+ctx | RiskDecision | risk | yes | LIVE deny default; R7 gates | LIVE_TINY host separate from Risk LIVE deny |
| Execution planning | size/price/order type | plan artifacts | approved intent + book | sized plan / exit plan | planning + lifecycle_exit_plan | yes | R7E/R7F/R8 | FAK fill not guaranteed |
| OMS protocol | submit/cancel/stop | local OrderId only | commands | OrderId | execution.protocol | yes | protocol tests | |
| ShadowOMS | deterministic fills | shadow books | commands | exec events | shadow_oms | yes | shadow suite | not venue-true |
| Live Polymarket OMS | venue submit path | transport-backed | commands | exec events | polymarket/* | yes | R6–R7 live (operator) | L2 creds required |
| Order store | local order truth | OrderStore | exec events | order views | execution | yes | unit | |
| Fill ledger | fill truth | FillLedger | exec events | fills | execution | yes | unit | |
| Portfolio | position truth | Portfolio | fills | positions | portfolio | yes | unit | dust vs venue recon |
| Trade lifecycle | phase machine | TradeLifecycle | notes | phase | lifecycle | yes | R7 ladder | |
| Persistence / recovery | snapshots / durable R7 state | `var/state`, config seals | runtime | reloadable state | persistence + r7_* | yes | ack/residual tests | `var/` gitignored |
| Reporting / facts | evidence JSONL | facts files | runtime | facts/report | reporting | yes | sha256 in reports | disposable under reporting/ |
| Runtime composition | hosts / CLI | mode | config | run result | runtime | yes | CLI + hosts | |
| OBSERVE | signals only | — | feeds | facts | ObserveHost | yes | observe tests | no OMS |
| SHADOW | paper OMS | shadow | feeds | facts | ShadowHost | yes | shadow tests | |
| LIVE_TINY | guarded one-shot | R7 session | operator flag | report | r7b_live_once | yes | 3 live tests | not general live loop |

### ReferenceMomentumStrategy assessment

**Proves:** end-to-end framework integration (feeds → intent → risk → plan → OMS → settlement → exit → facts) under R7 envelope.  
**Does not prove:** profitability, calibration quality, or Z-Gap readiness. Z-Gap design begins only after R8 PASS.

---

## 6. Account-wide inventory (R8 recon)

| Item | State |
|------|-------|
| Ack positions | 4 sealed; all redeemable resolved; none targeted |
| Dust | 3× `0.000587` `FLAT_WITH_DUST`; distinct tokens |
| Other nonzero Data API | none |
| Open orders | 0 |
| Unexpected fifth | would remain visible; not auto-acked; blocks entry |
| Auto redeem / on-chain cleanup | **absent** (`cleanup_policy=NONE`) |

---

## 7. Documentation audit

| Topic | Disposition |
|-------|-------------|
| Pending third live | **Removed** — runbook CLOSED; success doc owns evidence |
| Verbatim / session auth | Historical / superseded (`r7a2_*`) |
| NautilusTrader adoption | Explicitly **not** a dependency (objective/architecture) |
| `old/` paths | Reference-only; import firewall tests |
| Obsolete lifecycle states | Settlement ladder documented; MATCHED≠CONFIRMED |
| `FLAT` when dust exists | Corrected to `FLAT_WITH_DUST` |
| R7 not yet completed | Superseded by this R8 closure |

---

## 8. Remaining risks / debt

- FAK exit still race-prone vs book move (floor-protected, not fill-guaranteed).
- Venue fee amount not always present (`fee_rate_bps=0`); P&L uses estimates.
- General LIVE loop beyond one-shot not productized.
- RiskEngine still denies generic `LIVE_TINY` outside R7 CLI path (intentional separation).
- Three dust leftovers remain until optional future cleanup phase.
- Historical success report on disk still shows pre-R8 `terminal=FLAT` string (annotated; runtime fixed).

---

## Objective → architecture → functionality trace

| Objective (00) | Architecture (02) | Functionality proven |
|----------------|-------------------|----------------------|
| Fail-closed small capital | LIVE_TINY + $5 cap + ack/residual gates | 3 live tests under $5 |
| Shared stack across strategies | Strategy emits intents only | ReferenceMomentum exercises stack |
| No NT / no old imports | Firewall + docs | tests green |
| Z-Gap later | Out of R8 scope | not implemented |
