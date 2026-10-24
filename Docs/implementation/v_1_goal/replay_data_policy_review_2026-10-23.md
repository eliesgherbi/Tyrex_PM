# Replay data policies: from raw recordings to a qualified result

Date: 23 October 2026. Status: analysis only. Nothing here is implemented or authorises a collector, a paper session or an order.

## 1. Why this review

The first replay run from the screen produced 15 windows, 9 entry attempts, 1 fill and **zero qualified trades**. Across all saved jobs, 0 of 92 windows that tried to trade are qualified; the 32 qualified windows are exactly the windows where nothing was traded. With the current rules, no replay on the data we have can ever produce a qualified trade or a qualified P&L.

This review walks the chain from the raw recording to the result, names each rule that removes or downgrades a window, checks each rule against what the data actually contains, and proposes realistic policies. Numbers are measured on the four warehouses (209 fully recorded windows on four days) unless marked *inferred*.

The conclusion in one line: **most of the restriction comes from two data-handling bugs and one reconstruction artifact, not from bad data. Fixing those is not a relaxation. After that, a small number of explicit, named assumptions make the data usable without hiding anything.**

## 2. The chain and its language

```
Raw capture → Normalized datasets → Reconstructed books → Quality report (coverage)
   → Policy filter → Window set → Replay window load (+ warm-up)
   → Strategy host gates → Strategy decision → Simulated execution → Qualification → Result
```

| Term | Meaning in this project |
|---|---|
| Recording (session) | One capture from start to clean stop. We have four: 1 Oct (0.75 h + 0.09 h), 6 Oct (5 h), 15 Oct (6 h), 19 Oct (6 h). |
| Window | One 5-minute BTC Up/Down market. 209 windows fall fully inside a recording. |
| Raw capture | Exactly what the venues sent (WebSocket frames, REST payloads), sealed with checksums. |
| Normalized dataset | One table per kind of fact. Every row has `source_time` (venue time) and `received_at` (our clock). Replay orders by `received_at`. |
| Book snapshot / update | Full-depth CLOB book (≈100 levels) and incremental `price_change` frames. 400–5,000 snapshots and 78k–604k updates per window. |
| Reconstructed book | The book rebuilt from snapshots + updates. It is *valid* or *invalid*; once invalid it waits for the next snapshot. |
| Epoch | A stretch of time during which one reconstructed book stays valid. |
| Coverage / usable ratio | Valid time ÷ required time, per input (Up book, Down book, reference). |
| Reference | Chainlink BTC/USD 60 s TWAP from Polymarket's live feed. One tick per second, ~1–1.5 s delivery lag. This is what settles the market. |
| Warm-up | Reference history before the window opens, so the model can estimate volatility. |
| K (price to beat) | The reference value at the exact window start. Up wins if the closing reference is at or above K. |
| K quality | `CONFIRMED_CANONICAL` (attested), `PROVISIONAL` (exact tick, not attested), `INFERRED`, `LATE`, `MISMATCHED`, `MISSING`. |
| Outcome label | Who won. Comes from the provider's resolution payouts (`[1,0]` or `[0,1]`). |
| Policy | Named rule deciding which windows a set may contain and what assumptions travel with them. |
| Window set | A frozen list of windows + recordings + policy + file pins. |
| Host gate | Checks the trading host makes before it asks the strategy (session open, K sealed, reference fresh…). A failure is `HOST_SKIPPED`. |
| Strategy gate | Checks inside the strategy (entry band, K quality accepted, model ready, edge, depth). |
| Execution scenario | Assumptions for simulated trading: delays, depth participation, price penalty, fees (`friction`, `favorable`). |
| Qualification | The engine's verdict that it could compute this window's trades and P&L from known inputs. Any one *limitation* makes the window unqualified. |

## 3. What the data actually contains

| Input | What we have | Evidence |
|---|---|---|
| Books | Full depth, both legs, continuous WebSocket stream plus ~12 REST audits per window. | Measured, all warehouses |
| Book losses | 15.1% of book time is invalid. **89% of that is WebSocket disconnects** (median gap 18.9 s, p90 30 s). 0.8% is ~9,900 "crossed book" blips of 3–17 ms. | Measured over 214 windows |
| One-sided books | In the last ~60 s of 18 of 19 sampled windows, the winning leg has no asks and the losing leg no bids (price at 0.99 / 0.999). That is the market, not missing data. The engine correctly treats these books as valid. | Measured, 20-window sample |
| Tick size | Known before open for 99% of markets (0.01). In ~30% of windows the venue changes it to **0.001** a median ~35 s before close. | Measured |
| Minimum order size | Known before open for 99% of markets: **5 shares** in 100% of 52,903 rows. | Measured |
| Fees | Known before open for 99% of markets: taker-only, rate 0.07, exponent 1. Identical to the `friction` scenario's assumed fee. | Measured |
| Reference | Chainlink TWAP, 1 s spacing, no interior gaps inside recordings. Only the first ~4 windows of each recording lack the 15-minute warm-up. | Measured |
| K | The exact tick at window start is present for 207 of 209 windows. When the provider later publishes `priceToBeat` (75 windows, 3–165 s after close), it equals our K in **75 of 75**. | Measured |
| Outcome | Provider payouts available for 207 of 209 windows (~75 s after close). Winner derived from reference vs K agrees with the payout in **203 of 203**. | Measured |

Read as a quant, this is good data. The weaknesses are the disconnects (real, about 1 in 2 windows has one) and the fact that nobody "attested" K or the outcome. Every other input is present and consistent.

## 4. Rule-by-rule review

Verdicts: **Bug** (the rule misreads the data; fixing it is not a relaxation), **Artifact** (the rule reacts to our own processing, not the market), **Redefine** (the rule protects something real but measures the wrong thing), **Keep**.

### 4.1 Set creation (Data Library)

| # | Rule today | What it protects | What the data shows | Verdict |
|---|---|---|---|---|
| S1 | **Complete / strict**: every input covers 100% of its interval and the "semantic quality" verdict is READY. | Nothing may be missing. | **0 of 1,196 windows** pass. No book reaches 1.0 because of millisecond crossed-book blips. Semantic READY needs a `provider_policy.json` that exists in no warehouse. `provider_confirmed` is hard-coded impossible. | **Redefine.** In this form it cannot be satisfied by any recording. |
| S2 | **Exploratory 95%**: each book ≥ 95% valid over the 300 s window. | Enough book to simulate decisions. | Admits 78–82 of 214. Losses are disconnects. The rule ignores **where** the gap is and **how long** the longest gap is: a 15 s hole exactly in the entry period passes, while a window with 20 s missing at minute 1, long before any entry is allowed, fails. | **Redefine** (gap-aware, see §5). |
| S3 | **Reference warm-up**: Chainlink feed covering 15 min before open, at 95%. | Volatility estimate ready. | Costs the first ~4 windows of every recording and nothing else. The strategy already waits on its own readiness signal (`MODEL_NOT_READY`, only 137 decisions in all jobs). The "warm-up" check repeats the reference check. | **Redefine**: require what the strategy needs, let the strategy's own readiness gate decide. |
| S4 | **Outcome**: provider-reported payout required; "provider confirmed" never possible. | We score with the true winner. | Payout present for 207/209 and agrees with the reference-derived winner 203/203. | **Keep provider payout** as the scoring label, cross-checked against the reference. Retire "provider confirmed" until something can produce it. |
| S5 | **Reserved evaluation**: markets in the notebook-era holdout file are excluded at set creation (39 windows). | Protect a holdout. | The agreed design says a set has no partition. Only Tune uses a holdout, chosen by the user. | **Bug** against the agreed design: remove from set creation. |

### 4.2 Strategy and host gates (inside the replay)

| # | Rule | What the data shows | Verdict |
|---|---|---|---|
| G1 | `HOST_SKIPPED exact_candidate_absent` | 23k of these are the 5-minute pre-open warm-up and the 1–32 s until K is sealed. Not a data fault, and outside the entry band. The decision recorder labels it a data fault. | **Keep the behaviour; relabel** as "before K is sealed". |
| G2 | K quality must be in the accepted list; schema default accepts only `CONFIRMED_CANONICAL`. | Replay never attests, so K is `PROVISIONAL` on 100% of rows. Default settings therefore produce `K_NOT_ACCEPTED` everywhere. Yet the provisional K is the exact start tick and matched the provider's value 75/75. | **Redefine**: give a replay K that comes from the exact start tick its own quality (§5.3) and accept it as a named research assumption. Mark `CONFIRMED_BY_PROVIDER` after the fact when `priceToBeat` matches. |
| G3 | `require_verified_settlement_spec` | Off in every job; settlement provenance is "ASSUMED". | **Keep off for research**, recorded in the result. |

### 4.3 Simulated execution and qualification (engine)

| # | Limitation | Trigger | What the data shows | Verdict |
|---|---|---|---|---|
| E1 | `order_constraints_unqualified` | Tick or minimum size unknown when an order arrives. | Fires on **every** order. Normalization writes the minimum-size unit as `UNKNOWN` (`registry.py:518`) and the engine only accepts unit `"shares"` (`engine.py:348`), although the value is 5 shares in every row. Re-checked against tick 0.01 / minimum 5: **0 of 92** attempts would have been rejected. | **Bug.** Record the unit as shares (it is Polymarket's documented unit for `orderMinSize`). The flag will then fire only for the ~2 markets per recording first seen after open, as intended. |
| E2 | Tick precedence | `tick_size_change` (0.01 → 0.001) can be overwritten by an older metadata snapshot still saying 0.01. | Happens in 47 of 65 windows with a tick change. Orders near close at 0.999 would be rejected as off-tick. | **Bug.** The latest venue event wins over stale metadata. |
| E3 | `gap_with_exposure` | A book turns invalid while we hold a position or have a live order. | 13 of 13 filled windows. Of 305 interruptions while holding: **302 were locked/crossed blips lasting 0–45 ms** (median 0), all healed by the next snapshot. 2 were real disconnects (1.8 s, 5.4 s), 1 an unsubscribe (71 ms). **None** was the one-sided book at 0.99. | **Artifact + Redefine.** (a) The blips come from the "legacy" reconstruction ordering updates against snapshots; fix the reconstruction or treat an invalid stretch shorter than a threshold as no gap. (b) A gap only matters if something needed the book during it (a decision, an exit, a mark). For a position held to resolution, a 5 s gap with no action changes nothing. |
| E4 | `invalid_book_at_action` | Order arrives on an invalid book; order rejected. | Did not occur. | **Keep.** |
| E5 | `arrival_outside_recording` | Order arrives after the recorded data ends. | Did not occur. | **Keep.** |
| E6 | `recorded_fee_curve_unavailable`, `fractional_fee_exponent_unsupported` | Recorded fee curve missing or unsupported. | Fee schedule present for 99%, exponent 1. | **Keep**, and switch research to **recorded fees** (identical numbers today, stronger evidence). |

### 4.4 Not a qualification rule, but the reason nothing fills

79 of 92 entry attempts ended `unfilled_cancelled`. The strategy sets its limit at the best ask it sees (`limit_buffer_ticks: 0`). The `friction` scenario applies a 1-tick price penalty by **excluding every level whose price + 1 tick exceeds the limit** (`engine.py:207`). So the best-ask level can never fill; an order fills only if a cheaper ask appears during the 400 ms of simulated delay.

On the venue, a fill-and-kill buy at the best ask fills at the best ask, as long as that depth is still there when the order arrives. The delay is already simulated: the engine sees the book as it is 400 ms later. Adding a tick on top counts the same adverse move twice, and in this implementation it blocks the fill instead of making it more expensive.

This makes every result look like "the strategy never trades". It is the most important realism issue, ahead of qualification.

## 5. Proposed policies

### 5.1 Fix first (bugs and artifacts, no assumption added)

1. Minimum order size unit = shares (E1). Effect, inferred from the 92 saved attempts: all 79 non-filled windows lose their only limitation.
2. Tick precedence: venue `tick_size_change` beats older metadata (E2).
3. Remove the notebook holdout from set creation, bind and listing (S5).
4. Book reconstruction: eliminate locked/crossed blips caused by update/snapshot ordering. Test whether `venue_top_v1` does it. Same fix applies to coverage, where the blips make "complete" impossible.
5. Relabel pre-seal `HOST_SKIPPED` rows (G1).

Inferred effect on saved jobs: of 13 filled windows, 12 would become qualified (only 0xf6e1…, with real 1.8 s and 5.4 s disconnects while holding, would remain flagged under rule 5.3-E3 below).

### 5.2 Data policy for window sets: `replay_research_v2`

A set stays strategy-neutral. It guarantees data that a replay can honestly simulate, and records where data is missing so the engine and the reader can account for it.

| Input | Rule | Why this threshold |
|---|---|---|
| Books, both legs | Valid ≥ **90%** of the 5-minute window **and** longest single invalid gap ≤ **30 s**. | Disconnects are the real loss (median 19 s, p90 30 s). A 30 s cap bounds the worst blind spot; 90% keeps windows with one ordinary reconnect. |
| Books, final 60 s | No gap allowed while the window is still open in the last 60 s unless the book is one-sided at 0.99/0.01 (valid). | That is where settlement-value strategies enter and where tick changes happen. |
| Reference | Exact start tick present (K computable) and reference continuous over the window. Warm-up: whatever exists; the strategy's `MODEL_NOT_READY` decides. | Removes the "first four windows" loss without hiding anything. |
| K | Exact-start Chainlink tick. Quality `REPLAY_EXACT_START` (new, honest name for today's `PROVISIONAL`). Upgraded to `CONFIRMED_BY_PROVIDER` when a later `priceToBeat` matches. | 75/75 match where checkable. |
| Outcome | Provider payout, with reference-derived winner as cross-check. Disagreement → window excluded with reason `label_disagreement`. | 203/203 agreement today. |
| Metadata | Tick, minimum size and fee schedule known before open. | 99% coverage. |
| Partition | None. | Agreed design. |

Each set stores, per window: the valid ratio, longest gap, gap positions, and whether K was provider-confirmed. These numbers travel into results.

*To measure before adopting:* how many of the 214 windows pass these thresholds (expected well above today's 78, since most failures are one disconnect early in the window, but this is not yet measured).

### 5.3 Qualification v2 (per window, in the engine)

Three levels instead of one boolean, so the screen never mixes them:

| Level | Meaning |
|---|---|
| **Qualified** | All trades, marks and the settlement were computed from known inputs under the stated assumptions. |
| **Qualified with notes** | As above, with informational notes that did not affect any computed number (e.g. a 3 s gap while holding to resolution with no decision in it). |
| **Unqualified** | At least one computed number relied on missing or unknown inputs. |

Rules:

| Limitation | v2 rule |
|---|---|
| Order constraints | Unqualified only if tick or minimum size truly unknown at submission. |
| Invalid book at action | Unqualified (unchanged). |
| Gap with exposure | Ignore invalid stretches < **250 ms** (reconstruction blips). Longer gap: **unqualified** if any decision, order, exit or mark fell inside it; otherwise **note** (`held_through_gap`, with duration). |
| Fees | Recorded fee schedule; unqualified if missing. |
| K | Accepted qualities under `replay_research_v2`: `CONFIRMED_BY_PROVIDER`, `REPLAY_EXACT_START`. Anything else blocks entry (unchanged behaviour). |
| Outcome | Provider payout. Missing → P&L unknown, unqualified. |

Every result keeps its list of assumptions: "K from the exact start tick, not attested", "outcome provider-reported", "execution scenario friction v2". That is what makes it honest; the boolean alone is not.

### 5.4 Execution scenario: `friction_v2`

| Parameter | Today | Proposed | Reason |
|---|---|---|---|
| Delays (feed 100 ms, order 300 ms) | as today | keep | Realistic for our host; the book really moves during the delay. |
| Adverse price ticks | 1, applied as level exclusion | **0** in the base scenario | Delay already models adverse movement; the current implementation turns it into a non-fill. |
| Stress variant | — | `friction_stress`: adverse ticks 1 applied as *paying* one tick more, not excluding the level; participation 0.10 | Keeps a pessimistic check that still lets orders fill. |
| Participation | 0.25 of displayed depth | keep | Conservative and simple. |
| Fees | assumed 0.07 | **recorded** schedule | Same value, verifiable. |

### 5.5 Strict policy for promoting a candidate: `promotion_v1`

Used only when a result is meant to justify a candidate or paper review:

- Books ≥ 99% with no gap in the last 120 s.
- K `CONFIRMED_BY_PROVIDER` (available for ~35% of windows today).
- Outcome payout and reference cross-check agree.
- Qualification level **Qualified** (no notes).
- `friction_v2` and `friction_stress` both reported.

This replaces today's "complete", which no recording can meet.

## 6. What this changes for the user

| Today | With the proposals |
|---|---|
| 78 windows usable out of 214, none with a qualified trade. | More windows usable (to measure), and trades qualify when their inputs were genuinely known. |
| "Unknown" qualified P&L on every run. | A real qualified P&L, with its assumptions listed beside it. |
| 86% of entries never fill because of a double-counted penalty. | Fills follow the recorded book after realistic delay; a separate stress scenario stays pessimistic. |
| One strict mode that admits nothing. | A promotion policy that admits the best windows with provider-confirmed K. |

## 7. Order of work (when authorised)

1. The five fixes in §5.1, each with a regression test against a saved window.
2. Re-run the coverage measurement under `replay_research_v2` and show the funnel by reason.
3. Qualification v2 (three levels) and assumption list in results and on the dashboard.
4. `friction_v2` and `friction_stress`; rerun the 6 Oct set with both.
5. `promotion_v1` and its use in candidates.

Earlier results stay as they are, labelled with the policy and engine version they were produced under.

## 8. Open decisions for you

- Thresholds: 90% / 30 s for books, 250 ms for reconstruction blips, last-60 s protection. They are first proposals from the measured gap distribution.
- Whether `REPLAY_EXACT_START` K is acceptable for research results (proposed yes, labelled), or only for exploration.
- Whether the base execution scenario should drop the extra adverse tick (proposed yes), keeping a stress scenario beside it.
- Whether disconnect-affected windows are included with their gaps recorded (proposed, within the 30 s cap) or excluded.

## Evidence

Measurements and scripts from this review (scratch, not part of the repository): `agentA` (raw data), `agentB` (coverage and policies), `agentC` (engine and qualification) under the session scratchpad. Key code: `data_warehouse/quality/coverage.py`, `research/workflows/inventory.py`, `research/workflows/recipes.py`, `data_warehouse/normalize/registry.py:492-527`, `data_warehouse/reconstruction/books.py:150-292`, `research/replay/engine.py:95-365, 490`, `strategies/settlement_value/driver.py:744-779`, `domain/polymarket/ptb_capture.py:463-526`.

## 9. Decisions taken and implemented, 23 October 2026

Decided with the interactive review page (four decisions) and implemented the same day. Results produced earlier keep their own policy and scenario names.

| Decision | Chosen | Implemented in |
|---|---|---|
| Book coverage | `replay_research_v2`: both books valid ≥ 90% of the window, no hole > 30 s, none in the last 60 s, blips < 250 ms ignored, reference must cover the window itself, no fixed warm-up | `workflows/recipes.py` (`RESEARCH_V2`, `book_profile`), Data Library default |
| K | Accepted for research: K from the exact start tick (engine label `PROVISIONAL`), checked after the fact against the provider's `priceToBeat`; a mismatch makes the window unqualified (`k_differs_from_provider`) | `workflows/worker.py` (`k_value`), `workflows/results.py` (`k_check`) |
| Execution | `friction_v2` (no extra tick, recorded fees, 250 ms blip tolerance) is the default; `friction_stress_v1` (book one tick worse at arrival, 600 ms arrival, 10% depth) is its pessimistic companion | `config/research/scenarios/` |
| Connection drops | Kept when the window passes `replay_research_v2`; each hole stored with the set and the result | `book_profile` on every included row |

The stress scenario differs from the first proposal ("pay one tick more"). Paying a tick above the limit would fill orders a real limit order cannot fill. The stress scenario therefore keeps limit prices and assumes the book is one tick worse at arrival, with thinner depth and slower arrival.

Fixes shipped with it:

- Minimum order size: normalization writes unit `shares`; the engine reads earlier Polymarket rows with unit `UNKNOWN` as shares and writes `minimum_size_unit_interpreted` to the ledger.
- Tick precedence: a venue `tick_size_change` can no longer be undone by an older metadata snapshot.
- Notebook holdout: retired from set creation, `experiment.bind` and job start. `partition.py` keeps only an explicit caller-supplied reservation.
- Qualification v2 in the engine (`gap_tolerance_ms`): a book gap shorter than the tolerance is a blip; a longer gap with an order arriving inside it disqualifies; a longer gap held without any order is a note. Results carry `qualification` = `qualified`, `qualified_with_notes` or `unqualified`.
- Experiment identity includes execution, so two runs differing only by scenario both start.

Measured with the real `data_recipe.resolve` on the four warehouses: `replay_research_v2` keeps **104** windows (exploratory 95% now keeps 82; it kept 78 while the holdout applied). The 6 October recording alone gives 23 windows under v2 (15 before).

Note found while testing: in a 300 s window, a single hole over 30 s already loses more than 10%, so the 30 s cap never binds before the 90% rule. It stays in the policy for longer windows.

Tests: `tests/unit/research/replay/test_engine_qualification_v2.py` (8), `tests/unit/research/test_replay_results_contract.py` (now 13).
