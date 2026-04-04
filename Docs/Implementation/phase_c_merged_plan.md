# Tyrex_PM — Phase C Plan (Merged)

## Purpose

This document defines **Phase C** as a concise, implementation-ready planning artifact.

- **Phase A** made framework state real and readable.
- **Phase B** enforced risk using that real state.
- **Phase C** improves how Tyrex captures a trusted guru’s edge under real follower constraints.

This document describes the whole phase and its main areas so each area can later be specified and implemented separately.

---

## Precondition

Phase A (framework truth) and Phase B (portfolio-aware risk) are operationally accepted.

Phase C does **not** rebuild state, exposure accounting, balance truth, or Phase B risk semantics.

---

## Phase C Objective

**Maximize follower alpha capture — not follower activity.**

Given a skilled guru, Phase C should reduce the gap between:
- the guru’s realized edge, and
- the follower’s realized edge.

Phase C does this through three objective areas:
1. **Reduce time-to-follow**
2. **Improve capital allocation**
3. **Improve execution quality**

---

## Optimization Target

Phase C exists to reduce **follower alpha decay**.

The main sources of alpha decay are:
1. **Detection latency** — follower learns too late.
2. **Guru price impact** — the guru already moved the book.
3. **Capital asymmetry** — the follower cannot fund everything.
4. **Signal quality variation** — not every guru trade deserves equal capital.
5. **Execution slippage** — the follower’s own order gives away price.
6. **Exit timing asymmetry** — guru exits are detected later and are harder to interpret.

Phase C focuses first on the top three controllable areas with the highest expected P&L impact.

---

## Architectural Principles

### Ownership split
- **Signal / policy** decides whether a guru signal is worth following now and how much to target.
- **Risk** decides whether the follow is safe and allowed.
- **Execution** decides how to express an approved follow at the venue.

### Design rule
Each Phase C feature should:
- map to a specific alpha leak,
- have a narrow swappable interface,
- produce explicit logs / reasons,
- have a measurable success metric.

### Explicit non-goals
Do **not**:
- rebuild guessed counters or private exposure state,
- move balance / allowance / order bookkeeping into `CopyStrategy`,
- replace Phase B gates with venue rejects,
- optimize for number of follows instead of captured alpha.

---

## Phase C Areas

| Area | Objective | Main alpha leak addressed | Minimum outcome |
|---|---|---|---|
| **C1 — Time-to-Follow** | Reduce delay and stop chasing stale signals | Detection latency, guru price impact | Faster awareness and freshness-aware skipping |
| **C2 — Capital Allocation** | Spend scarce capital on the best follow opportunities | Capital asymmetry, signal quality variation | Better sizing and better skipping |
| **C3 — Execution Quality** | Reduce implementation loss at entry | Execution slippage, venue constraints | Better order expression and bounded entry quality |

---

## C1 — Time-to-Follow

### Objective
Reduce the time between guru fill and follower submit, and avoid following signals whose edge has already decayed beyond an acceptable threshold.

### Why it matters
Latency is usually the largest single source of copied-edge loss. On thin books, the guru’s own trade may already have moved the price before the follower arrives.

### Current planning assumption
The poll interval is the largest controllable latency source. Internal processing is not the main bottleneck.

### Minimum valuable scope
1. **Adaptive polling / urgency modes**
   - Poll faster when the guru is recently active.
   - Decay back to the base interval when idle.
2. **Stale-edge gate**
   - Before follow, compare current executable price to guru fill price.
   - Skip when price has already moved too far.
3. **Latency telemetry**
   - Measure detection delay, internal processing delay, and submit delay.

### Architectural home
- ingestion / `GuruMonitorActor` for polling behavior,
- signal or pre-trade policy for freshness evaluation,
- risk unchanged except consuming approved intent.

### Minimum interfaces
- `PollPolicy.next_interval(state) -> seconds`
- `SignalFreshnessPolicy.evaluate(signal, market_state) -> allow/skip + reason`

### Example config surface
```yaml
# runtime
base_poll_seconds: 30
guru_poll_fast_seconds: 4
guru_poll_idle_decay_seconds: 120

# policy or risk
max_follow_slippage_cents: 3
```

### Success metrics
- median guru-fill → follower-submit latency,
- percentage of stale signals skipped,
- latency distribution during guru-active windows.

### Deferred
- full event-driven guru ingestion,
- speculative pre-positioning,
- predictive / anticipatory guru trading.

---

## C2 — Capital Allocation

### Objective
Use limited follower capital on the follow opportunities most likely to matter, rather than applying the same treatment to all guru trades.

### Why it matters
A smaller wallet cannot reproduce the guru portfolio exhaustively. The follower must concentrate capital where copied edge is most worth preserving.

### Minimum valuable scope
1. **Conviction-weighted sizing**
   - Replace flat `copy_scale` with a simple heuristic.
   - Initial heuristic: guru trade size relative to guru recent average trade size.
2. **Minimum-follow-notional filter**
   - Skip follows that are economically too small to matter, even if the venue would accept them.
3. **Simple priority under scarcity**
   - When capital is constrained, higher-value signals should win before low-value ones.

### Architectural home
- signal / sizing policy for target size and simple ranking,
- risk remains owner of reserve, caps, concurrency, and safety,
- strategy remains orchestration only.

### Minimum interfaces
- `SizingPolicy.size(signal, context) -> target_notional | skip`
- `FollowPriorityPolicy.rank(signals, context) -> ordered_signals`
- `MinFollowPolicy.evaluate(intent) -> allow/skip + reason`

### Example config surface
```yaml
# strategy / signal
base_copy_scale: 1.0
conviction_sizing_enabled: true
conviction_sizing_cap: 3.0
conviction_sizing_lookback_trades: 20
min_follow_notional_usd: 5.0
```

### Success metrics
- capital deployed on positive-outcome follows,
- return per dollar deployed,
- percentage of trivial follows skipped,
- concentration of capital into higher-value opportunities.

### Deferred
- portfolio optimization,
- correlation-aware allocation,
- active rebalancing of current positions,
- ML-based signal scoring.

---

## C3 — Execution Quality

### Objective
Reduce implementation loss after a follow has been approved by expressing the order more intelligently at the venue.

### Why it matters
Fast follow is not enough if the order crosses a thin book and gives away too much price.

### Minimum valuable scope
1. **Entry price guard**
   - Do not submit if current executable price is beyond acceptable slippage from guru fill.
2. **Limit order with timeout**
   - Use a bounded-price order instead of blindly taking any price.
   - Cancel if not filled within a short configurable window.
3. **Basic book-depth check**
   - Reduce size when intended size would walk the book too aggressively.
4. **Venue normalization**
   - Normalize tick size, min size, and min notional before submit.

### Architectural home
- execution layer / execution port implementation,
- policy may optionally pass urgency,
- risk does not own order-type logic.

### Minimum interfaces
- `ExecutionPolicy.prepare(intent, market_state) -> executable_order | skip`
- `ExecutionPolicy.on_timeout(order_state) -> cancel/keep`

### Example config surface
```yaml
# execution / risk
max_entry_slippage_cents: 3
limit_order_enabled: true
limit_timeout_seconds: 30
book_depth_utilization_cap: 0.5
venue_normalize_enabled: true
```

### Success metrics
- average entry slippage vs guru fill,
- fill rate,
- effective spread paid,
- percentage of follows rejected by entry guard.

### Deferred
- advanced execution algorithms,
- staged / split entry beyond a simple timeout model,
- passive entry strategies,
- adversarial-aware execution.

---

## Mapping Back to the Roadmap

### Already covered by Phase A / B
- framework-visible order / position / balance truth,
- reconciliation and state freshness,
- pending and filled exposure,
- reserve and capital gates,
- portfolio / token caps,
- concurrent follow safety controls.

### Covered in this Phase C plan
- venue normalization,
- smarter follow decision policy,
- minimum execution policy,
- capital-aware follow behavior,
- freshness-aware follow logic.

### Deferred from the original Phase C framing
- per-token cooldown,
- max follows per cycle,
- repeated-buy rules,
- burst prioritization,
- explicit pending-order suppression.

These may still matter, but they should only be added after the three core areas are in place and measured.

---

## Dependencies and Recommended Order

### Dependencies
- **C1** can ship with no new infrastructure beyond Phase A/B.
- **C2** can ship with no new infrastructure beyond Phase A/B.
- **C3** depends more heavily on stable orderbook access and framework-backed order management.

### Recommended implementation order
1. **C1 — Time-to-Follow**
   - highest expected alpha impact per engineering effort,
   - improves upstream signal quality for the rest.
2. **C2 — Capital Allocation**
   - solves the small-wallet problem,
   - improves how scarce capital is used.
3. **C3 — Execution Quality**
   - refines approved follows at the venue,
   - carries the heaviest execution-side requirements.

**Exception:** if live evidence shows book slippage is currently a bigger alpha leak than capital misallocation, move C3 ahead of C2.

---

## Validation Criteria

Each area is validated when its primary metrics improve versus the pre–Phase C baseline in shadow or live.

| Area | Validated when |
|---|---|
| **C1** | Median guru-fill → follower-submit latency drops materially during active windows |
| **C2** | Capital deployed on positive-outcome follows improves vs flat `copy_scale` baseline |
| **C3** | Average entry slippage vs guru fill improves vs naive market-style entry baseline |

### Overall Phase C acceptance
Follower realized return relative to guru realized return improves over a comparable observation window.

---

## What Each Area Spec Should Contain Next

For each area, the next design document should define:
- objective,
- exact minimum feature set,
- interface / composition point,
- config surface,
- logs and reason codes,
- metrics,
- test plan,
- explicit out-of-scope items.

---

## One-Line Working Memory

**Phase C = preserve more of the guru’s edge under real follower constraints.**
