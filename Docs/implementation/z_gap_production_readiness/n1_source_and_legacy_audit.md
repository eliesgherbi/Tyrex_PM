# N1 — Source and legacy audit

**Status:** planned (documentation only — no runtime code)  
**Document:** `Docs/implementation/z_gap_production_readiness/n1_source_and_legacy_audit.md`  
**Baseline:** F1–F5 accepted · branch `rest_project`  
**Depends on:** P0 design baseline + F1–F5 acceptance  
**Unblocks:** N2 (real adapters), N3 (PTB/basis alignment)

---

## 1. Objective

Produce an evidence-driven audit that freezes source, timing, discovery, and
legacy-reuse recommendations **before** any production network adapters for
Z-Gap are implemented.

N1 answers unresolved integration questions with measured or document-backed
evidence. It writes **no** runtime code under `src/`, tests, or configs.

---

## 2. Why the milestone exists

F2–F5 proved the sealed Z-Gap decision path on fixtures. Production readiness
fails if we guess:

- how Polymarket derives the displayed PTB / resolution \(K\);
- which Chainlink / Binance / RTDS path is authoritative vs laggy;
- how market discovery should bind tokens and windows;
- which `old/` operational concepts remain valid under the current architecture.

Implementing adapters before this audit risks hard-coding wrong boundary
semantics, wrong PTB provenance, or incomplete feed lifecycles (the Path A/B
split that blocked old tiny-live unification).

---

## 3. Scope

### A. Chainlink / PTB source proof

| Question | Method |
|----------|--------|
| Do BTC Up/Down 5m market rules use Chainlink BTC/USD Data Streams? | Read market rules / resolution text for sample windows; record exact wording |
| Can Polymarket RTDS supply Chainlink prices? | Document official endpoint `wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`, symbol `btc/usd` ([Polymarket RTDS docs](https://docs.polymarket.com/market-data/websocket/rtds)) |
| How is the **displayed** PTB derived at the five-minute boundary? | Browser network inspection of the live Polymarket market page at/around a boundary; classify each candidate as structured endpoint, RTDS, embedded page data, or other |
| Is “first tick at or after boundary” correct? | **Do not assume.** Compare candidate algorithms against displayed PTB and resolution-aligned evidence across multiple windows |

**Recording model (audit artifacts only):**

| Label | Meaning |
|-------|---------|
| `candidate_ptb` | Value from a proposed capture rule (e.g. first RTDS tick with `source_ts >= event_start`) |
| `confirmed_ptb` | Independently attested value (display agreement and/or post-resolution K) |
| `mismatch_evidence` | Structured diff: sources, timestamps, bps, window_id, capture rule id |

Prefer supported structured sources. HTML parsing may be planned only as a
**secondary attestation/fallback**, never as the primary hot-path source.

### B. Source latency comparison

Plan a timestamped, offline-analyzable capture campaign comparing:

1. Polymarket RTDS Chainlink BTC/USD (`crypto_prices_chainlink` / `btc/usd`)
2. Polymarket RTDS Binance BTC/USDT (`crypto_prices` / `btcusdt`)
3. Direct Binance Spot WebSocket (existing `@trade` adapter path)
4. PTB displayed by Polymarket (from A)

**Per-message / per-window metrics:**

| Metric | Definition |
|--------|------------|
| `source_ts` | Provider event timestamp |
| `receive_ts` | Local receive wall time |
| Sequencing | Message order / gaps |
| Missing messages | Detected drops after reconnect |
| Reconnects | Count + gap duration |
| Boundary arrival delay | `receive_ts - event_start` for first usable post-boundary tick |
| Displayed PTB delay | When UI shows PTB vs `event_start` |
| Mismatches | Cross-source price disagreement at paired times |

Goal: select sources from measured behavior, not assumption.

### C. Market discovery

Evaluate and recommend:

| Concern | Current baseline | Audit output |
|---------|------------------|--------------|
| Deterministic 5m slug | `btc-updown-5m-{epoch}` via `btc_5m_window.py` | Confirm vs live Gamma |
| Gamma lookup by exact slug | `GammaMarketDiscovery` | Latency, failure modes, retries |
| Active-event discovery fallback | Partial / CLI-era patterns | When slug miss → how to recover without wrong market |
| Token / outcome mapping | YES/NO token IDs from Gamma | Validation checklist |
| Start/end timestamps | Slug-epoch = trading start (Gamma `startDate` must not override) | Reconfirm |
| Resolution rule / source validation | `BinaryResolutionRule` contract exists | Proof that rules text matches Chainlink Data Streams |

### D. `old/` review (concepts only)

Produce a keep / adapt / reject table covering:

- one-run path;
- continuous window rollover;
- adapters;
- rollover / subscription replacement;
- reconnection / heartbeat;
- timing / TimeAuthority;
- reconciliation;
- live safety (preflight, kill, approval).

**Hard rule:** no imports from `old/`. Reuse only concepts; reimplement through
current ports and hosts.

---

## 4. Explicit non-goals

- No adapters, hosts, strategy, OMS, or config changes
- No live orders, credentials use for trading, or `.env` mutation
- No accepting “first tick ≥ boundary” without evidence
- No treating Binance as Chainlink / resolution truth
- No HTML scraping as the planned primary PTB path
- No profitability study
- No N2–N7 implementation

---

## 5. Dependencies and entry criteria

| Entry criterion | Status at plan time |
|-----------------|---------------------|
| F1–F5 accepted | Required |
| Clean understanding of `PtbSnapshot` / `PtbLockStore` | Available (`domain/polymarket/ptb.py`) |
| Gamma discovery + CLOB WS + Binance WS exist | Available (momentum live path) |
| RTDS / Chainlink adapter | Absent — N1 plans proof before N2 builds it |
| Operator can run read-only network captures | Required for B (and browser inspect for A) |
| Access to official Polymarket docs | Required |

---

## 6. Decisions that must already be frozen

From P0 / F1–F5 (must not reopen):

1. Thin `ZGapStrategy`; framework owns data/state/risk/OMS/portfolio/lifecycle
2. Provider-independent `PtbSnapshot` + lock immutability
3. OBSERVE and SHADOW share sealed decision path
4. Binance is reference only; not settlement truth
5. No `old/` / `runtime/r7*` / `config/r7/` imports into Z-Gap
6. Resolution hold is explicit intent + capability (F5), not F1 action leakage

---

## 7. Responsibility / module ownership

| Concern | Owner during N1 | Must not own |
|---------|-----------------|--------------|
| Audit plan & evidence artifacts | Operator / docs under this initiative | Strategy code |
| Browser / network capture scripts (if any) | One-off scripts under `scripts/` or `var/reporting/n1/` (optional; still no product adapters) | `strategies/z_gap` |
| Legacy concept extraction | Docs only | Runtime dependency on `old/` |
| Freeze recommendations | This README + decision table in implementation README | Silent code defaults |

---

## 8. Contracts, ports, and data structures to add or evolve

N1 **does not** add production ports. It **specifies** what N2/N3 must implement:

| Planned contract | Purpose |
|------------------|---------|
| `SettlementReferenceTick` (name provisional) | Normalized Chainlink/RTDS tick with `source_ts`, `receive_ts`, symbol, value |
| PTB capture rule id | Versioned algorithm identity recorded in provenance |
| `PtbQuality` evolution notes | Whether attestation upgrades `PROVISIONAL` → `CONFIRMED_CANONICAL` |
| Latency comparison schema | JSONL fact shape for N1 campaign (source, timestamps, window_id) |
| Discovery validation report | Market identity + rule text + token map checksum |

Existing (reuse as targets): `PtbSnapshot`, `PtbLockStore`, `BinaryResolutionRule`,
`MarketDiscovery`, `TimeAuthorityView`.

---

## 9. Expected files / modules affected

**During N1 (docs / optional offline analysis only):**

```text
Docs/implementation/z_gap_production_readiness/n1_source_and_legacy_audit.md  # this plan
Docs/implementation/z_gap_production_readiness/             # initiative docs + future evidence notes
var/reporting/n1/                                          # optional capture outputs (gitignored)
```

**Must not touch during N1 implementation phase:** `src/`, product tests, runtime
config for observe/shadow/live, credentials.

---

## 10. End-to-end data or control flow

```text
[N1 audit campaign — read-only]

Public Polymarket page / Gamma / RTDS / Binance
  → capture with local receive timestamps
  → offline join on window boundaries
  → compare candidate_ptb vs displayed / confirmed
  → latency & mismatch tables
  → keep/adapt/reject from old/
  → freeze recommendations into N1 acceptance report
  → (no adapter, no strategy evaluate, no OMS)
```

---

## 11. Failure and degraded-mode behavior

| Failure | Audit behavior |
|---------|----------------|
| RTDS disconnect mid-window | Record gap; do not invent ticks |
| Displayed PTB unavailable | Mark window incomplete; do not force a K |
| Sources disagree | Record mismatch_evidence; do not pick silently |
| Market rules ambiguous | Block recommendation for live K; escalate as open decision |
| Browser inspect blocked | Fall back to structured-only sources; document limitation |

---

## 12. Persistence and restart behavior

- Capture files append-only under `var/reporting/n1/` (or equivalent)
- Restart mid-campaign: resume next window; never rewrite prior window evidence
- No `StateSnapshotStore` / Portfolio involvement

---

## 13. Facts, metrics, and reporting

Minimum audit outputs:

| Artifact | Contents |
|----------|----------|
| `ptb_source_proof.md` (or section in acceptance report) | Rules text citations; UI provenance classification |
| `latency_comparison.jsonl` | Per-tick / per-window metrics (§3B) |
| `discovery_validation.md` | Slug/Gamma/token/rule checklist results |
| `legacy_keep_adapt_reject.md` | Table for §3D |
| `n1_acceptance.md` | Frozen recommendations + open items deferred |

---

## 14. Configuration ownership and units

| Item | Owner | Units |
|------|-------|-------|
| Capture window count | Operator / N1 config note | count |
| Max boundary lag candidate | Recommendation only | ms |
| Mismatch tolerance candidate | Recommendation only | bps |
| RTDS heartbeat interval (docs) | Official docs (~5s PING) | s |
| CLOB market PING (existing) | Existing adapter (~10s) | s |

No product config schema change in N1.

---

## 15. Test strategy

N1 is evidence + documentation. Verification:

1. Acceptance report answers A–D with citations
2. No `src/` or test changes required for N1 completion
3. Recommendations explicitly labeled `PROVISIONAL` vs `FROZEN`
4. Regression: `python -m pytest tests -q --tb=no` unchanged (464+ baseline)

Optional: schema validation of capture JSONL only if scripts are added.

---

## 16. Deterministic acceptance criteria

N1 is accepted when **all** are true:

1. Market-rule evidence supports or refutes Chainlink Data Streams for BTC 5m UP/DOWN (cited).
2. RTDS subscription parameters documented from official sources.
3. Displayed-PTB provenance classified (structured / RTDS / embedded / other) with capture evidence.
4. At least one explicit statement: boundary sampling rule is **proven**, **refuted**, or **still open** — never silently assumed.
5. Latency comparison completed for the four sources in §3B across a bounded window sample (recommend ≥3 consecutive windows; freeze actual N in report).
6. Discovery evaluation covers slug, Gamma, tokens, timestamps, resolution-rule validation.
7. `old/` keep/adapt/reject table complete for §3D items.
8. Frozen recommendations listed for N2/N3; open decisions linked to master decision table.
9. No application code shipped.

---

## 17. Expected deliverables

- This authoritative README (plan)
- N1 acceptance / evidence report (when N1 is executed)
- Capture schemas + sample metrics
- Frozen adapter/source recommendations for N2
- Frozen PTB quality / confirmation recommendations for N3
- Updated master decision table rows (in `Docs/implementation/z_gap_production_readiness/README.md`)

---

## 18. Stop conditions

Stop N1 and escalate if:

- Resolution rules contradict Chainlink Data Streams without a clear alternate
- No structured path to PTB can be identified and HTML is the only option for hot path
- Latency campaign cannot run (infra/network) and no alternate evidence exists
- Evidence would force strategy-layer network access (architecture violation)

---

## 19. Remaining risks and decisions

| Risk / decision | Why it matters | When to decide |
|-----------------|----------------|----------------|
| Exact boundary sampling semantics | Wrong K invalidates \(p=\Phi(z)\) | Before N3 lock policy; ideally end of N1 |
| PTB confirmation source | Live entry gate | Before N4 non-fixture entry claims |
| Display vs resolution K drift | Model vs payout mismatch | Before N7 |
| RTDS vs direct Binance for trading \(S\) | Latency/basis | Before N3 |
| Deployment clock sync | τ and boundary lag | Before N4 continuous |

**Provisional defaults (safe until N1 evidence):**

- Treat RTDS Chainlink as **candidate** settlement reference, not confirmed K
- Treat direct Binance Spot as primary trading \(S\)
- Treat RTDS Binance as comparison/fallback only
- Max lag / mismatch numbers remain provisional (legacy candidates: 5000 ms lag, 0.5 bps mismatch) until measured

---

## 20. Expected commit boundary

```text
N1 commit theme:
  "Audit Z-Gap PTB sources, latency, discovery, and legacy reuse"

Include: N1 evidence + acceptance under z_gap_production_readiness/; optional reporting samples
Exclude: src/, product tests, runtime configs, credentials, live commands
```

Planning-only commits for this initiative may ship documentation under
`z_gap_production_readiness/` without starting N1 implementation.
