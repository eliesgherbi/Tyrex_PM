"""Remaining R1 acceptance fixtures F6, F8–F16, F18–F22."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from support.runtime import bare_runtime

from tyrex_pm.execution.evidence import (
    DispatchAuthorized,
    ExecutionRole,
    ManualInterventionRequired,
    OrderRequested,
    SessionOpened,
    SubmissionAttempted,
    SubmissionFailed,
    SubmissionResponseObserved,
    TradeAttributionCorrected,
    TradeStatus,
    TradeStatusObserved,
    new_envelope,
)
from tyrex_pm.execution.fill_attribution import attribute_owned_fill
from tyrex_pm.execution.orders import LimitOrderSpec, OrderSide, TimeInForce
from tyrex_pm.execution.reducer import reduce_execution_event
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.runtime.capabilities import CapabilityController

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)


def _open() -> ExecutionSessionState:
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(
        state,
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="m1",
            window_id="w1",
            token_id="yes",
        ),
    )
    spec = LimitOrderSpec(
        order_id="entry-1",
        market_id="m1",
        instrument_id="yes",
        token_id="yes",
        side=OrderSide.BUY,
        limit_price=Decimal("0.40"),
        shares=Decimal("5"),
        time_in_force=TimeInForce.GTC,
    )
    state, _ = reduce_execution_event(
        state,
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="req"),
            role=ExecutionRole.ENTRY,
            order=spec,
        ),
    )
    return state


def _authorize(state: ExecutionSessionState) -> ExecutionSessionState:
    state, _ = reduce_execution_event(
        state,
        DispatchAuthorized(
            **new_envelope(session_id="s1", dedupe_key="auth"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            prepared_order_digest="d1",
        ),
    )
    state, _ = reduce_execution_event(
        state,
        SubmissionAttempted(
            **new_envelope(session_id="s1", dedupe_key="att"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
        ),
    )
    return state


def test_f6_duplicate_stream_and_rest_same_clip() -> None:
    state = _authorize(_open())
    state, _ = reduce_execution_event(
        state,
        SubmissionResponseObserved(
            **new_envelope(session_id="s1", dedupe_key="resp"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
            accepted=True,
            venue_order_id="our",
            status="live",
        ),
    )
    stream = TradeStatusObserved(
        session_id="s1",
        event_id="stream",
        observed_at=NOW,
        dedupe_key="stream-t1",
        source="user_stream",
        venue_trade_id="t1",
        venue_order_id="our",
        order_id="entry-1",
        token_id="yes",
        side=OrderSide.BUY,
        shares=Decimal("3"),
        price=Decimal("0.40"),
        status=TradeStatus.CONFIRMED,
    )
    rest = TradeStatusObserved(
        session_id="s1",
        event_id="rest",
        observed_at=NOW,
        dedupe_key="rest-t1",
        source="readonly_rest",
        venue_trade_id="t1",
        venue_order_id="our",
        order_id="entry-1",
        token_id="yes",
        side=OrderSide.BUY,
        shares=Decimal("3"),
        price=Decimal("0.40"),
        status=TradeStatus.CONFIRMED,
    )
    state, _ = reduce_execution_event(state, stream)
    state, effects = reduce_execution_event(state, rest)
    assert len(state.trades) == 1
    assert state.confirmed_position_shares == Decimal("3")
    assert all(effect.reason != "conflicting_trade_identity" for effect in effects)


def test_f8_trade_before_submission_response_attaches() -> None:
    state = _authorize(_open())
    early = TradeStatusObserved(
        session_id="s1",
        event_id="early",
        observed_at=NOW,
        dedupe_key="early-t1",
        source="user_stream",
        venue_trade_id="t1",
        venue_order_id="our",
        order_id="entry-1",
        token_id="yes",
        side=OrderSide.BUY,
        shares=Decimal("5"),
        price=Decimal("0.49"),
        status=TradeStatus.CONFIRMED,
    )
    state, _ = reduce_execution_event(state, early)
    assert state.trades["t1"].shares == Decimal("5")
    state, _ = reduce_execution_event(
        state,
        SubmissionResponseObserved(
            **new_envelope(session_id="s1", dedupe_key="resp"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
            accepted=True,
            venue_order_id="our",
            status="live",
        ),
    )
    assert state.confirmed_position_shares == Decimal("5")
    assert state.phase in {
        ExecutionPhase.POSITION_OPEN,
        ExecutionPhase.POSITION_PENDING_CONFIRMATION,
    }


def test_f9_working_maker_needs_venue_followup() -> None:
    state = _authorize(_open())
    state, _ = reduce_execution_event(
        state,
        SubmissionResponseObserved(
            **new_envelope(session_id="s1", dedupe_key="resp"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
            accepted=True,
            venue_order_id="our",
            status="live",
        ),
    )
    assert state.working_maker_entry
    assert state.confirmed_position_shares == Decimal("0")
    assert state.needs_venue_followup
    assert not state.safe_to_open_entry


def test_f14_ambiguous_post_is_not_proven_no_fill() -> None:
    state = _authorize(_open())
    state, _ = reduce_execution_event(
        state,
        SubmissionFailed(
            **new_envelope(session_id="s1", dedupe_key="fail"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
            error_class="timeout",
            message="cancelled",
            ambiguous=True,
        ),
    )
    assert state.has_unresolved_mutation_attempt
    assert not state.entry_authoritatively_unfilled
    assert state.phase is not ExecutionPhase.COMPLETED_NO_FILL


def test_f22_conflicting_clips_fail_closed() -> None:
    state = _authorize(_open())
    state, _ = reduce_execution_event(
        state,
        SubmissionResponseObserved(
            **new_envelope(session_id="s1", dedupe_key="resp"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
            accepted=True,
            venue_order_id="our",
            status="live",
        ),
    )
    state, _ = reduce_execution_event(
        state,
        TradeStatusObserved(
            session_id="s1",
            event_id="stream",
            observed_at=NOW,
            dedupe_key="stream-t1",
            source="user_stream",
            venue_trade_id="t1",
            venue_order_id="our",
            order_id="entry-1",
            token_id="yes",
            side=OrderSide.BUY,
            shares=Decimal("3"),
            price=Decimal("0.40"),
            status=TradeStatus.CONFIRMED,
        ),
    )
    state.trades["t1"].attribution_digest = "stream-digest"
    state, _ = reduce_execution_event(
        state,
        TradeAttributionCorrected(
            session_id="s1",
            event_id="conflict",
            observed_at=NOW,
            dedupe_key="conflict-t1",
            source="readonly_rest",
            venue_trade_id="t1",
            original_event_id="stream",
            previous_venue_order_id="our",
            previous_side=OrderSide.BUY,
            previous_shares=Decimal("3"),
            previous_price=Decimal("0.40"),
            previous_token_id="yes",
            venue_order_id="our",
            order_id="entry-1",
            token_id="yes",
            side=OrderSide.BUY,
            shares=Decimal("4"),
            price=Decimal("0.41"),
            status=TradeStatus.CONFIRMED,
        ),
    )
    assert state.phase is ExecutionPhase.MANUAL_INTERVENTION
    assert state.last_error == "conflicting_trade_identity"
    assert state.confirmed_position_shares == Decimal("3")


@pytest.mark.asyncio
async def test_f15_same_market_live_maker_inspects_resume_cancel() -> None:
    restored = _authorize(_open())
    restored, _ = reduce_execution_event(
        restored,
        SubmissionResponseObserved(
            **new_envelope(session_id="s1", dedupe_key="resp"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
            accepted=True,
            venue_order_id="our",
            status="live",
        ),
    )
    runtime = bare_runtime()
    runtime.known_dust_residuals = []
    runtime.capabilities = CapabilityController()
    runtime.run_recorder = type("Rec", (), {"record": staticmethod(lambda *_a, **_k: None)})()
    runtime.config = type(
        "Cfg", (), {"risk": type("Risk", (), {"maximum_total_debit": Decimal("5")})()}
    )()
    action = await runtime._inspect_retained_session(restored, bound_market_id="m1")
    assert action == "resume_cancel_then_prove"


def test_f21_same_market_manual_stays_blocked() -> None:
    state = _open()
    state, _ = reduce_execution_event(
        state,
        ManualInterventionRequired(
            **new_envelope(session_id="s1", dedupe_key="man"),
            reason="prior_open_order",
        ),
    )
    assert not state.safe_to_open_entry
    assert state.terminal


def test_f6_attribute_same_clip_twice() -> None:
    first = attribute_owned_fill(
        {
            "taker_order_id": "foreign",
            "side": "BUY",
            "size": "10",
            "price": "0.41",
            "asset_id": "yes",
            "maker_orders": [
                {"order_id": "our", "matched": "3", "price": "0.40", "asset_id": "yes"}
            ],
        },
        {"our"},
        "yes",
        owned_side_by_venue={"our": "BUY"},
    )
    second = attribute_owned_fill(
        {
            "taker_order_id": "foreign",
            "side": "BUY",
            "size": "10",
            "price": "0.41",
            "asset_id": "yes",
            "maker_orders": [
                {"order_id": "our", "matched": "3", "price": "0.40", "asset_id": "yes"}
            ],
        },
        {"our"},
        "yes",
        owned_side_by_venue={"our": "BUY"},
    )
    assert first.clip == second.clip
