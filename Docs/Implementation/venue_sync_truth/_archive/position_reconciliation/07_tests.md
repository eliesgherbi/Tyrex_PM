# Position Reconciliation — Test Plan

## Unit tests: Diff algorithm (Step 3)

### T3.1: Match — no action

```python
def test_diff_match_no_action():
    """venue_qty == cache_qty → empty action list."""
    actor = build_actor(position_reconciliation_enabled=True)
    # Cache: instrument A has 50 shares
    mock_cache_positions(actor, {"A": Decimal("50")})
    # Venue: instrument A has 50 shares
    rows = [{"asset": TOKEN_A, "size": "50"}]
    actions = actor._reconciliation_pass(rows)
    assert actions == []
```

### T3.2: Stale close — full close action

```python
def test_diff_stale_close():
    """venue_qty == 0, cache_qty > 0 → close action with PositionStatusReport."""
    actor = build_actor(position_reconciliation_enabled=True)
    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "0"}]
    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 1
    assert actions[0].diff_direction == "close"
    assert actions[0].venue_qty == Decimal("0")
    assert actions[0].cache_qty == Decimal("50")
    report = actions[0].report
    assert isinstance(report, PositionStatusReport)
    assert report.position_side == PositionSide.FLAT
    assert report.quantity == Quantity(0, PRECISION)
```

### T3.3: Stale partial — partial reduce action

```python
def test_diff_stale_partial():
    """0 < venue_qty < cache_qty → partial_reduce action."""
    actor = build_actor(position_reconciliation_enabled=True)
    mock_cache_positions(actor, {"A": Decimal("100")})
    rows = [{"asset": TOKEN_A, "size": "30"}]
    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 1
    assert actions[0].diff_direction == "partial_reduce"
    assert actions[0].venue_qty == Decimal("30")
    assert actions[0].cache_qty == Decimal("100")
    report = actions[0].report
    assert report.position_side == PositionSide.LONG
    assert report.quantity == Quantity.from_str("30")
```

### T3.4: Venue-has-more — no action (default)

```python
def test_diff_venue_has_more_default_no_action():
    """venue_qty > cache_qty, reconcile_venue_has_more=False → no action."""
    actor = build_actor(
        position_reconciliation_enabled=True,
        reconcile_venue_has_more=False,
    )
    mock_cache_positions(actor, {"A": Decimal("10")})
    rows = [{"asset": TOKEN_A, "size": "50"}]
    actions = actor._reconciliation_pass(rows)
    assert actions == []
```

### T3.5: Venue-has-more — action when enabled

```python
def test_diff_venue_has_more_when_enabled():
    """venue_qty > cache_qty, reconcile_venue_has_more=True → action."""
    actor = build_actor(
        position_reconciliation_enabled=True,
        reconcile_venue_has_more=True,
    )
    mock_cache_positions(actor, {"A": Decimal("10")})
    rows = [{"asset": TOKEN_A, "size": "50"}]
    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 1
    assert actions[0].diff_direction == "venue_has_more"
```

### T3.6: Venue absent, cache has position — treat as close

```python
def test_diff_venue_absent_cache_has():
    """Instrument in cache but not in venue rows → stale close."""
    actor = build_actor(position_reconciliation_enabled=True)
    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = []  # No venue positions
    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 1
    assert actions[0].diff_direction == "close"
```

### T3.7: Multiple instruments — mixed actions

```python
def test_diff_multiple_instruments():
    """Multiple instruments with different diff directions."""
    actor = build_actor(position_reconciliation_enabled=True)
    mock_cache_positions(actor, {
        "A": Decimal("50"),   # Will be closed
        "B": Decimal("100"),  # Will be partially reduced
        "C": Decimal("25"),   # Will match
    })
    rows = [
        {"asset": TOKEN_A, "size": "0"},
        {"asset": TOKEN_B, "size": "60"},
        {"asset": TOKEN_C, "size": "25"},
    ]
    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 2  # A=close, B=partial, C=match (no action)
    dirs = {a.diff_direction for a in actions}
    assert dirs == {"close", "partial_reduce"}
```

### T3.8: Disabled — no actions regardless of diff

```python
def test_reconciliation_disabled_no_actions():
    """position_reconciliation_enabled=False → empty actions."""
    actor = build_actor(position_reconciliation_enabled=False)
    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "0"}]
    actions = actor._reconciliation_pass(rows)
    assert actions == []
```

### T3.9: PositionStatusReport shape validation

```python
def test_position_status_report_shape():
    """Validate all fields of the constructed PositionStatusReport."""
    actor = build_actor(position_reconciliation_enabled=True)
    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "0"}]
    actions = actor._reconciliation_pass(rows)
    report = actions[0].report
    assert report.account_id is not None
    assert report.instrument_id == INSTRUMENT_A
    assert report.position_side == PositionSide.FLAT
    assert report.quantity == Quantity(0, PRECISION)
    assert report.venue_position_id is None  # Netting OMS
    assert report.ts_last > 0
    assert report.ts_init > 0
```

## Unit tests: Race defenses (Step 4)

### T4.1: Race B — Data API lag debounce

```python
def test_race_b_first_detection_deferred():
    """First detection of discrepancy is deferred (lag tolerance)."""
    actor = build_actor(
        position_reconciliation_enabled=True,
        data_api_lag_tolerance_seconds=5.0,
    )
    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "0"}]

    # First cycle: deferred
    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 1
    assert actions[0].deferred is True
    assert actions[0].defer_count == 1
    assert actions[0].report is None
```

### T4.2: Race B — Second detection after tolerance → action

```python
def test_race_b_second_detection_action():
    """Second detection after lag tolerance → action sent."""
    actor = build_actor(
        position_reconciliation_enabled=True,
        data_api_lag_tolerance_seconds=5.0,
    )
    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "0"}]

    # First cycle: deferred
    actor._reconciliation_pass(rows)
    # Simulate time passing > lag tolerance
    advance_time(6.0)
    # Second cycle: action
    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 1
    assert actions[0].deferred is False
    assert actions[0].report is not None
```

### T4.3: Race C — In-flight sell covers delta → deferred

```python
def test_race_c_inflight_sell_defers():
    """In-flight SELL qty >= delta → deferred."""
    actor = build_actor(position_reconciliation_enabled=True)
    mock_cache_positions(actor, {"A": Decimal("50")})
    mock_inflight_orders(actor, {"A": [sell_order(qty=50)]})
    # Simulate already past lag tolerance
    actor._deferred_reconciliations[INSTRUMENT_A] = 1
    rows = [{"asset": TOKEN_A, "size": "0"}]

    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 1
    assert actions[0].deferred is True
```

### T4.4: Race C — Max deferrals reached → proceed

```python
def test_race_c_max_deferrals_proceeds():
    """After max deferrals, proceed with reconciliation."""
    actor = build_actor(
        position_reconciliation_enabled=True,
        position_reconciliation_deferral_max=3,
    )
    mock_cache_positions(actor, {"A": Decimal("50")})
    mock_inflight_orders(actor, {"A": [sell_order(qty=50)]})
    actor._deferred_reconciliations[INSTRUMENT_A] = 3  # At max

    rows = [{"asset": TOKEN_A, "size": "0"}]
    actions = actor._reconciliation_pass(rows)
    assert len(actions) == 1
    assert actions[0].deferred is False
    assert actions[0].report is not None
```

### T4.5: Race E — Recently reconciled TTL skip

```python
def test_race_e_recently_reconciled_skipped():
    """Instrument reconciled within TTL → skipped."""
    actor = build_actor(
        position_reconciliation_enabled=True,
        recently_reconciled_ttl_seconds=60.0,
    )
    mock_cache_positions(actor, {"A": Decimal("50")})
    actor._recently_reconciled[INSTRUMENT_A] = time.monotonic()

    rows = [{"asset": TOKEN_A, "size": "0"}]
    actions = actor._reconciliation_pass(rows)
    # Should be skipped entirely (no action)
    assert actions == [] or all(a.diff_direction == "skipped_ttl" for a in actions)
```

### T4.6: Race F — Concurrent timer skipped

```python
def test_race_f_concurrent_timer_skipped():
    """Timer fires while cycle in progress → skipped."""
    actor = build_actor(position_reconciliation_enabled=True)
    actor._cycle_in_progress = True

    actor.on_timer(mock_event())
    # Verify no new executor task was submitted
    assert executor_submit_count() == 0
```

### T4.7: Stuck deferral count

```python
def test_stuck_deferral_count():
    """stuck_deferral_count correctly counts instruments at max deferrals."""
    actor = build_actor(
        position_reconciliation_enabled=True,
        position_reconciliation_deferral_max=5,
    )
    actor._deferred_reconciliations = {
        INSTRUMENT_A: 5,  # At max
        INSTRUMENT_B: 3,  # Under max
        INSTRUMENT_C: 5,  # At max
    }
    assert actor.stuck_deferral_count == 2
```

## Unit tests: Action application (Step 5)

### T5.1: `_apply_reconciliation_actions` sends to correct endpoint

```python
def test_apply_actions_sends_reports():
    """Each non-deferred action triggers msgbus.send."""
    actor = build_actor(position_reconciliation_enabled=True)
    report = build_position_status_report(INSTRUMENT_A, qty=0)
    action = ReconciliationAction(
        instrument_id=INSTRUMENT_A,
        venue_qty=Decimal("0"),
        cache_qty=Decimal("50"),
        diff_direction="close",
        deferred=False,
        defer_count=0,
        report=report,
    )

    actor._apply_reconciliation_actions([action])

    mock_msgbus.send.assert_called_once_with(
        "ExecEngine.reconcile_execution_report",
        report,
    )
```

### T5.2: `_apply_reconciliation_actions` updates tracking state

```python
def test_apply_actions_updates_state():
    """Action application updates recently_reconciled and reconciliation_count."""
    actor = build_actor(position_reconciliation_enabled=True)
    report = build_position_status_report(INSTRUMENT_A, qty=0)
    action = ReconciliationAction(
        instrument_id=INSTRUMENT_A,
        venue_qty=Decimal("0"),
        cache_qty=Decimal("50"),
        diff_direction="close",
        deferred=False,
        defer_count=0,
        report=report,
    )

    actor._apply_reconciliation_actions([action])

    assert INSTRUMENT_A in actor._recently_reconciled
    assert actor._reconciliation_count == 1
```

### T5.3: Queue draining processes all batches

```python
def test_queue_drain_processes_multiple_batches():
    """Multiple batches in queue are all processed."""
    actor = build_actor(position_reconciliation_enabled=True)
    actor._reconciliation_queue.put([action_a])
    actor._reconciliation_queue.put([action_b])

    actor._drain_reconciliation_queue()

    assert mock_msgbus.send.call_count == 2
```

## Unit tests: Fact emission (Step 6)

### T6.1: Reconciliation fact emitted for action

```python
def test_reconciliation_fact_emitted():
    """position_reconciliation fact emitted with correct keys."""
    fact_collector = []
    actor = build_actor(
        position_reconciliation_enabled=True,
        fact_emit=lambda ft, p: fact_collector.append((ft, p)),
    )

    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "0"}]
    actor._reconciliation_pass(rows)

    facts = [(ft, p) for ft, p in fact_collector if ft == "position_reconciliation"]
    assert len(facts) == 1
    _, payload = facts[0]
    assert payload["instrument_id"] == str(INSTRUMENT_A)
    assert payload["diff_direction"] == "close"
    assert payload["venue_qty"] == "0"
    assert payload["cache_qty"] == "50"
```

### T6.2: Deferred action emits fact with deferred=True

```python
def test_deferred_fact_emitted():
    """Deferred reconciliation emits fact with deferred=True."""
    fact_collector = []
    actor = build_actor(
        position_reconciliation_enabled=True,
        fact_emit=lambda ft, p: fact_collector.append((ft, p)),
    )

    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "0"}]
    actor._reconciliation_pass(rows)  # First detection = deferred

    facts = [(ft, p) for ft, p in fact_collector if ft == "position_reconciliation"]
    assert len(facts) == 1
    assert facts[0][1]["deferred"] is True
    assert facts[0][1]["reconciliation_sent"] is False
```

### T6.3: Fact schema validation

```python
def test_reconciliation_fact_validates():
    """position_reconciliation fact passes facts_v1 validation."""
    from tyrex_pm.reporting.schema.facts_v1 import validate_fact_row

    row = {
        "fact_type": "position_reconciliation",
        "run_id": "test",
        "fact_schema_version": SCHEMA_VERSION,
        "recorded_at_utc": "2026-04-14T12:00:00Z",
        "cycle": 1,
        "instrument_id": "YES-123.POLYMARKET",
        "venue_qty": "0",
        "cache_qty": "50",
        "diff_direction": "close",
        "deferred": False,
        "defer_count": 0,
        "reconciliation_sent": True,
    }
    validate_fact_row(row)  # Should not raise
```

## Unit tests: Health source (Step 7)

### T7.1: Stuck deferral → DEGRADED_OMS

```python
def test_health_stuck_deferral_degraded():
    """stuck_deferral_count > 0 → DEGRADED_OMS."""
    ws = mock_wallet_sync(
        first_sync_complete=True,
        stuck_deferral_count=2,
        consecutive_failure_count=0,
    )
    source = NautilusLiveExecutionHealthSource(engine, wallet_sync_status=ws)
    snap = source.snapshot()
    assert snap.level == TradableStateHealth.DEGRADED_OMS
    assert snap.reason_code == "position_reconciliation_stuck"
```

### T7.2: Stale + stuck deferral → stale wins

```python
def test_health_stale_wins_over_stuck():
    """Stale wallet sync takes priority over stuck deferrals."""
    ws = mock_wallet_sync(
        first_sync_complete=True,
        stuck_deferral_count=2,
        consecutive_failure_count=5,  # Triggers stale
    )
    source = NautilusLiveExecutionHealthSource(engine, wallet_sync_status=ws)
    snap = source.snapshot()
    assert snap.level == TradableStateHealth.DEGRADED_OMS
    assert snap.reason_code == "wallet_sync_stale"
```

### T7.3: No stuck deferrals → HEALTHY

```python
def test_health_no_stuck_healthy():
    """No stuck deferrals, sync complete → HEALTHY."""
    ws = mock_wallet_sync(
        first_sync_complete=True,
        stuck_deferral_count=0,
        consecutive_failure_count=0,
    )
    source = NautilusLiveExecutionHealthSource(engine, wallet_sync_status=ws)
    snap = source.snapshot()
    assert snap.level == TradableStateHealth.HEALTHY
```

## Integration test: Original failing scenario (Step 8)

### T8.1: Full cycle — external close → budget update

```python
def test_integration_external_close_unblocks_cap():
    """
    Simulates: bot opens 3 positions to fill cap → external close drops
    venue qty to 0 → reconciliation synthesizes close → budget drops →
    next risk decision approves.
    """
    # Setup
    actor = build_actor(position_reconciliation_enabled=True)
    engine = mock_live_exec_engine(generate_missing_orders=True)
    budget = NautilusDeploymentBudget(portfolio, cache, exec_reader, {})

    # Phase 1: 3 positions fill the cap
    create_positions(cache, [
        ("A", Decimal("50"), 0.6),   # 30 USD
        ("B", Decimal("40"), 0.5),   # 20 USD
        ("C", Decimal("30"), 0.5),   # 15 USD
    ])
    total, ok, _ = budget.portfolio_deployment_usd()
    assert ok and total == pytest.approx(65.0)  # Over cap

    # Phase 2: External close — venue shows 0 for all
    rows = [
        {"asset": TOKEN_A, "size": "0"},
        {"asset": TOKEN_B, "size": "0"},
        {"asset": TOKEN_C, "size": "0"},
    ]

    # Phase 3: Sync cycle detects discrepancy
    # (First cycle deferred due to lag tolerance, second cycle acts)
    actor._reconciliation_pass(rows)  # Defer
    advance_time(6.0)
    actions = actor._reconciliation_pass(rows)  # Act

    # Phase 4: Apply actions (event-loop thread simulation)
    for action in actions:
        if action.report:
            engine.reconcile_execution_report(action.report)

    # Phase 5: Verify positions closed in cache
    assert len(cache.positions_open(venue=POLYMARKET_VENUE)) == 0

    # Phase 6: Verify budget updated
    total, ok, _ = budget.portfolio_deployment_usd()
    assert ok and total == pytest.approx(0.0)

    # Phase 7: Verify risk approval
    decision = risk_policy.evaluate(buy_signal)
    assert decision.allowed is True
```

## Idempotence tests

### T-idem.1: Same discrepancy across two cycles

```python
def test_idempotence_same_discrepancy():
    """After reconciliation, next cycle sees match (no action)."""
    actor = build_actor(position_reconciliation_enabled=True)
    # Setup: cache has 50, venue has 0
    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "0"}]

    # First cycle: defer
    actor._reconciliation_pass(rows)
    advance_time(6.0)
    # Second cycle: act
    actions = actor._reconciliation_pass(rows)
    assert any(a.report is not None for a in actions)

    # Simulate engine processing: position closed
    mock_cache_positions(actor, {"A": Decimal("0")})

    # Third cycle: no action (match)
    actions = actor._reconciliation_pass(rows)
    assert all(a.report is None for a in actions) or len(actions) == 0
```

### T-idem.2: Stable match across multiple cycles

```python
def test_idempotence_stable_match():
    """Repeated matching state produces no actions."""
    actor = build_actor(position_reconciliation_enabled=True)
    mock_cache_positions(actor, {"A": Decimal("50")})
    rows = [{"asset": TOKEN_A, "size": "50"}]

    for _ in range(5):
        actions = actor._reconciliation_pass(rows)
        assert actions == []
```
