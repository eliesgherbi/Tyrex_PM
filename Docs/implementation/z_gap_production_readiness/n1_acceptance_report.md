# N1 acceptance report — Source and legacy audit

**Verdict:** `PASS_WITH_BLOCKERS`  
**Date:** 2026-07-20  
**Branch:** `rest_project`  
**Baseline HEAD (start):** `022a39c19e48af00419062c71a818df384907f81`  
**Scope:** read-only evidence + documentation under this initiative; audit tools outside `src/` only  
**Unblocks:** N2 read-only adapters (with stated blockers for N3/live PTB semantics)

---

## 1. Verdict

**`PASS_WITH_BLOCKERS`**

N2 may proceed to implement read-only adapters for:

- Polymarket RTDS Chainlink (`crypto_prices_chainlink` / `btc/usd`);
- direct Binance Spot trades;
- Gamma discovery with label-based Up/Down mapping;
- clock-sync snapshot ingress (no network I/O in core TimeAuthority).

N3 / live PTB lock semantics remain blocked on:

- authoritative crypto PTB HTTP endpoint still **OPEN** (no documented structured crypto PTB API found);
- boundary inequality rule **PROVISIONAL** (exact-on-boundary ticks made `first_ge` ≡ `last_le` in all three windows);
- `clock_uncertainty_ms` **OPEN** (not measured; Binance `receive − source` frequently negative);
- RTDS Binance subscription quirks (documented symbol filter returned empty; unfiltered stream works).

HTML/SSR page parsing is **attestation / provenance only**, never the production hot-path PTB source.

---

## 2. Executive conclusions

1. **Resolution source (PROVEN):** BTC Up/Down 5m markets resolve from Chainlink BTC/USD Data Streams (`https://data.chain.link/streams/btc-usd`), cited in Gamma `resolutionSource` and market rules text.
2. **Displayed PTB provenance:** UI “Price To Beat” matches SSR dehydrated React Query `openPrice` for queryKey `["crypto-prices","price","BTC",start,"fiveminute",end]`. Documented equity PTB HTTP exists; crypto analogue **404 / OPEN**. Not promoted to hot path.
3. **Boundary vs display:** Across three consecutive windows, the Chainlink tick with `source_ts == event_start` equals displayed `openPrice` at **0.0 bps** (`MATCH`). Rule classification: **PROVISIONAL** (plumbing + agreement proven; inequality semantics when no exact tick remain OPEN).
4. **Sources:** RTDS Chainlink is the settlement-reference candidate. Direct Binance Spot is the preferred fast trading reference. RTDS Binance is comparison/fallback only (filter bug / lower priority).
5. **Discovery:** Deterministic slug `btc-updown-5m-{epoch}` + exact Gamma lookup is reliable primary. Map `"Up"→UP`, `"Down"→DOWN` by label only.
6. **`old/`:** Keep/adapt operational concepts (reconnect, prepared-next, PTB lock/attestation axes, preflight/kill); reject imports and Z-Gap-specific network ownership in strategy.

---

## 3. Sources and official citations

| Claim | Evidence |
|-------|----------|
| RTDS endpoint | [Polymarket RTDS docs](https://docs.polymarket.com/market-data/websocket/rtds): `wss://ws-live-data.polymarket.com` |
| Chainlink topic/symbol | Same docs: topic `crypto_prices_chainlink`, type `*`, symbol `btc/usd` (slash form); empty `filters` works |
| Binance RTDS topic | Same docs: topic `crypto_prices`, symbol `btcusdt`; **N1 observation:** `filters:"btcusdt"` produced 0 messages; unfiltered stream includes `btcusdt` |
| Equity PTB HTTP (not crypto) | Docs: `GET https://polymarket.com/api/equity/price-to-beat/{slug}` |
| Chainlink resolution | Gamma + UI rules: `resolutionSource` = `https://data.chain.link/streams/btc-usd` |
| Market rule semantics | “Up” if end price ≥ begin price of titled range; else “Down”; Chainlink stream only |

Verified live RTDS subscription (N1 capture):

```json
{
  "action": "subscribe",
  "subscriptions": [
    {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
    {"topic": "crypto_prices", "type": "update"}
  ]
}
```

PING every 5s as documented.

---

## 4. Market samples

Captured/validated windows (UTC 2026-07-20):

| Slug | Market ID | Condition ID | Start → End | Outcomes | Resolution source |
|------|-----------|--------------|-------------|----------|-------------------|
| `btc-updown-5m-1784582100` | 2993863 | `0xb8c3b4d7…8961dc` | 21:15 → 21:20 | Up, Down | Chainlink BTC/USD stream |
| `btc-updown-5m-1784582400` | 2993887 | `0x9442449b…0718cf` | 21:20 → 21:25 | Up, Down | same |
| `btc-updown-5m-1784582700` | 2993890 | `0xb08f0d54…65e594` | 21:25 → 21:30 | Up, Down | same |

Additional Gamma samples (prior windows) confirm identical rule text and `resolutionSource`.  
Token IDs are bound via label index (see discovery validation artifact). Full token IDs in `var/reporting/n1/discovery_validation.json` (gitignored).

**Complete rule text (representative):**

> This market will resolve to "Up" if the Bitcoin price at the end of the time range specified in the title is greater than or equal to the price at the beginning of that range. Otherwise, it will resolve to "Down".  
> The resolution source for this market is information from Chainlink, specifically the BTC/USD data stream available at https://data.chain.link/streams/btc-usd.

---

## 5. PTB / UI provenance findings

| Candidate | Classification | Notes |
|-----------|----------------|-------|
| Documented crypto PTB HTTP | **OPEN / not found** | Equity endpoint documented; crypto `/api/crypto/price*` and `/api/crypto/price-to-beat/{slug}` → 404 in probes |
| SSR dehydrated React Query `openPrice` | **Structured UI state (undocumented HTTP)** | Stable field; matches displayed “Price To Beat” (UI rounds for display) |
| RTDS Chainlink boundary tick | **Primary candidate for hot-path K** | Exact numeric match to `openPrice` in all 3 windows |
| HTML visible text | **Secondary attestation only** | Must not be production hot path |
| Gamma fields | No PTB numeric | Provides rules, IDs, times, tokens |

**Availability:** For an active window, `openPrice` is present in SSR after the window has started; `closePrice` fills after the window ends (and becomes next window’s `openPrice` in sampled cases).

**Priority ranking for production:**

1. RTDS Chainlink tick selected by versioned boundary rule (hot path)  
2. Independent attestation (SSR `openPrice` or future documented crypto PTB API)  
3. HTML text only as last-resort human/debug evidence  

---

## 6. Per-window boundary comparison

Windows: `1784582100`, `1784582400`, `1784582700` (three consecutive complete 5m windows).

Attested K = SSR `openPrice` with matched queryKey start.

| Window | Rule | Candidate K | Displayed K | Exact diff | bps | Boundary recv lag | Result |
|--------|------|-------------|-------------|------------|-----|-------------------|--------|
| 21:15 | first ≥ start | 65276.78644629988 | 65276.78644629988 | 0 | 0.0 | ~5496 ms | **MATCH** |
| 21:15 | last ≤ start | same tick | same | 0 | 0.0 | ~5496 ms | **MATCH** |
| 21:20 | first ≥ start | 65280.1292639332 | 65280.1292639332 | 0 | 0.0 | ~1592 ms | **MATCH** |
| 21:20 | last ≤ start | same tick | same | 0 | 0.0 | ~1592 ms | **MATCH** |
| 21:25 | first ≥ start | 65286.739965721725 | 65286.739965721725 | 0 | 0.0 | ~2219 ms | **MATCH** |
| 21:25 | last ≤ start | same tick | same | 0 | 0.0 | ~2219 ms | **MATCH** |

Every sampled window had a Chainlink tick with **`source_ts` exactly equal to `event_start`**, so `first_ge` and `last_le` selected the same tick.

**Classification:**

| Rule ID | Class | Rationale |
|---------|-------|-----------|
| Equality to displayed `openPrice` via on-boundary Chainlink tick | **PROVISIONAL** | Consistent exact matches; not an official documented selection algorithm |
| `first_source_ts_ge_event_start` | **PROVISIONAL** | Matches sample; coincides with `last_le` here; old/ also used this |
| `last_source_ts_le_event_start` | **PROVISIONAL** | Not distinguishable in this sample |
| Nearest absolute | **PROVISIONAL** | Same tick in sample; not preferred for live (ambiguity) |

Runtime attestation remains mandatory even after provisional freeze.

Full rows: `var/reporting/n1/window_comparisons.jsonl` (includes `capture_sequence`, `source_ts`, `receive_wall_utc`, `receive_monotonic_ns`, `clock_uncertainty_ms=null`).

---

## 7. Latency summary

From `var/reporting/n1/analysis_summary.json` (bounded capture overlapping the three windows):

| Source | Count | Source gap p50 | Recv−source p50 | Notes |
|--------|------:|----------------|-----------------|-------|
| RTDS Chainlink `btc/usd` | 864 | **1000 ms** | ~1694 ms | Steady ~1 Hz; no OOO markers; recv lag at boundary 1.6–5.5 s |
| Direct Binance Spot `@trade` | 24256 | ~0–89 ms | ~234 ms | High rate; **many negative recv−source** → clock skew / uncertainty OPEN |
| RTDS Binance (capture) | 0 | — | — | Capture used documented `filters:"btcusdt"` which returned **0**; follow-up probe: unfiltered stream carries `btcusdt` |

**Causal basis** (`LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`, n=830 pairs):

| Metric | Value |
|--------|------:|
| Skew p50 (`CL.source − BN.source`) | ~566 ms |
| Skew p95 | ~2577 ms |
| Basis p50 (ln·1e4) | ~−7.1 bps |
| \|basis\| p95 | ~7.5 bps |

**Reconnects:** none observed during the successful capture segment.  
**Late/OOO markers:** 0 on Chainlink path.

**Clock uncertainty:** field present as `null` with explicit OPEN note — N2 must own a clock-sync provider emitting `ClockSyncSnapshot` for TimeAuthority.

---

## 8. Source recommendation

| Role | Recommendation | Status |
|------|----------------|--------|
| Authoritative settlement reference candidate | Polymarket RTDS Chainlink `btc/usd` | **FROZEN for N2 wiring** (attestation still required for N3 lock) |
| Preferred fast trading reference \(S\) | Direct Binance Spot trade stream | **FROZEN for N2** |
| Comparison / fallback | RTDS Binance `crypto_prices` (client-filter `btcusdt`; do not rely on symbol filter alone) | **PROVISIONAL** |
| Displayed PTB attestation | SSR `openPrice` (or future documented crypto PTB API) | Attestation only; **not** hot path |
| Live pairing | `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK` | **FROZEN** (planning + N1 causal samples) |

**Numeric ranges (not frozen as hard thresholds):**

| Parameter | Suggested initial band from sample | Freeze status |
|-----------|-------------------------------------|---------------|
| Max CL/BN source skew | start monitoring around **3–5 s** (p95 ~2.6 s) | **OPEN** — N3 must freeze with larger sample + clock sync |
| Bounded lateness (boundary recv) | observed **~1.6–5.5 s**; legacy 5000 ms still a candidate | **OPEN / provisional** |
| Boundary lag usable for lock | keep ≤ ~5 s as provisional until N3 | **PROVISIONAL** |
| PTB mismatch tolerance | sample diffs were **0 bps**; keep tight attestation (legacy 0.5 bps candidate) | **OPEN** for N3 |
| Prep lead for next window | Gamma slug for `+300s` already resolvable (~100–150 ms); recommend prep **≥30–60 s** before boundary (ops, not measured soak) | **OPEN** for N4 |

---

## 9. Discovery validation

| Check | Result |
|-------|--------|
| Deterministic slug `btc-updown-5m-{epoch}` | **Reliable primary** — exact Gamma hits for current/next windows |
| Exact Gamma lookup | OK with User-Agent; slug epoch == `eventStartTime` |
| Active-event fallback | Allowed only to **discover candidates**; bind only if slug epoch, condition ID, and label map match requested window — never nearest-other-window |
| Start/end timestamps | Slug epoch is trading start; end = start+300s; Gamma `endDate` agrees |
| Condition ID / market ID | Present and stable per window |
| Resolution rules/source | Chainlink stream URL present |
| Token mapping | Label-based Up/Down only |
| Prepared-next | Next-window slug already listed on Gamma before start |

**Rejection cases (synthetic, policy):** missing Up/Down, duplicate Up, unknown label, token-count mismatch, Yes/No instead of Up/Down → reject. Reversed `["Down","Up"]` still maps correctly by label.

**Current code note (not changed in N1):** `market_from_gamma_event` maps Up/Down into `yes`/`no` instrument fields by label index — functionally label-based, but naming remains YES/NO-era; N2/discovery cleanup should expose `UP`/`DOWN` explicitly.

Artifact: `var/reporting/n1/discovery_validation.json`.

---

## 10. `old/` keep / adapt / reject

Inspected read-only under `old/src/tyrex_pm/` (never executed/imported).

| Concept | Old path (representative) | Behavior / evidence | Decision | Target owner | Reason / prohibited |
|---------|---------------------------|---------------------|----------|--------------|---------------------|
| One-window run | `runtime/run_once.py`, `z_gap_run.py` | Bounded one-shot session | **ADAPT** | runtime hosts | Reimplement; no `old/` import |
| Continuous run | `run_continue.py`, `btc_5m_window_scheduler.py` | Next-window wake + prestart | **ADAPT** | N4 host / scheduler | Keep prepared-next idea |
| Deterministic discovery | `ingestion/market_discovery.py`, `btc_5m_*` | Slug epoch construction | **KEEP_CONCEPT** | `adapters/polymarket` discovery | Already mirrored in current `btc_5m_window.py` |
| Next-window preparation | scheduler `prestart_seconds` | Wake before boundary | **ADAPT** | N4 prepared-next | Atomic promote; no strategy eval pre-promote |
| Binance connection | `venue/binance_data/ws_client.py` | Reconnect backoff, combined streams | **ADAPT** | Binance adapter | Direct Spot primary |
| Polymarket RTDS connection | `venue/polymarket_rtds/ws_client.py` | PING 5s, reconnect | **ADAPT** | RTDS adapter | Empty CL filters; client-filter BN |
| Heartbeat | config `heartbeat_*`, RTDS PING | Stall detection | **ADAPT** | adapters + health | |
| Reconnect/backoff | ws clients `reconnect_backoff_s` | Retry with sleep | **KEEP_CONCEPT** | adapters | |
| Resubscription | RTDS subscribe on connect | Resub after reconnect | **KEEP_CONCEPT** | adapters | |
| Clock/time handling | `runtime/time_authority.py`, `z_gap_clock_sanity.py` | Authority interprets sync | **ADAPT** | core TimeAuthority + external clock provider | **No network I/O in core** |
| PTB acquisition | `price_to_beat_tracker.py` (`first >= start`), `z_gap_ptb_capture.py` | Live + log sidecar | **ADAPT** | N3 PTB services | Rule provisional; attestation required |
| Boundary timing | `z_gap_boundary_gate.py` | Gate around event_start | **ADAPT** | N3/N4 | |
| Graceful shutdown | session orchestrator stop events | Cooperative stop | **KEEP_CONCEPT** | hosts | |
| State recovery | lifecycle / preflight dirs | Restart artifacts | **ADAPT** | portfolio/lifecycle hosts | No Z-Gap private stores bypassing framework |
| Order/fill recon concepts | `strategies/z_gap/reconciliation.py`, survival | Recon before retry | **ADAPT** | N6 OMS/recon | Generic, not strategy-owned network |
| Kill/preflight/approval | `z_gap_preflight.py`, `z_gap_live_preflight.py`, interactive approval | Fail-closed live | **ADAPT** | N6/N7 | Auth read-only is N6 completion / N7 prereq |
| Retry/idempotency | config + survival | Must verify venue idempotency | **ADAPT** | N6 | Do not assume |
| Strategy-embedded feed ownership | `signal_feed_runtime.py` coupled paths | Mixed ownership | **REJECT** | — | Strategy receives sealed inputs only |
| Importing old modules | entire `old/` | Historical Path A/B split risk | **REJECT** | — | Hard rule: no `old/` imports |

---

## 11. Architecture mapping

```text
[Adapters — provider I/O only]
  RTDS Chainlink  → SettlementReferenceTick (append-only raw ingress)
  Binance Spot    → ReferenceTick
  RTDS Binance    → optional comparison ticks
  Gamma discovery → MarketBinding (UP/DOWN labels)
  Clock sync provider → ClockSyncSnapshot

[Core]
  TimeAuthority interprets ClockSyncSnapshot (no network)
  PTB lock store / quality axes (N3)
  Causal basis aligner: LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK

[Hosts]
  Prepared-next discover/validate/subscribe → atomic promote
  Continuous EWMA across windows
  OBSERVE/SHADOW sealed path → strategy

[Strategy]
  Sealed inputs only — no Z-Gap-specific network logic
```

Preserve: append-only raw ingress; continuous EWMA; prepared-next with atomic promotion.

---

## 12. Frozen decisions (for N2 / planning)

1. Primary Chainlink source: **Polymarket RTDS** `crypto_prices_chainlink` / `btc/usd` (empty filters + client filter).  
2. Primary Binance source: **direct Binance Spot** trade stream.  
3. RTDS Binance role: **comparison/fallback only**; subscribe unfiltered (or proven filter) and client-filter `btcusdt`.  
4. Outcome mapping: **`"Up"→UP`, `"Down"→DOWN` by label only**.  
5. Causal live policy: **`LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`**.  
6. Late/OOO ingress: **retain append-only**; mark; do not drop silently.  
7. Clock sync: **outside core**; TimeAuthority consumes snapshots only.  
8. Strategy: **sealed inputs only**.  
9. HTML/SSR: **attestation/provenance**, not hot-path K.  
10. Discovery primary: **deterministic slug + exact Gamma**; safe fallback cannot bind wrong window.

---

## 13. Provisional decisions

1. Boundary candidate rule: prefer **`first_source_ts_ge_event_start`** (matches old/ + sample), classification **PROVISIONAL**.  
2. Attestation source: SSR `openPrice` until a documented crypto PTB API appears.  
3. Boundary lag budget ~5 s and mismatch ~0.5 bps remain **provisional candidates**.  
4. Prep lead 30–60 s provisional for N4 engineering.

---

## 14. Remaining blockers

| Blocker | Blocks | Notes |
|---------|--------|-------|
| Crypto PTB structured HTTP **OPEN** | Strong N3 confirmation path | Attestation via SSR OK for engineering; not hot path |
| Boundary inequality undecidable on exact ticks | Canonical N3 rule claim | Need windows without exact-on-boundary tick or official semantics |
| `clock_uncertainty_ms` unmeasured | Tight latency/skew freezes | N2 clock provider |
| RTDS Binance filter empty in main capture | Comparative BN latency via RTDS | N2 should use unfiltered + client filter |
| Numeric skew/lateness not frozen | N3 thresholds | Use larger sample after clock sync |

---

## 15. Exact implications for N2

N2 **should** implement:

- RTDS Chainlink adapter → normalized settlement ticks + raw JSONL evidence  
- Direct Binance Spot adapter (existing patterns OK)  
- Optional RTDS Binance comparison adapter with working subscribe shape  
- Gamma discovery binding with explicit UP/DOWN  
- Clock sync snapshot producer  
- Ports/events for ticks, market binding, clock snapshots  

N2 **must not**:

- Lock live PTB as canonical without attestation path  
- Scrape HTML as primary K  
- Put network I/O in core TimeAuthority  
- Import `old/`  

---

## 16. Exact implications for N3

N3 must:

- Version the provisional boundary rule and keep runtime attestation  
- Define quality × lock × readiness axes using N1 provenance classes  
- Freeze skew / lateness / mismatch only after clock-sync-backed samples  
- Resolve OPEN crypto PTB API if/when documented; until then SSR attestation + RTDS candidate  
- Seek at least one window where `first_ge` ≠ `last_le` or obtain official semantics before claiming PROVEN  

---

## 17. Reproduction instructions

Public, read-only. No credentials. Do not import `old/`.

```text
# From repo root (Python 3.12+)
python tools/n1_audit/capture_sources.py --duration-s 1100 --out var/reporting/n1/raw_capture.jsonl
python tools/n1_audit/poll_displayed_ptb.py --duration-s 1100 --out var/reporting/n1/displayed_ptb.jsonl
python tools/n1_audit/finalize_n1.py
python tools/n1_audit/validate_discovery.py
```

Cover ≥3 consecutive window boundaries (minute marks divisible by 300).  
Offline analysis does not require live UI; PTB attestation uses page SSR `openPrice` extraction (escaped JSON), not HTML label scraping as truth.

Official docs: https://docs.polymarket.com/market-data/websocket/rtds

---

## 18. Files / artifacts produced

**Committed (this initiative):**

- `Docs/implementation/z_gap_production_readiness/n1_acceptance_report.md` (this file)  
- `Docs/implementation/z_gap_production_readiness/n1_source_and_legacy_audit.md` (updated)  
- `Docs/implementation/z_gap_production_readiness/README.md` (updated)  
- `Docs/implementation/z_gap_production_readiness/n1_latency_sample.jsonl` (sanitized small sample)  
- `tools/n1_audit/*` (read-only audit scripts)

**Local only (`var/` gitignored):**

- `var/reporting/n1/raw_capture.jsonl`  
- `var/reporting/n1/displayed_ptb.jsonl`  
- `var/reporting/n1/window_comparisons.jsonl`  
- `var/reporting/n1/analysis_summary.json`  
- `var/reporting/n1/discovery_validation.json`  

---

## 19. Test and git status

- Full offline suite expected green: `python -m pytest tests -q --tb=no` (≥464)  
- No `src/`, product tests, product config, or `.env` changes  
- No orders, wallets, credentials, or `old/` execution  
- Commit theme: `Audit Z-Gap PTB sources, latency, discovery, and legacy reuse`  
- **Do not push; do not start N2 in this milestone**
