# Phase 3 completion report — Complete submission evidence

```text
phase: 3 — Complete submission evidence
starting_head: 28c4d804f0c2927579c35743bfcedb807330d768
ending_head: 28c4d804f0c2927579c35743bfcedb807330d768 (uncommitted working tree; no commit per operator request)
files_changed:
  - src/tyrex_pm/execution/polymarket/transport.py
  - src/tyrex_pm/execution/polymarket/mutation_transport.py
  - src/tyrex_pm/execution/polymarket/fake_transport.py
  - src/tyrex_pm/execution/polymarket/normalize.py
  - src/tyrex_pm/execution/polymarket/live_oms.py  (Phase 3 integrated; Phase 2 obligations preserved)
  - src/tyrex_pm/execution/order_store.py
  - src/tyrex_pm/portfolio/matched_exposure.py  (new)
  - src/tyrex_pm/portfolio/__init__.py
  - Docs/implementation/event_driven_execution_lifecycle/sdk_amount_semantics.md  (new)
  - Docs/implementation/event_driven_execution_lifecycle/phase_03_completion_report.md  (new)
  - tests/fixtures/execution/*.json  (new sanitized SDK wire shapes)
  - tests/test_exec_lifecycle_response_match.py  (new)
contracts_added_or_changed:
  - SubmitOrderResult: trade_ids, making/taking amounts, cumulative_matched_qty,
    remaining_qty, client_order_id, raw_redacted (+ deprecated raw alias)
  - ResponseMatchEvidence + match_evidence_from_submit_result (normalize.py)
  - OrderStore.matched_quantity_hwm / apply_matched_cumulative (positive delta only)
  - MatchedExposureProjection (lineage-bound; MATCHED ≠ CONFIRMED/sellable)
  - LiveOMS.apply_response_match single-writer path after accept
  - Critical audit mutation.venue_submit_result carries redacted full evidence
tests_added:
  - tests/test_exec_lifecycle_response_match.py (24 tests)
targeted_test_result: 24 passed (test_exec_lifecycle_response_match.py)
related_smoke: test_r6_live_oms_scenarios + test_reporting_live_barrier + test_r7a_architecture → pass
full_suite_result: 857 passed, 3 skipped
venue_mutations_attempted: 0
evidence_artifacts:
  - tests/fixtures/execution/submit_accepted_live.json
  - tests/fixtures/execution/submit_matched_full_buy.json
  - tests/fixtures/execution/submit_matched_partial_buy.json
  - tests/fixtures/execution/submit_matched_sell.json
  - tests/fixtures/execution/submit_delayed.json
  - tests/fixtures/execution/submit_rejected.json
  - Docs/implementation/event_driven_execution_lifecycle/sdk_amount_semantics.md
deviations_from_plan:
  - Coordinated with parallel Phase 2: live_oms.py obligations / lifecycle_store left intact;
    Phase 3 only added match evidence application and audit payload fields.
  - LiveOMS still defaults SubmitOrderRequest.order_type to GTC unless
    command.evidence["order_type"] is set (e.g. FAK). SDK FAK mapping is proven;
    callers must pass evidence.order_type=FAK for marketable one-shot submits.
remaining_blockers:
  - None for D-04 (FAK SDK mapping proven via SecureClient.place_market_order
    default/literal and SdkMutationTransport passthrough test).
  - Phase 4+ still required for stream wiring, exit supervisor, settlement reuse.
gate: PASS
```

## Exit-gate evidence

- Complete submission evidence reaches LiveOMS without SDK-native leakage
  (`test_no_sdk_native_types_leak_from_mapper`, mapper unit tests).
- One match produces one matched-exposure delta; duplicates/decreasing HWM ignored
  with diagnostics.
- MATCHED exposure > 0 while confirmed portfolio qty and sellable proxy remain 0.
- Critical audit lane records redacted submit evidence (no secrets).

## D-04 FAK

**Resolved / not blocking.** `polymarket-client==0.2.0` `SecureClient.place_market_order`
types `order_type: Literal['FAK','FOK'] = 'FAK'`. `SdkMutationTransport` forwards
`FAK`/`FOK` to `place_market_order` and does not substitute GTC.
