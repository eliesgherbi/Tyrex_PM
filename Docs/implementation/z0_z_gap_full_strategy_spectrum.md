# Z0 — Z-Gap full strategy spectrum

**Status:** P0 frozen — accepted full-strategy specification  
**Date:** 2026-07-19 (P0 freeze 2026-07-20)  
**Companion evidence document:** [`z0_z_gap_design_audit.md`](z0_z_gap_design_audit.md) (legacy Phase A reconstruction)  
**Implementation plan:** [`z_gap_full_strategy_implementation_plan.md`](z_gap_full_strategy_implementation_plan.md)  
**Branch baseline:** `rest_project` @ `0a48dfc` / framework `fb9d0d8`

This document answers:

```text
What is the full Z-Gap strategy we would ideally want,
independently of what the old code happened to implement?
```

It does **not** freeze legacy Phase A as the whole strategy.  
It does **not** adopt Phase B/C labels as automatic truth.  
Framework mapping and milestones live in the implementation plan.

---

## 0. Accepted planning baseline and mandatory corrections (2026-07-19)

### 0.1 Frozen strategy identity (planning)

Z-Gap is a short-horizon fair-value mispricing strategy for Polymarket BTC Up/Down five-minute markets. It estimates settlement probabilities from resolution-aligned \(K\), reference \(S\), volatility \(\sigma\), and remaining time \(\tau\); compares fair probabilities to executable token prices after costs; buys the single leg with the best valid positive edge; continuously values the active position; and may monetize via market repricing or, when supported and economically preferable, holding to resolution. Strategy emits economic decisions and reasons; the framework owns authorization, quantity, plans, OMS, fill/inventory truth, settlement, retry, persistence, recovery, and terminal classification.

**Core position policy:** one entry lineage per window; one single-leg position; no scale-in/out; full exits only; no simultaneous opposite exposure; no same-window reversal or re-entry; fixed tiny notional initially; thresholds provisional until OBSERVE/SHADOW evidence.

**Strategy semantic actions** (full Z-Gap economics): prefer enter / hold / exit / flatten / wait / skip / blocked, plus resolution-hold **preference** when comparing SELL NOW · CONTINUE · HOLD TO RESOLUTION.

**Mapping to framework (see implementation plan §D.1):**

| Z-Gap semantic | F1 generic `StrategyDecision.action` | Intent |
|----------------|--------------------------------------|--------|
| Wait / skip / hold / blocked | `WAIT` / `SKIP` / `HOLD` / `BLOCKED` | none |
| Enter | `ENTER` | `EnterIntent` |
| Exit fully (rich / thesis / time / …) | `EXIT` + distinct `reason_code` | `ExitIntent` |
| Emergency / risk flatten | `FLATTEN` | `FlattenIntent` |
| Prefer hold-to-resolution | **Not an F1 action** | Explicit resolution request in **F5** only |

`STOP` may remain a reason/label; it is not required as a separate generic effect if it maps to `EXIT` + `ExitIntent`.

### 0.2 Correction A — Sell-side valuation signs (**CANONICAL**)

Define unit costs and proceeds with an explicit pricing convention:

\[
C_{\mathrm{entry,unit}} = ask_{\mathrm{entry}} + fee_{\mathrm{buy}} + slippage_{\mathrm{buy}}
\]

\[
V_{\mathrm{sell,unit}} = bid_{\mathrm{executable}} - fee_{\mathrm{sell}} - slippage_{\mathrm{sell}}
\]

**Convention:** if \(bid_{\mathrm{executable}}\) is already a depth-walk worst price that incorporates size slippage, set \(slippage_{\mathrm{sell}}=0\) in the formula (do not subtract twice). Record which convention was used in facts.

For held-leg probability \(p_{\mathrm{held}}\):

\[
\mathrm{remaining\_hold\_edge} = p_{\mathrm{held}} - V_{\mathrm{sell,unit}}
\]

\[
\mathrm{market\_richness} = V_{\mathrm{sell,unit}} - p_{\mathrm{held}}
\]

These are equivalent with opposite signs.

**REJECT** reusing the BUY-side expression \(p - bid - fee - slip\) as a sell-side richness formula: fees reduce net sell proceeds, so that expression has the wrong economic sign for comparing sell-now value with model value.

| Exit policy | Rule | Role |
|-------------|------|------|
| **Market-rich exit (canonical discretionary realization)** | \(V_{\mathrm{sell,unit}} - p_{\mathrm{held}} \ge \theta_{\mathrm{rich}}\) | Sell when net executable proceeds are sufficiently rich vs model |
| Remaining-edge realization (diagnostic / alternate thresholding) | \(p_{\mathrm{held}} - V_{\mathrm{sell,unit}} \le \theta_{\mathrm{remaining}}\) | Equivalent family; useful as diagnostic mirror |

**Recommendation:** canonicalize **market-rich exit**; keep remaining-hold-edge as the diagnostic dual and for entry/continuation language.

### 0.3 Correction B — Hold-to-resolution is not sticky suppression (**CANONICAL**)

Hold-to-resolution must **not** automatically suppress an economically superior sell.

Compare permitted actions: **SELL NOW** · **CONTINUE** · **HOLD TO RESOLUTION**.  
Operational readiness only controls which actions are *available*. A resolution-qualified position must still sell when \(V_{\mathrm{sell}}\) is sufficiently superior to \(V_{\mathrm{resolve,adj}}\).

Do **not** model hold-to-resolution as a sticky mode unless an explicit operational point of no return exists (e.g. redeem already submitted / irreversible settlement commit). Until that point, every tick re-compares values.

### 0.4 Correction C — Pure fair-value leg selection (**CANONICAL**)

Do **not** require the selected leg to agree with \(\mathrm{sign}(z)\).

A less probable outcome can still be strongly underpriced. Example: \(p_{UP}=0.70\), \(p_{DOWN}=0.30\), all-in UP cost \(0.69\), all-in DOWN cost \(0.14\) → DOWN has larger expected edge.

**Canonical selection:** choose the leg with the **largest valid positive economic edge**.  
Optional low-probability / confidence gates are separate configurable filters — not a direction-consistency rule.  
Simultaneous apparent edge on both legs is a quality/arbitrage diagnostic; the strategy remains single-leg.

### 0.5 Correction D — Round-trip entry economics (**CANONICAL**)

\[
E_{\mathrm{settlement}} = p_{\mathrm{selected}} - C_{\mathrm{entry,unit}}
\]

For mandatory pre-resolution monetization, conservative proxy:

\[
E_{\mathrm{repricing}} = p_{\mathrm{selected}} - C_{\mathrm{entry,unit}} - F_{\mathrm{exit}}
\]

where \(F_{\mathrm{exit}} = fee_{\mathrm{sell}} + slippage_{\mathrm{sell}} +\) any explicit exit-liquidity reserve.

\(E_{\mathrm{repricing}}\) assumes a future exit near model value; it is **not** a prediction of the future executable bid and must **not** be called realized edge.

Record separately: settlement edge; conservative repricing edge; actual realized round-trip P&L.

| Resolution-hold capability | Entry rule |
|----------------------------|------------|
| Unavailable | Require \(E_{\mathrm{repricing}} \ge \theta_{\mathrm{take}}\) (or stricter) |
| Available but not currently qualified | Same as unavailable for entry; may still record \(E_{\mathrm{settlement}}\) |
| Available and may qualify later | May admit on \(E_{\mathrm{settlement}}\) **only if** policy explicitly allows resolution-seeking entries; default: still require \(E_{\mathrm{repricing}}\) until hold is a real option for that position |

### 0.6 Correction E — Unknown inventory (**CANONICAL**)

```text
UNKNOWN
→ block new exposure
→ reconcile internal and venue evidence
→ resolve confirmed/sellable quantity
→ flatten only a known confirmed quantity
→ manual intervention if unresolved
```

Emergency flatten = urgent controlled flatten of **confirmed** exposure — never guessing inventory.

### 0.7 Correction F — Thesis invalidation via probability (**CANONICAL**)

\(z\) and \(p=\Phi(z)\) are monotonic representations of the same model state (e.g. \(z=-0.25 \Rightarrow p_{UP}\approx 0.401\)).

The strategy decision is a **held-leg probability/invalidation threshold + confirmation policy**. Prefer human-readable \(p_{\mathrm{held}}\) thresholds; retain \(z\) for diagnostics and mathematical parity with legacy goldens.

---

## Evidence / proposal labels

| Label | Meaning |
|-------|---------|
| **LEGACY IMPLEMENTED** | Present in executable legacy code/tests under `old/` |
| **LEGACY INTENDED** | Described in old documents; not implemented reliably |
| **ECONOMIC INFERENCE** | Logically implied by the hypothesis |
| **PROPOSED CANONICAL** | Recommended complete strategy behavior |
| **OPTIONAL EXTENSION** | Useful later; not required for core identity |
| **USER DECISION** | Material choice requiring review |
| **REJECT** | Should not be retained |

Legacy planning labels “Phase A/B/C” appear only as historical citations.

---

## 1. Economic hypothesis — critical evaluation

### 1.1 Restatement

```text
A short-horizon BTC fair-value model may identify temporary mispricing
between the estimated probability of a Polymarket UP/DOWN outcome
and the executable market price of its outcome tokens.
```

### 1.2 What is being predicted?

| Claim | Assessment |
|-------|------------|
| Final settlement outcome \(S_{\mathrm{settle}} \ge K\) | The \(\Phi(z)\) model is a **settlement-probability** estimator under a geometric Brownian / normal-log-distance approximation. **ECONOMIC INFERENCE** |
| Near-term tradable token value | Token mid/bid/ask can diverge from \(p\) due to inventory, fees, latency, and risk premia. The model does **not** directly estimate microstructure fair mid. **ECONOMIC INFERENCE** |
| Direction of the next BTC tick | Not the primary claim; \(z\) is a distance-to-strike scaled by vol and time. **ECONOMIC INFERENCE** |

**Strategy type:** primarily a **fair-value / probability mispricing** strategy, with **directional exposure** as the instrument of expression, and optional **latency/data-quality** sensitivity through freshness and basis gates. It is not a pure latency arb unless K/reference capture is the edge.

### 1.3 Why Polymarket might temporarily disagree

**ECONOMIC INFERENCE** (and consistent with **LEGACY INTENDED** framing):

- Slow or uneven incorporation of spot moves into binary books
- Fee/friction making small edges non-executable until they widen
- Inventory and adverse-selection premia in short-horizon binaries
- Disagreement or lag between trading reference (e.g. Binance) and settlement reference
- Noise in short-horizon \(\sigma\) estimates

### 1.4 How the opportunity becomes profitable

There are **two distinct monetization paths**:

| Path | Mechanism | Requires |
|------|-----------|----------|
| **A. Market repricing** | Token price moves toward model \(p\); sell before resolution | Liquid bid, exit friction model, continuous revaluation |
| **B. Hold to resolution** | Collect \(1\) or \(0\) payout vs entry cost | Settlement/redemption path, willingness to bear binary terminal risk |

**Critical distinction (**ECONOMIC INFERENCE**):**

```text
Underpriced vs expected final payout
≠
Attractive as a round-trip trade that must sell before resolution
```

Legacy **implemented** entry used:

\[
\mathrm{edge}_{\mathrm{entry}} = p - ask - \phi(ask) - slip_{\mathrm{entry}}
\]

That compares model settlement probability to **buy** friction. It does **not** subtract expected **exit** friction or compare against sell-now value. Using that alone while mandating pre-resolution exits systematically overstates opportunity. **ECONOMIC INFERENCE**; entry form is **LEGACY IMPLEMENTED**.

### 1.5 What would demonstrate no real edge

**ECONOMIC INFERENCE** / calibration goals (**LEGACY INTENDED** in Phase C docs):

- After costs, average sell-now or resolution PnL ≤ 0 across regimes
- Model \(p\) poorly calibrated (Brier / reliability)
- Apparent edges exist only when books are toxic or unfillable
- Edge vanishes under realistic exit friction assumptions
- Basis/settlement-source mismatch dominates model error

### 1.6 Model assumptions behind \(p=\Phi(z)\)

With \(z=\ln(S/K)/(\sigma\sqrt{\tau_{\mathrm{eff}}})\):

| Assumption | Implication if false |
|------------|----------------------|
| Log-distance ≈ normal under EWMA σ | Mis-sized \(p\) in jumps/regimes |
| \(S\) is the right trading reference | Wrong \(z\) if wrong spot |
| \(K\) matches settlement strike | Systematic bias; model invalid |
| Settlement uses a source aligned with \(K\) construction | “Fair” \(p\) ≠ venue payout probability |
| Continuous trading approximation to discrete binary | Near-expiry sensitivity explosion |

**Binance vs settlement-source basis:** if settlement follows a Chainlink (or other) print while trading uses Binance, basis is not a cosmetic filter — it is part of model validity. **ECONOMIC INFERENCE**. Legacy treated large basis as an entry block (**LEGACY IMPLEMENTED**).

### 1.7 Settlement probability vs tradable short-term value

| Quantity | Meaning |
|----------|---------|
| \(p\) | Model estimate of **P(UP wins at resolution)** given \(S,K,\sigma,\tau\) |
| Token ask/bid | **Tradable** prices including premia and friction |

A coherent full strategy must decide whether it is:

1. harvesting **repricing toward \(p\)** (round-trip),  
2. harvesting **settlement EV** (hold), or  
3. a **hybrid** that chooses per position.

This is a **strategy-identity** choice (**USER DECISION**), not a coding detail.

---

## 2. PTB as a strategy requirement (not a provider choice)

### 2.1 Economic requirement

```text
The strategy needs a frozen strike K that matches, or is demonstrably
aligned with, the value used to resolve the selected Polymarket market.
```

| Aspect | Requirement |
|--------|-------------|
| What \(K\) is | The binary’s settlement threshold for UP vs DOWN |
| Alignment | \(K\) used in \(\Phi(z)\) must match venue resolution rule |
| Capture time | Must be known (or safely provisional) before entry that depends on \(p(K)\) |
| Immutability | Once used for an entry decision, \(K\) must not silently change mid-position |
| Acceptable uncertainty | Bounded lag/error vs canonical resolution \(K\); beyond bound → model invalid |

### 2.2 K quality classes

| Class | Meaning | Trading implication (**PROPOSED CANONICAL**) |
|-------|---------|-----------------------------------------------|
| **Confirmed canonical K** | Proven aligned with market resolution definition | Full entry allowed |
| **Provisional K** | Best current estimate; not yet locked/attested | OBSERVE ok with quality flag; live/shadow entry **USER DECISION** (recommend: block live entry) |
| **Inferred K** | Derived indirectly (e.g. from metadata heuristics) | OBSERVE only unless attestation proves alignment |
| **Late K** | Arrived after acceptable boundary lag | Block entry; may still OBSERVE for diagnostics |
| **Mismatched K** | Competing sources disagree beyond tolerance | Block; model invalid for that window |

### 2.3 Implementation options for obtaining K (not strategy truth)

| Option | Role | Notes |
|--------|------|-------|
| Chainlink/RTDS first tick at/after window start | **LEGACY IMPLEMENTED** capture method | Common Polymarket BTC 5m practice; not sacred |
| Market metadata / Gamma fields | Possible source | Must verify it is the **resolution** K, not a display field |
| Sidecar/log boundary capture | **LEGACY IMPLEMENTED** fallback | Operator/debug path |
| Post-hoc attestation against known resolution K | Validation | Essential for trust; can gate enforce |
| Manual operator K | Emergency only | High operational risk |

**USER DECISION:** which sources are allowed for canonical K, and what attestation is required before non-observe trading.

**REJECT:** silently treating any convenient price as \(K\) without resolution alignment.

---

## 3. Full strategy timeline (one BTC 5m market)

Strategy-level process — not software modules.

| Stage | Strategy knows | Calculates | Allowed decisions | Missing info risk | Late data | Proceed / wait / block |
|-------|----------------|------------|-------------------|-------------------|-----------|------------------------|
| Market discovered | Window identity, tokens, open/close | Eligibility of market family | Accept/reject market | Wrong market family | Wait for metadata | Wait/block if incomplete |
| Resolution rule understood | How UP wins; what K means | Alignment checklist | Continue only if rule clear | Ambiguous resolution | Block trading | Block if unclear |
| K established | K class (canonical/provisional/…) | Lock policy | Freeze K for window | Unlocked/changing K | Wait for canonical; OBSERVE may use provisional | Block entry without acceptable K class |
| Model warmed | \(S\) history, σ readiness | σ, jump state | Wait until ready | Cold start | Wait | Wait (not force entry) |
| Opportunity evaluated | Books, fees, \(p\), frictions | Edges, valuations | SKIP / WAIT / ENTER intent | Stale books/fees | Recompute or skip | Skip if stale |
| Entry considered | Gates + economics | Max price, size | ENTER or no | Depth/fee unknown | Block | Block if incomplete |
| Order pending | Intent submitted | Fill uncertainty | Cancel/abort policy (**USER DECISION**) | Fill unknown | Wait for truth | No second entry while pending |
| Position active | Cost basis, qty, leg | Continuous valuations | HOLD / EXIT / REDUCE / HOLD-RESOLVE / FLATTEN | Model or book gaps | Degrade to safe exit policy | Prefer fail-closed |
| Continuous re-eval | Updated \(S,p,z\), bid, τ | \(V_{\mathrm{sell}}, V_{\mathrm{cont}}, V_{\mathrm{resolve}}\) | Same | — | — | — |
| Terminal | Flat / resolved / residual / unknown | Attribution | Stop window; no silent re-entry | Settlement lag | Wait for settlement truth | Block new risk if unknown |

Invalidation can occur at any stage: wrong K, broken basis, jump regime, kill switch, unknown inventory.

---

## 4. Full entry spectrum

### 4.1 Fair-value entry concepts

| Concept | Classification | Notes |
|---------|----------------|-------|
| \(p - ask - \phi(ask) - slip_{\mathrm{in}} \ge \theta_{\mathrm{take}}\) | **LEGACY IMPLEMENTED**; incomplete if round-trip | Settlement-EV vs buy price |
| Round-trip-aware entry: also reserve expected exit friction or require \(V_{\mathrm{cont}}\) edge | **ECONOMIC INFERENCE** → **PROPOSED CANONICAL** for pre-resolution mode | Prevents entering “edge” that cannot survive a sell |
| Entry only if \(V_{\mathrm{resolve}} - C_{\mathrm{entry}}\) large **and** hold-to-resolution allowed | **LEGACY INTENDED** / hybrid | Different product mode |
| Midpoint-based edge | **REJECT** for execution decisions | Not executable |

### 4.2 Direction and leg selection

| Idea | Classification |
|------|----------------|
| Always select max valid positive economic edge leg | **LEGACY IMPLEMENTED** (max edge); **CANONICAL** — including less-probable legs |
| Require \(\mathrm{sign}(z)\) agreement with selected leg | **REJECT** as core rule (Correction C) | Optional separate min-\(p\) / confidence gate if desired |
| Both legs show edge due to inconsistent books | Quality/arbitrage diagnostic | Do not enter; remain single-leg |
| Tie / near-tie handling | Prefer no entry | Configurable epsilon |

### 4.3 Timing

| Idea | Classification |
|------|----------------|
| Fixed \(\tau\) band (e.g. 60–210s) | **LEGACY IMPLEMENTED**; workable but crude |
| Dynamic \(\tau\) vs required edge (larger edge when \(\tau\) small) | **ECONOMIC INFERENCE** / **OPTIONAL EXTENSION** |
| Minimum warm-up before any entry | **LEGACY IMPLEMENTED** (σ ready); **PROPOSED CANONICAL** |
| Early-window entry | Attractive if σ ready and K locked; cold-start often blocks | Evidence-dependent |
| Late-window entry | Higher \(z\) sensitivity, worse liquidity, harder exits | Prefer stricter edge / often block |
| Fixed bands forever | **USER DECISION** whether to keep as config or replace with dynamic rule |

### 4.4 Signal strength / quality

| Idea | Classification |
|------|----------------|
| \(z\) band (min/max \|z\|) | **LEGACY IMPLEMENTED** |
| Extreme-\|z\| reject (`block_abs_z`) | **LEGACY IMPLEMENTED**; **PROPOSED CANONICAL** |
| Jump-guard block | **LEGACY IMPLEMENTED**; **PROPOSED CANONICAL** |
| Basis confidence gate | **LEGACY IMPLEMENTED**; **PROPOSED CANONICAL** |
| Book/liquidity quality | **LEGACY IMPLEMENTED**; **PROPOSED CANONICAL** |
| Explicit model uncertainty band beyond jump/σ-ready | **OPTIONAL EXTENSION** |

### 4.5 Execution-aware entry

| Idea | Classification |
|------|----------------|
| Executable ask (not mid) | **LEGACY IMPLEMENTED**; **PROPOSED CANONICAL** |
| Depth / participation cap | **LEGACY INTENDED** (participation mentioned); recommend core depth check | **PROPOSED CANONICAL** minimum depth for size |
| Fee curve φ from venue `fd` | **LEGACY IMPLEMENTED**; **PROPOSED CANONICAL** |
| Slippage ticks | **LEGACY IMPLEMENTED**; configurable |
| Model-capped max fill price | **LEGACY IMPLEMENTED**; **PROPOSED CANONICAL** |
| Partial entry allowed | **USER DECISION**; recommend **no** for core (FAK all-or-reduce complexity) |
| Unfilled retry | **LEGACY INTENDED**/ops; host concern with strategy epoch rules | Allow bounded retry **without** changing thesis epoch |
| Scaling / multiple adds | **OPTIONAL EXTENSION**; recommend **REJECT** for initial canonical |

### 4.6 Entry summary recommendation

**PROPOSED CANONICAL entry:**

```text
Enter single leg when:
  K class acceptable for mode
  model ready (σ, no jump)
  quality gates pass (feeds, basis, books, fees, clock policy)
  selected_leg = argmax valid positive economic edge (no z-sign rule)
  E_repricing (or mode-appropriate E_settlement per §0.5) ≥ threshold
  size fits depth and fixed notional policy
  lifecycle allows exactly one entry attempt lineage per window
```

---

## 5. Full position-management spectrum

After entry, the position is a **continuously valued claim**, not a dormant stop-watch.

### 5.1 Continuous evaluation variables

| Variable | Why |
|----------|-----|
| Current \(p_{\mathrm{held}}\) | Settlement EV of held leg |
| Executable bid (depth-aware) | Gross sell price before/inside convention |
| \(V_{\mathrm{sell,unit}}\) | \(bid_{\mathrm{executable}} - fee_{\mathrm{sell}} - slip_{\mathrm{sell}}\) (no double slip) |
| Remaining hold edge | \(p_{\mathrm{held}} - V_{\mathrm{sell,unit}}\) (diagnostic dual) |
| Market richness | \(V_{\mathrm{sell,unit}} - p_{\mathrm{held}}\) (canonical realization signal) |
| Unrealized mark-to-market | \(V_{\mathrm{sell}} - C_{\mathrm{entry}}\) |
| \(V_{\mathrm{sell}}, V_{\mathrm{resolve,adj}}\), continuation policy | Decision economics (§7 / §0) |
| Remaining \(\tau\) | Time/liquidity/sensitivity |
| Model uncertainty / jump / σ change | Thesis confidence |
| Basis change | Model validity |
| Liquidity deterioration | Exit feasibility |
| Data freshness | Authority of all of the above |
| Entry cost basis | PnL and stop reference |
| Expected exit fees/slip | Round-trip truth |
| Max loss / risk flags | Overlay protection |

### 5.2 Economic alternatives while active

| Action | Meaning | Canonical? |
|--------|---------|------------|
| **HOLD** | Keep full position; thesis/economics still favor continuation | Core |
| **EXIT FULLY** | Flatten inventory via sell path | Core |
| **REDUCE** | Sell part | **OPTIONAL EXTENSION** — adds complexity; recommend **out of core** |
| **TAKE PROFIT** | Exit because economics realized (see §6B) | Core *as model-based realization*, not arbitrary % TP |
| **STOP** | Exit because thesis false and/or loss limit | Core (split thesis vs dollar stop) |
| **HOLD TO RESOLUTION** | Prefer awaiting payout **while** it remains economically superior and operationally available; re-compare every tick | Core capability; default unavailable until settlement support |
| **EMERGENCY FLATTEN** | Urgent controlled flatten of **confirmed** exposure only | Core (risk/ops) |

**Partial reduction / scaling:** economically optional; for a tiny single-window binary with fixed USD, full exit is simpler and usually sufficient. **PROPOSED CANONICAL:** full exits only; reduce/scale = **OPTIONAL EXTENSION**.

---

## 6. Full exit spectrum

### A. Edge-realization / market-repricing exit

**Motivation:** entered because token was cheap vs \(p\); if **net sell-now proceeds** are rich vs model \(p_{\mathrm{held}}\), the mispricing is harvested.

**Legacy Phase B docs used** \(p - bid - \phi(bid) - slip\) and exited when that quantity was sufficiently negative. That expression has the **wrong economic sign** for sell-side richness once fees reduce proceeds (Correction A). Treat legacy formula as historical intent only.

**CANONICAL sell-side realization:**

\[
V_{\mathrm{sell,unit}} = bid_{\mathrm{executable}} - fee_{\mathrm{sell}} - slippage_{\mathrm{sell}}
\]

\[
\mathrm{market\_richness} = V_{\mathrm{sell,unit}} - p_{\mathrm{held}}
\]

Exit (discretionary) when \(\mathrm{market\_richness} \ge \theta_{\mathrm{rich}}\) and liquidity/depth qualify.

Diagnostic dual: \(\mathrm{remaining\_hold\_edge} = p_{\mathrm{held}} - V_{\mathrm{sell,unit}}\).

| Interpretation of “edge disappeared” | Meaning |
|--------------------------------------|---------|
| \(V_{\mathrm{sell,unit}}\) rose toward/through \(p_{\mathrm{held}}\) | **Success / realization** |
| \(p_{\mathrm{held}}\) fell toward sell value (model invalidated) | **Invalidation** — thesis path |
| Both moved | Decompose via \(\Delta p\) vs \(\Delta V_{\mathrm{sell}}\) in facts |

Still eligible even if resolution-hold is available (Correction B).

**REJECT:** midpoint “TP”; BUY-side \(p-bid-fee-slip\) as sell richness.

### B. Take-profit exit

| Variant | Fit to Z-Gap |
|---------|--------------|
| Fixed token-price TP (e.g. +0.05 from entry) | Weak — ignores \(p\) and fees; **REJECT** as core |
| % return on collateral | Risk overlay possible; not thesis-native | **OPTIONAL** risk policy |
| Captured fraction of original entry edge | Coherent with mispricing harvest | **OPTIONAL EXTENSION** |
| Model-based rich exit (§A) | Best aligned | **PROPOSED CANONICAL** |
| Time-dependent TP | Possible | **OPTIONAL** |
| Liquidity-aware TP (require depth) | Necessary qualifier on §A | **PROPOSED CANONICAL** qualifier |

### C. Thesis-invalidation exit

**LEGACY IMPLEMENTED:** adverse-\(z\) stop with confirmation (UP: \(z \le -z_{\mathrm{stop}}\); DOWN: \(z \ge +z_{\mathrm{stop}}\)).

**Correction F:** \(z\) and \(p=\Phi(z)\) are monotonic duals of the same model state. Example: \(z=-0.25 \Rightarrow p_{UP}\approx 0.401\). The important decision is the **held-leg probability threshold + confirmation**, not the storage variable.

**CANONICAL:** invalidate when \(p_{\mathrm{held}}\) stays below \(p_{\mathrm{stop}}\) (or equivalent mapped from legacy \(z_{\mathrm{stop}}\)) for ≥ `stop_confirm_s`, with feeds/model valid. Always emit both \(p_{\mathrm{held}}\) and \(z\) in facts for parity and diagnostics.

| Related failure | Classification |
|-----------------|----------------|
| \(S\) crosses \(K\) against held side | Optional diagnostic, not core |
| Market richness negative because **model** moved | Invalidation path (not rich exit) |
| Basis / K invalid after entry | Model-validity flatten (Layer 3), not thesis |

```text
Thesis false ⇔ held-leg settlement probability has deteriorated past the
configured threshold for the confirmation duration, with valid model inputs.
```

### D. Price / P&L stop-loss

| Protection | Role |
|------------|------|
| Thesis invalidation | “Model says we were wrong” |
| Dollar / % / MAE stop | “We cannot afford this path even if model still likes it” |

**ECONOMIC INFERENCE:** both can be needed. Model can stay “optimistic” while liquidation value collapses (liquidity hole, fee spike, gap).

**PROPOSED CANONICAL:** hard max loss (risk overlay) **in addition to** thesis invalidation.  
**REJECT** as sole exit: entry-price stop copied from trend systems without model context (**LEGACY INTENDED** already rejected entry-price TP/SL as primary).

### E. Time-based exit

| Rule | Notes |
|------|-------|
| Max holding duration | Independent of window end; optional |
| Flatten deadline before `event_end` | **LEGACY IMPLEMENTED** (20s); value is **USER DECISION** |
| Liquidity-sensitive earlier flatten | **ECONOMIC INFERENCE** near expiry |
| Interaction with hold-to-resolution | Deadline deferred only if hold mode active and settlement path exists | **LEGACY INTENDED** |

Do **not** treat 20 seconds as economically proven — it is an operational default.

### F. Hold-to-resolution

For quantity \(q\):

| Value | Definition |
|-------|------------|
| \(V_{\mathrm{sell}}\) | \(q \times V_{\mathrm{sell,unit}}\) |
| \(V_{\mathrm{resolve,gross}}\) | \(q \times p_{\mathrm{held}}\) |
| \(V_{\mathrm{resolve,adj}}\) | \(V_{\mathrm{resolve,gross}}\) − binary-risk − settlement/redeem − capital-lock penalties (contracts defined; **not** pre-calibrated) |

**Correction B:** resolution eligibility makes HOLD_TO_RESOLUTION *available*; it does **not** sticky-suppress sells. Each evaluation compares permitted actions:

```text
if V_sell sufficiently superior to V_resolve,adj → SELL NOW (EXIT_FULLY)
else if resolution available and qualified → may prefer HOLD_TO_RESOLUTION
else → CONTINUE / other exits per hierarchy
```

Point of no return (irreversible redeem/commit) is the only sticky exception.

**Infrastructure:** resolution detection, payout/redeem/reconcile, recovery while settlement-pending — required before enabling availability. Default: capability **unavailable** → mandatory pre-resolution monetization path.

### G. Signal reversal

If model flips from favoring UP to DOWN:

| Policy | Economics | Classification |
|--------|-----------|----------------|
| Exit only | Stops bleeding; avoids churn | **PROPOSED CANONICAL** with no same-window re-entry |
| Exit and wait | Same | Aligns with one-idea-per-window |
| Exit, confirm flat, reassess | Correct ownership of inventory | **PROPOSED CANONICAL** mechanics |
| Reverse same window | Can capture flip; high churn/fee risk | **OPTIONAL EXTENSION** / **USER DECISION** |
| Simultaneous opposite | Hedged mess / double fees | **REJECT** |

**LEGACY IMPLEMENTED:** `no_reentry_after_exit` / one position per window.

### H. Risk and emergency exits

| Trigger | Owner class |
|---------|-------------|
| Kill switch | Shared risk / ops |
| Max loss / max exposure | Shared risk (+ strategy config) |
| Stale market or reference data | Ops/quality → flatten or block |
| PTB uncertainty after entry | Strategy validity → flatten/block |
| Clock uncertainty | Mode-dependent policy |
| Unknown submission / lifecycle inconsistency | Framework/ops — **block + reconcile; no blind sell** (Correction E) |
| Venue/account disagreement | Framework/ops |
| Market suspension | Ops |
| Operator intervention | Ops |

Separate strategy discretionary exits from shared-risk/emergency exits. Emergency flatten = urgent controlled flatten of **confirmed** quantity only.

---

## 7. Canonical decision economics (active position)

### 7.1 Quantities

For confirmed sellable quantity \(q\):

| Symbol | Definition |
|--------|------------|
| \(C_{\mathrm{entry,unit}}\) | \(ask + fee_{\mathrm{buy}} + slip_{\mathrm{buy}}\) |
| \(C_{\mathrm{entry}}\) | \(q \times C_{\mathrm{entry,unit}}\) (or actual fill all-in when known) |
| \(V_{\mathrm{sell,unit}}\) | \(bid_{\mathrm{executable}} - fee_{\mathrm{sell}} - slip_{\mathrm{sell}}\) (no double slip) |
| \(V_{\mathrm{sell}}\) | \(q \times V_{\mathrm{sell,unit}}\) |
| \(\mathrm{PnL}_{\mathrm{liquidation}}\) | \(V_{\mathrm{sell}} - C_{\mathrm{entry}}\) |
| \(V_{\mathrm{resolve,gross}}\) | \(q \times p_{\mathrm{held}}\) |
| \(V_{\mathrm{resolve,adj}}\) | gross − binary-risk − settlement/redeem − capital-lock (**penalty contracts; uncalibrated**) |
| \(\mathrm{PnL}_{\mathrm{resolve,adj}}\) | \(V_{\mathrm{resolve,adj}} - C_{\mathrm{entry}}\) |
| \(\mathrm{market\_richness}\) | \(V_{\mathrm{sell,unit}} - p_{\mathrm{held}}\) |
| \(\mathrm{remaining\_hold\_edge}\) | \(p_{\mathrm{held}} - V_{\mathrm{sell,unit}}\) |
| Continuation | Policy approximation from \(p_{\mathrm{held}}\), richness, \(\tau\), thesis, resolution eligibility — **not** optimal stopping |

Entry edges \(E_{\mathrm{settlement}}\) / \(E_{\mathrm{repricing}}\) are defined in §0.5.

### 7.2 Relation to hierarchy

Full \(V_{\mathrm{continue}}\) estimator is **not** required initially. Market-rich exit operationalizes discretionary realization.

---

## 8. Canonical exit decision hierarchy

```text
Layer 1 — State truth
  UNKNOWN → block new risk → reconcile → flatten only confirmed qty → else manual

Layer 2 — Emergency / shared risk
  kill, disagreement, max exposure, hard max liquidation loss, critical ops failure

Layer 3 — Model validity
  bad K, basis, stale ref/book, clock, σ anomaly
  (per failure: entry-only block vs flatten vs bounded wait vs manual)

Layer 4 — Thesis invalidation
  p_held breached for confirmation → EXIT_FULLY reason (intent, not OMS)

Layer 5 — Economic action comparison (among permitted actions)
  compare V_sell vs V_resolve,adj vs continuation policy
  market-rich exit remains eligible even if resolution hold available

Layer 6 — Time / operational deadline
  if resolution unavailable → exit
  if available → explicit sell vs resolve choice (no accidental drift)

Layer 7 — Hold
  only if nothing higher requires exit and continuation remains permitted
```

### Conflict examples

| Situation | Resolution |
|-----------|------------|
| Profitable MTM but thesis invalidated | Thesis → EXIT_FULLY |
| Losing MTM but model still strong | Hold unless hard loss |
| Rich signal but poor liquidity | Do not toxic-dump; wait or risk-only exit |
| Deadline with large mark loss | Deadline/risk wins; confirmed qty only |
| Stale data near resolution | Fail closed; no silent hold |
| Opposite signal while exit pending | Finish flatten; no same-window reverse |
| Resolution-qualified but sell much better | **SELL NOW** (Correction B) |
| UNKNOWN inventory | Reconcile; never guess-sell |

---

## 9. Complete state and decision model

Keep **four axes** separate.

### 9.1 Strategy decision state (examples)

`NOT_ELIGIBLE` · `WARMING_UP` · `READY` · `ENTRY_OPPORTUNITY` · `SKIP` · `ENTRY_DESIRED` · `ACTIVE_EVAL` · `EXIT_DESIRABLE` · `EXIT_REQUIRED` · `HOLD_TO_RESOLUTION_SELECTED` · `BLOCKED`

### 9.2 Order / execution state

`FLAT_NO_ORDER` · `ENTRY_PENDING` · `PARTIALLY_ENTERED` · `EXIT_PENDING` · `CANCEL_PENDING` · `UNKNOWN_SUBMISSION`

### 9.3 Economic inventory state

`FLAT` · `FLAT_WITH_DUST` · `POSITION_OPEN` · `RESIDUAL_EXPOSURE` · `UNKNOWN`

### 9.4 Terminal outcome / provenance

`AUTOMATIC_COMPLETE` · `RESOLVED_PAYOUT` · `EXTERNAL_OR_MANUAL_FLAT` · `MANUAL_INTERVENTION` · `FAILED_UNRESOLVED`

**REJECT:** collapsing all four into one enum.

`PARTIALLY_ENTERED`: acknowledge as a real state; core policy may still forbid intentional partial targeting (**USER DECISION**).

---

## 10. Full strategy variants

| Variant | Rationale | Added needs | Risk | Evidence needed | Complexity | Include in future canonical? |
|---------|-----------|-------------|------|-----------------|------------|------------------------------|
| **Mispricing capture, mandatory pre-resolution exit** | Harvest repricing; avoid settlement ops | Rich exit, \(V_{\mathrm{sell}}\), time flatten | Misses resolution EV; exit toxicity | Round-trip PnL after costs | Medium | **Strong core candidate** |
| **Fair-value entry + conditional hold-to-resolution** | Capture extreme \(p\) via payout | Settlement/redeem, hold policy | Binary blowups; ops burden | Hold vs sell regret analysis | High | **Define; enable later** (**USER DECISION**) |
| **Conservative one-entry / thesis+time exits only** | Minimal; what Phase A coded | Low | Leaves money on table; weak round-trip logic | Ops safety only | Low | Useful **evidence slice**, not full identity |
| **Richer dynamic exits** (\(V_{\mathrm{cont}}\), dynamic τ) | Better economics | Calibration | Overfit | Offline studies | High | **OPTIONAL EXTENSION** |
| **Same-window reversal** | Trade flips | Re-entry rules | Churn | Fee-aware flip sims | Medium | **OPTIONAL**; default off |
| **Partial profit / scale-out** | Lock gains | Reduce intents | Complexity | Rarely needed at $5 size | Medium | **OPTIONAL**; default off |

---

## 11. Proposed full canonical strategy

Name for discussion: **Z-Gap Fair-Value Mispricing (full spectrum definition)** — not “Phase A/B/C”.

### 11.1 Canonical core (**CANONICAL** planning baseline — see §0)

| Element | Specification |
|---------|---------------|
| Market family | Polymarket BTC Up/Down 5-minute binaries |
| Hypothesis | Short-horizon \(p=\Phi(z)\) detects temporary underpricing vs settlement probability; monetize via **repricing** and/or **resolution** when available and superior |
| PTB requirement | Frozen resolution-aligned \(K\); quality classes; provider not hardcoded in strategy |
| Reference price | Trading \(S\) + basis vs settlement-associated reference |
| Fair-value model | EWMA σ; \(z=\ln(S/K)/(\sigma\sqrt{\tau_{\mathrm{eff}}})\); \(p_{UP}=\Phi(z)\) |
| Entry | Argmax valid positive edge (less-probable legs allowed); \(E_{\mathrm{repricing}}\) when hold unavailable; depth/fees/caps/gates |
| Sizing | Fixed tiny notional; full position only |
| Active evaluation | \(V_{\mathrm{sell}}\), market richness, \(p_{\mathrm{held}}\), \(V_{\mathrm{resolve,adj}}\), τ, validity |
| Exit families | Layers 1–7 (§8); market-rich canonical realization; \(p_{\mathrm{held}}\) thesis |
| Hold-to-resolution | Available when capability on; **re-compared every tick**; not sticky suppression |
| Unknown | Block → reconcile → confirmed flatten only |
| Reversal / re-entry | None same window |
| Risk interaction | Shared kill/max loss/exposure override discretionary holds |
| Evidence | Settlement edge, repricing edge, realized RT PnL, exit-family attribution |

### 11.2 Configurable policy (identity-preserving)

Thresholds: \(\theta_{\mathrm{take}}\), \(\theta_{\mathrm{rich}}\), \(z_{\mathrm{stop}}\) / \(p_{\mathrm{stop}}\), confirm seconds, \(\tau\) bands or dynamic schedule, basis max, flatten deadline, max loss, hold thresholds if enabled, fee/slip assumptions.

### 11.3 Optional extensions

Dynamic \(\tau\)-edge schedule; \(V_{\mathrm{cont}}\) estimator; same-window reversal; scale-out; Kelly; maker entry; multi-window portfolio constraints; \(z_{\mathrm{neutral}}\) timeout.

### 11.4 Rejected for strategy identity

| Reject | Why |
|--------|-----|
| Dual-leg / pair entry | Different strategy |
| Midpoint “edge” trading | Not executable |
| Entry-price TP/SL as primary thesis logic | Misaligned with fair-value hypothesis |
| \(\mathrm{sign}(z)\) leg-consistency rule | Blocks valid underpriced tails (Correction C) |
| BUY-side \(p-bid-fee-slip\) as sell richness | Wrong economic sign (Correction A) |
| Sticky hold that suppresses superior sells | Correction B |
| Blind emergency sell on UNKNOWN inventory | Correction E |
| Simultaneous opposite exposure | Inventory incoherence |
| Treating provisional/mismatched K as tradeable in real modes | Model invalid |
| Assuming Chainlink is definitionally \(K\) without alignment proof | Provider ≠ requirement |
| Collapsing inventory/outcome/strategy states | Unsafe operations |

---

## 12. Functional needs (capability list — no framework mapping)

| Capability | Why needed | Input | Output | Required state | Failure | Mandatory? |
|------------|------------|-------|--------|----------------|---------|------------|
| Market/window discovery | Know what is trading | Slug/URL/schedule | Market id, tokens, times | Metadata complete | Wait/block | Mandatory |
| Canonical PTB | Valid \(K\) | Resolution rule + sources | K + quality class + lock | Locked or class-tagged | Block entry if inadequate | Mandatory |
| Time authority | Honest \(\tau\), confirms | Sync sources | Corrected now + uncertainty | Synced per mode policy | Degrade/block | Mandatory |
| Reference-price alignment | \(S\) and basis validity | Spot + settlement-associated ref | \(S\), basis, freshness | Fresh enough | Skip/flatten | Mandatory |
| Volatility & fair value | \(p,z\) | \(S,K,\sigma\) hist, \(\tau\) | FV snapshot | Model ready | No entry; cautious exits | Mandatory |
| Fee & friction modeling | Real edges | `fd`, tick, slip policy | φ, edge, caps | Fee resolved | No discretionary trade | Mandatory |
| Decision snapshots | Auditable tick | Books+model+flags | Snapshot | Consistent timestamping | Skip | Mandatory |
| Entry opportunity evaluation | When to enter | Snapshot + config | ENTER/SKIP + evidence | Ready lifecycle | Skip | Mandatory |
| Lifecycle-aware monitoring | Post-entry decisions | Position + snapshot | HOLD/EXIT/… | Active/pending | Fail closed | Mandatory |
| Sell-now / continue / resolve valuation | Exit economics | Bid, \(p\), fees, mode | \(V_{\mathrm{sell}}, V_{\mathrm{resolve}}\), rich edge | Position open | Conservative exit | Mandatory for full strategy |
| Strategy exits | Realization + thesis + time | Valuations + rules | Exit intents | Active | Retry/escalate | Mandatory |
| Risk exits | Safety | Limits, kill, quality | Flatten | Any | Immediate | Mandatory |
| Hold-to-resolution | Optional monetization | Extreme \(p/z\), settlement ready | Hold flag | Active + settlement capability | Fall back to flatten | Optional capability / identity decision |
| Order/fill/position truth | Know holdings | OMS/venue | Inventory truth | Consistent | Unknown → block | Mandatory |
| Persistence & recovery | Restart honesty | Snapshots | Restored phases | Non-terminal rules | Fail closed | Mandatory |
| Facts & reports | Learn & audit | All stages | JSONL/summaries | Always | Degraded logging | Mandatory |
| Calibration & analysis | Prove/disprove edge | Facts + outcomes | Reliability, PnL by exit family | Post-window | Incomplete study | Mandatory for conviction; not for first coding |

---

## 13. Decision register

### Defines the strategy identity

| ID | Decision | Options | Economic consequences | Operational consequences | Recommendation | Evidence that helps | Provisional? |
|----|----------|---------|----------------------|--------------------------|----------------|---------------------|--------------|
| I1 | Monetization mode | (a) Mandatory pre-resolution exit (b) Hybrid with conditional hold (c) Resolution-seeking primary | (a) round-trip only (b) mixed (c) binary terminal | (c)/(b) need settlement | **(b) defined, default hold OFF** → behaves like (a) until enabled | Hold vs sell regret studies | Yes: (b)/OFF |
| I2 | Primary discretionary profit exit | (a) Rich/repricing only (b) also % TP (c) none (thesis+time only) | (c) is incomplete round-trip | (a) needs bid+fee exits | **(a)** | Observe rich-edge frequency | Yes: (a) |
| I3 | Same-window reversal | (a) forbid (b) allow after flat | (b) more trades/fees | More lifecycle complexity | **(a)** | Flip-churn analysis | Yes: (a) |
| I4 | One entry vs scaling | (a) one (b) scale-in/out | (b) complexity | Partial fills | **(a)** | Tiny size makes (b) low value | Yes: (a) |

### Defines the model

| ID | Decision | Options | Recommendation | Evidence | Provisional? |
|----|----------|---------|----------------|----------|--------------|
| M1 | Canonical K sources & attestation | Chainlink boundary / metadata / dual-attested / … | Require **alignment proof**; treat Chainlink as **candidate source** | Resolution post-mortems; mismatch stats | No silent default to unverified source |
| M2 | Trading \(S\) vs settlement reference | Binance+\(S_{CL}\) basis gate vs single source | Keep dual-reference basis gate | Basis vs outcome error | Yes: Binance \(S\) + basis gate |
| M3 | Invalidation metric | \(p_{\mathrm{held}}\) threshold (canonical) vs raw \(z\) only | **\(p_{\mathrm{held}}\) + confirm; always log \(z\)** | Exit attribution / Φ parity | Yes: map legacy \(z_{\mathrm{stop}}\) → \(p\) |
| M4 | Entry edge definition | \(E_{\mathrm{settlement}}\) vs \(E_{\mathrm{repricing}}\) by mode | **§0.5** | Simulated exit friction | Yes: require \(E_{\mathrm{repricing}}\) when hold unavailable |
| M5 | Realization exit | Market-rich vs remaining-edge threshold | **Market-rich canonical** | Observe richness histograms | Yes |

### Defines risk appetite

| ID | Decision | Options | Recommendation | Provisional? |
|----|----------|---------|----------------|--------------|
| R1 | Hard max loss | On/off + magnitude | On, tiny | Yes |
| R2 | Flatten deadline | Fixed seconds / dynamic / none if hold | Fixed when hold OFF | Yes: keep configurable deadline |
| R3 | Stale-data near expiry | Always flatten vs wait | Flatten/fail-closed | Yes |
| R4 | Sizing | Fixed USD | Fixed tiny | Yes |

### Can be calibrated later

\(\theta_{\mathrm{take}}\), \(\theta_{\mathrm{rich}}\), \(z_{\mathrm{stop}}\), confirm seconds, \(z\)/\(\tau\) bands, basis max, slip ticks, participation, hold thresholds, dynamic schedules.

---

## 14. Explicit exclusions (this task)

- No code or framework changes  
- No Z1–Z4 roadmap  
- No module-to-framework allocation  
- No live/network/`.env`/`var/state` operations  
- No `old/` imports  
- No automatic acceptance of Phase A/B/C as canonical  
- No silent acceptance/rejection of hold-to-resolution or Chainlink  
- No commit/push  

---

## 15. Relationship to the Phase A audit

| Document | Answers |
|----------|---------|
| `z0_z_gap_design_audit.md` | What legacy **implemented** Phase A did, with formula traces |
| **This document** | What full Z-Gap **should** consider economically; proposed complete identity; decisions to freeze |

Phase A is a **conservative executable slice** of a larger intended spectrum (rich exit + hold-to-resolution + settlement were documented as later work). Treating Phase A as the whole strategy would omit the monetization path the hypothesis most naturally suggests for a round-trip trader: **repricing realization**.

---

## 16. Stop point / P0 freeze

Corrections A–F and the planning identity in §0 are frozen with [`z_gap_full_strategy_implementation_plan.md`](z_gap_full_strategy_implementation_plan.md) as the P0 design baseline. Remaining open product choices (especially PTB provider selection and numeric thresholds) must not reopen sell-side sign, sticky-hold, z-sign leg-selection, F1 resolution leakage, or non-atomic decision inputs. **F1 implementation is a separate authorization.**
