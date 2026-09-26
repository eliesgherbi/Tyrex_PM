"""S01–S14, S26 and S28 classification and durable clearance."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.execution.coordinator import AccountExecutionCoordinator, DispatchBlocked
from tyrex_pm.execution.evidence import (
    ExecutionRole,
    OrderAttemptResolved,
    SessionOperationalClearanceRecorded,
    SubmissionAttempted,
    SubmissionFailed,
    TradeStatus,
    new_envelope,
)
from tyrex_pm.execution.orders import MarketSellOrderSpec
from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
    OperationalDisposition,
    OrderExecutionState,
    SessionIdentity,
    TradeRecord,
)
from tyrex_pm.execution.startup_reconciliation import (
    AccountIdentity,
    ActivityView,
    Holding,
    InspectionFacts,
    MarketLifecycleObservation,
    OpenOrderView,
    OrderLookup,
    ReadCoverage,
    assess,
)
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal, SqliteExecutionJournal
from tyrex_pm.runtime.startup_controller import StartupController

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _coverage(**overrides) -> ReadCoverage:
    values = dict(
        complete=True,
        positions_complete=True,
        orders_complete=True,
        activity_complete=True,
    )
    values.update(overrides)
    return ReadCoverage(**values)


def _identity() -> AccountIdentity:
    return AccountIdentity(chain_id=137, environment="production", wallet="0xabc")


def _session(
    *,
    session_id: str = "s1",
    market_id: str = "cond",
    token_id: str = "token",
    shares: Decimal = Decimal("0"),
    phase: ExecutionPhase = ExecutionPhase.MANUAL_INTERVENTION,
    buy: Decimal = Decimal("0"),
) -> ExecutionSessionState:
    state = ExecutionSessionState(
        identity=SessionIdentity(
            session_id=session_id,
            strategy_id="ask70",
            market_id=market_id,
            window_id="w",
            token_id=token_id,
        ),
        phase=phase,
        confirmed_position_shares=shares,
    )
    if buy > 0:
        state.trades["buy-1"] = TradeRecord(
            venue_trade_id="buy-1",
            venue_order_id="venue-buy",
            local_order_id="local-buy",
            token_id=token_id,
            side="BUY",
            shares=buy,
            price=Decimal("0.50"),
            status=TradeStatus.CONFIRMED,
        )
    return state


def _facts(session: ExecutionSessionState | None = None, **overrides) -> InspectionFacts:
    sessions = () if session is None else (session,)
    values = dict(
        identity=_identity(),
        coverage=_coverage(),
        sessions=sessions,
        bound_wallet="0xabc",
    )
    values.update(overrides)
    return InspectionFacts(**values)


def test_s01_clean_account_is_ready() -> None:
    assessment = assess(_facts())
    assert assessment.status.value == "READY"
    assert assessment.proposals == ()


def test_s03_zero_balance_without_disposal_stays_blocked() -> None:
    state = _session(shares=Decimal("7"), buy=Decimal("7"))
    assessment = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
        )
    )
    assert assessment.status.value == "BLOCKED"
    assert any(issue.code == "BALANCE_LAGS_ENTRY" for issue in assessment.issues)


def test_s02_unique_external_sale_is_auto_evidence() -> None:
    state = _session(shares=Decimal("7"), buy=Decimal("7"))
    assessment = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
            activities=(
                ActivityView(
                    activity_id="act-1",
                    condition_id="cond",
                    token_id="token",
                    shares=Decimal("7"),
                    cause="sale",
                ),
            ),
        )
    )
    assert any(issue.code == "EXTERNAL_DISPOSAL" for issue in assessment.issues)
    assert assessment.status.value == "READY"


def test_s06_closed_without_payout_proof_blocks() -> None:
    state = _session(shares=Decimal("4"), buy=Decimal("4"))
    assessment = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("4")),),
            lifecycles={
                "cond": MarketLifecycleObservation(
                    condition_id="cond",
                    status="CLOSED_PENDING_RESOLUTION",
                    reason="end time passed is not finality",
                )
            },
        )
    )
    assert assessment.status.value == "BLOCKED"
    assert any(issue.code == "AWAITING_RESOLUTION" for issue in assessment.issues)


def test_s08_redeemable_without_supported_proof_does_not_settle() -> None:
    state = _session(shares=Decimal("4"), buy=Decimal("4"))
    assessment = assess(
        _facts(
            state,
            holdings=(
                Holding(
                    condition_id="cond",
                    token_id="token",
                    shares=Decimal("4"),
                    redeemable=True,
                ),
            ),
            lifecycles={
                "cond": MarketLifecycleObservation(
                    condition_id="cond",
                    status="UNKNOWN",
                    proof_supported=False,
                    reason="indexer redeemable is not finality",
                )
            },
        )
    )
    assert assessment.status.value == "BLOCKED"
    assert not any(issue.code == "SETTLEMENT_HOLDING" for issue in assessment.issues)


def test_s07_supported_payout_is_settlement_not_cash() -> None:
    state = _session(shares=Decimal("4"), buy=Decimal("4"))
    assessment = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("4")),),
            collateral=Decimal("10"),
            lifecycles={
                "cond": MarketLifecycleObservation(
                    condition_id="cond",
                    status="RESOLVED_FINAL",
                    payout_per_share=Decimal("1"),
                    proof_supported=True,
                    reason="supported_finality",
                )
            },
        )
    )
    settlement = next(issue for issue in assessment.issues if issue.code == "SETTLEMENT_HOLDING")
    assert settlement.evidence[0].fields["payout_per_share"] == "1"
    assert assessment.proposals == ()


def test_s09_external_inventory_is_shown_and_not_adopted() -> None:
    assessment = assess(
        _facts(
            holdings=(Holding(condition_id="other", token_id="foreign", shares=Decimal("3")),),
            open_orders=(
                OpenOrderView(
                    venue_order_id="foreign-order",
                    condition_id="other",
                    token_id="foreign",
                    side="BUY",
                ),
            ),
        )
    )
    assert assessment.status.value == "BLOCKED"
    assert {issue.code for issue in assessment.issues} >= {"EXTERNAL_INVENTORY", "EXTERNAL_ORDER"}
    assert assessment.proposals == ()


def test_s10_truncated_pagination_blocks() -> None:
    assessment = assess(
        _facts(
            coverage=_coverage(
                complete=False,
                positions_complete=False,
                reason="pagination_truncated",
            )
        )
    )
    assert assessment.status.value == "BLOCKED"
    assert assessment.issues[0].code == "ACCOUNT_VISIBILITY_INCOMPLETE"


def test_s11_wallet_mismatch_and_legacy_binding_do_not_clear() -> None:
    mismatch = assess(_facts(bound_wallet="0xother"))
    assert any(issue.code == "WALLET_MISMATCH" for issue in mismatch.issues)
    legacy = assess(_facts(_session(shares=Decimal("1")), legacy_unbound=True))
    assert any(issue.code == "LEGACY_JOURNAL_UNBOUND" for issue in legacy.issues)
    assert legacy.status.value == "BLOCKED"


def test_s12_two_sessions_do_not_share_one_disposal() -> None:
    first = _session(session_id="a", shares=Decimal("7"), buy=Decimal("7"))
    second = _session(session_id="b", shares=Decimal("7"), buy=Decimal("7"))
    assessment = assess(
        _facts(
            sessions=(first, second),
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("7")),),
            activities=(
                ActivityView(
                    activity_id="act",
                    condition_id="cond",
                    token_id="token",
                    shares=Decimal("7"),
                    cause="sale",
                ),
            ),
        )
    )
    assert any(issue.code == "AMBIGUOUS_ALLOCATION" for issue in assessment.issues)


def test_s04_terminal_no_fill_lookup_is_auto_and_partial_is_not() -> None:
    state = _session()
    state.orders["local"] = OrderExecutionState(
        role=ExecutionRole.ENTRY,
        spec=MarketSellOrderSpec(
            order_id="local",
            market_id="cond",
            instrument_id="token",
            token_id="token",
            shares=Decimal("1"),
            minimum_price=Decimal("0.01"),
        ),
        attempt_ids=["attempt-1"],
        venue_order_id="venue-1",
        ambiguous=True,
    )
    cleared = assess(
        _facts(
            state,
            order_lookups={
                "venue-1": OrderLookup(
                    found=True,
                    terminal=True,
                    matched_shares=Decimal("0"),
                    status="cancelled",
                )
            },
        )
    )
    assert any(issue.code == "ATTEMPT_RESOLVED_NO_FILL" for issue in cleared.issues)
    missing = assess(
        _facts(
            state,
            order_lookups={
                "venue-1": OrderLookup(
                    found=False,
                    terminal=False,
                    matched_shares=Decimal("0"),
                    status="404",
                    not_found=True,
                )
            },
        )
    )
    assert any(issue.code == "UNRESOLVED_ATTEMPT" for issue in missing.issues)


def test_s26_partial_external_sale_caps_the_proposal() -> None:
    state = _session(shares=Decimal("10"), buy=Decimal("10"))
    assessment = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("4")),),
            activities=(
                ActivityView(
                    activity_id="partial",
                    condition_id="cond",
                    token_id="token",
                    shares=Decimal("6"),
                    cause="sale",
                ),
            ),
            lifecycles={
                "cond": MarketLifecycleObservation(condition_id="cond", status="TRADABLE")
            },
        )
    )
    assert assessment.proposals
    assert assessment.proposals[0].max_shares == Decimal("4")


def test_s28_final_market_with_unresolved_attempt_blocks() -> None:
    state = _session(shares=Decimal("4"), buy=Decimal("4"))
    state.orders["local"] = OrderExecutionState(
        role=ExecutionRole.ENTRY,
        spec=MarketSellOrderSpec(
            order_id="local",
            market_id="cond",
            instrument_id="token",
            token_id="token",
            shares=Decimal("1"),
            minimum_price=Decimal("0.01"),
        ),
        attempt_ids=["attempt-1"],
        ambiguous=True,
    )
    assessment = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("4")),),
            lifecycles={
                "cond": MarketLifecycleObservation(
                    condition_id="cond",
                    status="RESOLVED_FINAL",
                    payout_per_share=Decimal("0"),
                    proof_supported=True,
                    reason="supported_finality",
                )
            },
        )
    )
    assert assessment.status.value == "BLOCKED"
    assert any(issue.code == "UNRESOLVED_ATTEMPT" for issue in assessment.issues)


@pytest.mark.asyncio
async def test_s02_persisted_adjustment_is_idempotent(tmp_path) -> None:
    state = _session(shares=Decimal("7"), buy=Decimal("7"))
    journal = MemoryExecutionJournal()
    async def _allow(_spec):  # noqa: ANN001
        return True, None

    coordinator = AccountExecutionCoordinator(
        journal=journal,
        gateway=_PostGate(),
        final_gate=_allow,
    )
    coordinator.states["s1"] = state
    from tyrex_pm.execution.evidence import SessionOpened

    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    controller = StartupController(coordinator=coordinator, now=lambda: NOW, generation="gen")
    facts = _facts(
        state,
        holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
        activities=(
            ActivityView(
                activity_id="act-1",
                condition_id="cond",
                token_id="token",
                shares=Decimal("7"),
                cause="sale",
            ),
        ),
    )
    first = await controller.inspect(facts)
    assert first.status.value == "READY"
    assert state.external_reduction_shares == Decimal("7")
    assert state.operational_disposition is OperationalDisposition.CLEARED_FLAT
    again = await controller.inspect(facts)
    assert again.status.value == "READY"
    assert state.external_reduction_shares == Decimal("7")
    sqlite = SqliteExecutionJournal(tmp_path / "execution.sqlite3")
    for event in journal.load("s1"):
        sqlite.append(event)
    restored = ExecutionSessionState()
    for event in sqlite.load("s1"):
        from tyrex_pm.execution.reducer import reduce_execution_event

        restored, _ = reduce_execution_event(restored, event)
    assert restored.external_reduction_shares == Decimal("7")
    assert restored.operational_disposition is OperationalDisposition.CLEARED_FLAT
    sqlite.close()
    await coordinator.close()


@pytest.mark.asyncio
async def test_s13_old_authorization_does_not_permit_a_new_process() -> None:
    journal = MemoryExecutionJournal()

    async def _allow(_spec):  # noqa: ANN001
        return True, None

    coordinator = AccountExecutionCoordinator(
        journal=journal, gateway=_PostGate(), final_gate=_allow
    )
    from tyrex_pm.execution.evidence import (
        ManualInterventionRequired,
        ReconciliationObserved,
        SessionOpened,
    )

    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    await coordinator.apply(
        ReconciliationObserved(
            **new_envelope(session_id="s1", dedupe_key="pos"),
            source="test",
            complete=True,
            confirmed_position_shares=Decimal("7"),
            sellable_shares=Decimal("7"),
            open_order_ids=(),
        )
    )
    await coordinator.apply(
        ManualInterventionRequired(
            **new_envelope(session_id="s1", dedupe_key="manual"),
            reason="restored",
        )
    )
    controller = StartupController(coordinator=coordinator, now=lambda: NOW, generation="old")
    from tyrex_pm.execution.startup_reconciliation import RecoveryProposal

    proposal = RecoveryProposal(
        issue_id="close:s1",
        session_id="s1",
        condition_id="cond",
        token_id="token",
        action="cancel_and_close",
        max_shares=Decimal("7"),
        min_price=Decimal("0.01"),
    )
    await controller.authorize(proposal, approved=True, live=True)
    restarted = AccountExecutionCoordinator(
        journal=journal,
        gateway=_PostGate(),
        final_gate=_allow,
    )
    restarted.recovery_generation = "new"
    restored = await restarted.restore("s1")
    await restarted.start()
    with pytest.raises(DispatchBlocked, match="generation_mismatch"):
        await restarted.submit(
            session_id="s1",
            role=ExecutionRole.EXIT,
            spec=MarketSellOrderSpec(
                order_id="exit-1",
                market_id="cond",
                instrument_id="token",
                token_id="token",
                shares=Decimal("7"),
                minimum_price=Decimal("0.01"),
            ),
            recovery=True,
        )
    assert restarted.gateway.posts == []
    assert restored.recovery_authorization is not None
    await coordinator.close()
    await restarted.close()


@pytest.mark.asyncio
async def test_s14_later_exposure_revokes_clearance() -> None:
    state = _session()
    state.applied_event_ids.add("proof")
    from tyrex_pm.execution.evidence import ReconciliationObserved
    from tyrex_pm.execution.reducer import reduce_execution_event

    state, _ = reduce_execution_event(
        state,
        SessionOperationalClearanceRecorded(
            **new_envelope(session_id="s1", dedupe_key="clear"),
            disposition="CLEARED_FLAT",
            proof_event_ids=("proof",),
        ),
    )
    assert state.safe_to_open_entry
    state, _ = reduce_execution_event(
        state,
        ReconciliationObserved(
            **new_envelope(session_id="s1", dedupe_key="later"),
            source="test",
            complete=True,
            confirmed_position_shares=Decimal("2"),
            sellable_shares=Decimal("2"),
            open_order_ids=(),
        ),
    )
    assert state.operational_disposition is OperationalDisposition.UNRESOLVED
    assert state.safe_to_open_entry is False


def test_reducer_rejects_clearance_without_proof() -> None:
    from tyrex_pm.execution.reducer import ReducerInvariantError, reduce_execution_event

    with pytest.raises(ReducerInvariantError, match="proof"):
        reduce_execution_event(
            _session(),
            SessionOperationalClearanceRecorded(
                **new_envelope(session_id="s1", dedupe_key="bare"),
                disposition="CLEARED_FLAT",
                proof_event_ids=(),
            ),
        )


def test_s05_named_attempt_resolution_round_trips() -> None:
    state = _session()
    state.orders["local"] = OrderExecutionState(
        role=ExecutionRole.ENTRY,
        spec=MarketSellOrderSpec(
            order_id="local",
            market_id="cond",
            instrument_id="token",
            token_id="token",
            shares=Decimal("1"),
            minimum_price=Decimal("0.01"),
        ),
        attempt_ids=["attempt-1"],
        ambiguous=True,
        accepted=None,
    )
    from tyrex_pm.execution.evidence import evidence_from_dict, evidence_to_dict
    from tyrex_pm.execution.reducer import reduce_execution_event

    event = OrderAttemptResolved(
        **new_envelope(session_id="s1", dedupe_key="resolved"),
        order_id="local",
        attempt_id="attempt-1",
        venue_order_id="venue-1",
        terminal_status="cancelled",
        cumulative_matched_shares=Decimal("0"),
        proof_source="exact_order",
    )
    restored_event = evidence_from_dict(evidence_to_dict(event))
    updated, _ = reduce_execution_event(state, restored_event)
    assert updated.orders["local"].ambiguous is False
    assert "attempt-1" in updated.resolved_attempt_ids
    failed = SubmissionFailed(
        **new_envelope(session_id="s1", dedupe_key="fail"),
        role=ExecutionRole.ENTRY,
        order_id="local",
        attempt_id="attempt-1",
        error_class="transport",
        message="lost",
        ambiguous=True,
    )
    assert isinstance(failed, SubmissionFailed)
    assert isinstance(SubmissionAttempted, type)


class _PostGate:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def prepare_order(self, spec):  # noqa: ANN001
        from tyrex_pm.execution.coordinator import PreparedOrder

        return PreparedOrder(spec.order_id, spec.order_id, spec)

    async def post_order(self, prepared):  # noqa: ANN001
        self.posts.append(prepared.payload.side.value)
        raise AssertionError("stale approval must not POST")

    async def discard_prepared(self, prepared):  # noqa: ANN001
        del prepared

    async def close(self) -> None:
        return None


def _sale(**overrides) -> ActivityView:
    values = dict(
        activity_id="act-1",
        condition_id="cond",
        token_id="token",
        shares=Decimal("7"),
        cause="sale",
        side="SELL",
    )
    values.update(overrides)
    return ActivityView(**values)


async def _controller_for(state: ExecutionSessionState):
    journal = MemoryExecutionJournal()

    async def _allow(_spec):  # noqa: ANN001
        return True, None

    coordinator = AccountExecutionCoordinator(
        journal=journal, gateway=_PostGate(), final_gate=_allow
    )
    coordinator.states[state.session_id or "s1"] = state
    from tyrex_pm.execution.evidence import SessionOpened

    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id=state.session_id or "s1", dedupe_key="open"),
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    return StartupController(coordinator=coordinator, now=lambda: NOW, generation="gen"), journal


@pytest.mark.asyncio
async def test_blocked_identity_does_not_persist_clearance() -> None:
    state = _session(shares=Decimal("7"), buy=Decimal("7"))
    controller, journal = await _controller_for(state)
    facts = _facts(
        state,
        bound_wallet="0xother",
        holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
        activities=(_sale(),),
    )
    assessment = await controller.inspect(facts)
    assert assessment.status.value == "BLOCKED"
    assert state.confirmed_position_shares == Decimal("7")
    assert state.external_reduction_shares == Decimal("0")
    assert state.operational_disposition is OperationalDisposition.UNRESOLVED
    names = [type(event).__name__ for event in journal._events]
    assert "ExternalPositionAdjustmentObserved" not in names
    assert not any(
        type(event).__name__ == "SessionOperationalClearanceRecorded" for event in journal._events
    )
    incomplete = _facts(
        state,
        coverage=_coverage(complete=False, reason="truncated"),
        holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
        activities=(_sale(),),
    )
    again = await controller.inspect(incomplete)
    assert again.status.value == "BLOCKED"
    assert state.confirmed_position_shares == Decimal("7")
    await controller.coordinator.close()


@pytest.mark.asyncio
async def test_two_sessions_keep_separate_clearance_proofs(tmp_path) -> None:
    from tyrex_pm.execution.reducer import ReducerInvariantError, reduce_execution_event

    left = _session(session_id="left", shares=Decimal("7"), buy=Decimal("7"))
    right = _session(
        session_id="right",
        market_id="cond-b",
        token_id="token-b",
        shares=Decimal("4"),
        buy=Decimal("4"),
    )
    journal = MemoryExecutionJournal()

    async def _allow(_spec):  # noqa: ANN001
        return True, None

    coordinator = AccountExecutionCoordinator(
        journal=journal, gateway=_PostGate(), final_gate=_allow
    )
    from tyrex_pm.execution.evidence import SessionOpened

    for state, market, token in (
        (left, "cond", "token"),
        (right, "cond-b", "token-b"),
    ):
        coordinator.states[state.session_id or ""] = state
        await coordinator.apply(
            SessionOpened(
                **new_envelope(
                    session_id=state.session_id or "",
                    dedupe_key=f"open:{state.session_id}",
                ),
                strategy_id="ask70",
                market_id=market,
                window_id="w",
                token_id=token,
            )
        )
    controller = StartupController(coordinator=coordinator, now=lambda: NOW, generation="gen")
    facts = _facts(
        holdings=(
            Holding(condition_id="cond", token_id="token", shares=Decimal("0")),
            Holding(condition_id="cond-b", token_id="token-b", shares=Decimal("0")),
        ),
        activities=(
            _sale(),
            _sale(
                activity_id="act-2",
                condition_id="cond-b",
                token_id="token-b",
                shares=Decimal("4"),
            ),
        ),
        sessions=(left, right),
    )
    assessment = await controller.inspect(facts)
    assert assessment.status.value == "READY"
    sqlite = SqliteExecutionJournal(tmp_path / "execution.sqlite3")
    for event in journal._events:
        sqlite.append(event)
    for session_id in ("left", "right"):
        restored = ExecutionSessionState()
        for event in sqlite.load(session_id):
            restored, _ = reduce_execution_event(restored, event)
        assert restored.operational_disposition is OperationalDisposition.CLEARED_FLAT
    bad = SessionOperationalClearanceRecorded(
        **new_envelope(session_id="left", dedupe_key="bad-clearance"),
        disposition="CLEARED_FLAT",
        proof_event_ids=("missing-proof",),
    )
    with pytest.raises(ReducerInvariantError, match="clearance missing proof events"):
        await coordinator.apply(bad)
    assert not any(event.event_id == bad.event_id for event in journal._events)
    replayed = ExecutionSessionState()
    for event in journal.load("left"):
        replayed, _ = reduce_execution_event(replayed, event)
    assert replayed.operational_disposition is OperationalDisposition.CLEARED_FLAT
    sqlite.close()
    await coordinator.close()


def test_multiple_sells_clear_and_buys_do_not() -> None:
    state = _session(shares=Decimal("7"), buy=Decimal("7"))
    bought = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
            activities=(_sale(side="BUY", cause="buy", activity_id="buy-act"),),
        )
    )
    assert bought.status.value == "BLOCKED"
    assert any(issue.code == "BALANCE_LAGS_ENTRY" for issue in bought.issues)
    several = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
            activities=(
                _sale(activity_id="a", shares=Decimal("3")),
                _sale(activity_id="b", shares=Decimal("4")),
            ),
        )
    )
    assert several.status.value == "READY"
    assert sum(issue.code == "EXTERNAL_DISPOSAL" for issue in several.issues) == 2


def test_duplicate_owned_fill_is_not_subtracted_twice() -> None:
    state = _session(shares=Decimal("3"), buy=Decimal("7"))
    state.external_activity_ids.add("act-1")
    state.external_reduction_shares = Decimal("4")
    state.trades["owned-fill"] = TradeRecord(
        venue_trade_id="owned-fill",
        venue_order_id="venue",
        local_order_id="local",
        token_id="token",
        side="SELL",
        shares=Decimal("4"),
        price=Decimal("0.4"),
        status=TradeStatus.CONFIRMED,
    )
    assessment = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("3")),),
            activities=(
                _sale(venue_trade_id="owned-fill"),
                _sale(activity_id="act-1", shares=Decimal("4")),
            ),
        )
    )
    assert state.external_reduction_shares == Decimal("4")
    assert any(issue.code == "OWNED_POSITION" for issue in assessment.issues)
    assert assessment.proposals[0].max_shares == Decimal("3")


def test_stale_zero_and_reappeared_holding_are_not_ready() -> None:
    stale = _session(shares=Decimal("0"), phase=ExecutionPhase.COMPLETED_FLAT)
    stale_assessment = assess(
        _facts(
            stale,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("7")),),
        )
    )
    assert stale_assessment.status.value == "BLOCKED"
    assert any(issue.code == "HOLDINGS_REAPPEARED" for issue in stale_assessment.issues)
    increased = _session(shares=Decimal("7"), buy=Decimal("7"))
    increased_assessment = assess(
        _facts(
            increased,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("10")),),
        )
    )
    assert increased_assessment.status.value == "BLOCKED"
    assert any(issue.code == "UNEXPECTED_HOLDING" for issue in increased_assessment.issues)


def test_dust_uses_market_bid_not_the_execution_floor() -> None:
    state = _session(shares=Decimal("2"))
    tiny_floor = assess(
        _facts(
            state,
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("2")),),
            token_bids={"token": Decimal("0.40")},
            token_min_order_sizes={"token": Decimal("5")},
            minimum_exit_price=Decimal("0.01"),
            dust_max_notional=Decimal("0.05"),
        )
    )
    assert tiny_floor.status.value != "READY"
    assert not any(issue.code == "CLASSIFIED_DUST" for issue in tiny_floor.issues)
    assert any(issue.code == "OWNED_POSITION" for issue in tiny_floor.issues)
