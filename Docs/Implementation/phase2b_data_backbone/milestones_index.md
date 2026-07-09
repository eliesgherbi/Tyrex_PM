# Phase 2B Milestones Index

**Program:** Event-driven data backbone and replay lab  
**Authority:** [`tyrex_pm_phase2b_plan.md`](../Phase%202%20completion/tyrex_pm_phase2b_plan.md)  
**Guardrails:** [`spec_00_program_guardrails.md`](spec_00_program_guardrails.md)  
**Last updated:** M2B.3-A RTDS fix accepted with follow-up; M2B.4 spec revised — **awaiting review before implementation**

> **Note:** M2B.1-B short discovery smoke accepted; 24h unattended recorder validation running / pending. M2B.3 normalizer accepted pending full-day artifact. **M2B.3-A** RTDS Chainlink + PTB recording verified on `date=2026-07-05`. **M2B.4** spec updated; implementation **not authorized** until reviewer sign-off.

### M2B.3 status

Implementation complete; full-day normalization acceptance pending 24h recording artifact.

### M2B.3-A (data completeness before M2B.4)

Extends PM market-channel events + RTDS Chainlink reference prices + price-to-beat derivation. See [`polymarket_data_coverage.md`](polymarket_data_coverage.md). **Accepted with follow-up** — RTDS subscription fix verified; 26/27 PTB on rich recording `2026-07-05`.

### M2B.4-B (exploratory & tutorial layer)

Adds Layer 2 exploratory traces (`*_exploratory_trace.json/.md`), tutorial notebook structure, EX1–EX3 notebooks, `plot_market_story`, assumed-latency sensitivity tables. **Awaiting acceptance.**

**Last updated:** M2B.4-B exploratory layer implemented; awaiting reviewer acceptance.

---

## Status legend

| Status | Meaning |
|--------|---------|
| Planned / specification drafted | Spec written; implementation not started |
| In progress | Implementation agent actively working |
| Accepted | Tests + acceptance criteria met |
| Accepted with follow-up | Accepted; minor follow-up tracked |
| Smoke accepted; 24h ops acceptance running / pending | Implementation complete; long-run validation in progress |
| Implementation complete; full-day acceptance pending | Code complete; awaiting 24h artifact |

| Implementation allowed? | Meaning |
|-------------------------|---------|
| No | Do not implement until user explicitly authorizes |
| Yes | User authorized; spec is current |

---

## Milestone table

| Milestone | Spec file | Status | Depends on | Acceptance gate | Implementation allowed? |
|---|---|---|---|---|---|
| **M2B.0-A** | [spec_m2b_0_a_event_contract.md](spec_m2b_0_a_event_contract.md) | Accepted | — | Sequencer + serialization tests green | No (complete) |
| **M2B.0-B** | [spec_m2b_0_b_ws_ingest_event_backbone.md](spec_m2b_0_b_ws_ingest_event_backbone.md) | Accepted with follow-up | M2B.0-A | Fixture projection equivalence | No (complete) |
| **M2B.0-C** | [spec_m2b_0_c_fact_event_correlation.md](spec_m2b_0_c_fact_event_correlation.md) | Accepted with follow-up | M2B.0-B | Optional correlation fields | No (complete) |
| **M2B.1-A** | [spec_m2b_1_a_recorder_mvp.md](spec_m2b_1_a_recorder_mvp.md) | Accepted with follow-up | M2B.0-B | One-market record smoke | No (complete) |
| **M2B.1-B** | [spec_m2b_1_b_full_recorder_gate.md](spec_m2b_1_b_full_recorder_gate.md) | Smoke accepted; 24h ops acceptance running / pending | M2B.1-A | 24h ≥95% coverage | No (implementation complete) |
| **M2B.2** | [spec_m2b_2_external_btc_feed.md](spec_m2b_2_external_btc_feed.md) | Accepted with follow-up | M2B.1-B | Joint PM+BTC smoke | No (complete) |
| **M2B.3** | [spec_m2b_3_normalizer_data_lake.md](spec_m2b_3_normalizer_data_lake.md) | Implementation complete; full-day acceptance pending | M2B.1-B | Parquet tables + golden | No (code complete) |
| **M2B.3-A** | [polymarket_data_coverage.md](polymarket_data_coverage.md) | Accepted with follow-up | M2B.3, M2B.2 | Extended events + RTDS + PTB | No (complete) |
| **M2B.4** | [spec_m2b_4_offline_lab.md](spec_m2b_4_offline_lab.md) | Accepted — strict offline lab | M2B.3-A | Notebooks 01–06 + strict artifacts | No |
| **M2B.4-B** | [spec_m2b_4_offline_lab.md](spec_m2b_4_offline_lab.md) § M2B.4-B | Implementation complete — **awaiting acceptance** | M2B.4 | Exploratory traces + EX1–EX3 + tutorial layer | No |
| **M2B.5** | [spec_m2b_5_replay_engine.md](spec_m2b_5_replay_engine.md) | Planned / specification drafted | M2B.0-C, M2B.3 | Golden replay-diff | No |
| **M2B.6–M2B.9, Phase 3** | — | Future | — | — | No |

See [`DATA_LAKE.md`](../DATA_LAKE.md) and [`polymarket_data_coverage.md`](polymarket_data_coverage.md).
