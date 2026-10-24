"""R1 fill identity, attribution correction, and scoped prior-session clearance."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from support.runtime import bare_runtime

from tyrex_pm.execution.account_state import AccountStateAuthority, AccountStatePolicy
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
from tyrex_pm.execution.fill_attribution import attribute_owned_fill, is_known_normalization_defect
from tyrex_pm.execution.orders import LimitOrderSpec, OrderSide, TimeInForce
from tyrex_pm.execution.reducer import reduce_execution_event
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.trading_runtime import TradingRuntime

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def test_f1_maker_buy_uses_owned_clip() -> None:
    result = attribute_owned_fill(
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
    assert result.reason == "maker_clip"
    assert result.clip is not None
    assert result.clip.venue_order_id == "our"
    assert result.clip.side == "BUY"
    assert result.clip.shares == Decimal("3")
    assert result.clip.price == Decimal("0.40")


def test_f2_maker_sell_uses_our_order_side() -> None:
    result = attribute_owned_fill(
        {
            "taker_order_id": "foreign",
            "side": "BUY",
            "size": "10",
            "price": "0.41",
            "asset_id": "yes",
            "maker_orders": [
                {"order_id": "our-sell", "matched": "3", "price": "0.40", "asset_id": "yes"}
            ],
        },
        {"our-sell"},
        "yes",
        owned_side_by_venue={"our-sell": "SELL"},
    )
    assert result.clip is not None
    assert result.clip.side == "SELL"
    assert result.clip.shares == Decimal("3")


def test_f3_taker_uses_trade_fields() -> None:
    result = attribute_owned_fill(
        {"taker_order_id": "our", "side": "BUY", "size": "5", "price": "0.49", "asset_id": "yes"},
        {"our"},
        "yes",
    )
    assert result.reason == "taker"
    assert result.clip is not None
    assert result.clip.shares == Decimal("5")


def test_f4_rest_trade_size_ignored_for_maker_clip() -> None:
    result = attribute_owned_fill(
        {
            "taker_order_id": "foreign",
            "size": "10",
            "price": "0.50",
            "asset_id": "yes",
            "maker_orders": [
                {"order_id": "our", "matched": "4", "price": "0.40", "asset_id": "yes"}
            ],
        },
        {"our"},
        "yes",
        owned_side_by_venue={"our": "BUY"},
    )
    assert result.clip is not None
    assert result.clip.shares == Decimal("4")


def test_f5_two_owned_maker_clips_fail_closed() -> None:
    result = attribute_owned_fill(
        {
            "maker_orders": [
                {"order_id": "a", "matched": "1", "price": "0.4", "asset_id": "yes"},
                {"order_id": "b", "matched": "2", "price": "0.4", "asset_id": "yes"},
            ],
            "size": "3",
            "price": "0.4",
            "asset_id": "yes",
        },
        {"a", "b"},
        "yes",
        owned_side_by_venue={"a": "BUY", "b": "BUY"},
    )
    assert result.clip is None
    assert result.reason == "multiple_owned_clips"


def test_known_defect_signature() -> None:
    assert is_known_normalization_defect(
        stored_venue_order_id="taker-foreign",
        clip_venue_order_id="our",
        owned_venue_ids={"our"},
    )


def _open_with_live_buy() -> ExecutionSessionState:
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(
        state,
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="old-market",
            window_id="w1",
            token_id="yes-old",
        ),
    )
    spec = LimitOrderSpec(
        order_id="entry-1",
        market_id="old-market",
        instrument_id="yes",
        token_id="yes-old",
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
    return state


def test_f7_correction_reverses_then_applies_and_is_idempotent(tmp_path) -> None:
    from tyrex_pm.execution.evidence import evidence_from_dict, evidence_to_dict
    from tyrex_pm.persistence.execution_journal import SqliteExecutionJournal

    state = _open_with_live_buy()
    wrong_applied = TradeStatusObserved(
        session_id="s1",
        event_id="wrong",
        observed_at=NOW,
        dedupe_key="stream-wrong",
        source="user_stream",
        venue_trade_id="t1",
        venue_order_id="our",
        order_id="entry-1",
        token_id="yes-old",
        side=OrderSide.BUY,
        shares=Decimal("10"),
        price=Decimal("0.41"),
        status=TradeStatus.CONFIRMED,
    )
    state, _ = reduce_execution_event(state, wrong_applied)
    assert state.trades["t1"].position_applied
    assert state.confirmed_position_shares == Decimal("10")
    correction = TradeAttributionCorrected(
        session_id="s1",
        event_id="fix",
        observed_at=NOW,
        dedupe_key="fix-t1",
        source="readonly_rest",
        venue_trade_id="t1",
        original_event_id="wrong",
        previous_venue_order_id="our",
        previous_side=OrderSide.BUY,
        previous_shares=Decimal("10"),
        previous_price=Decimal("0.41"),
        previous_token_id="yes-old",
        venue_order_id="our",
        order_id="entry-1",
        token_id="yes-old",
        side=OrderSide.BUY,
        shares=Decimal("3"),
        price=Decimal("0.40"),
        status=TradeStatus.CONFIRMED,
    )
    state, _ = reduce_execution_event(state, correction)
    assert state.trades["t1"].shares == Decimal("3")
    assert state.confirmed_position_shares == Decimal("3")
    journal = SqliteExecutionJournal(tmp_path / "execution.sqlite3")
    events = [
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open-j"),
            strategy_id="z_gap",
            market_id="old-market",
            window_id="w1",
            token_id="yes-old",
        ),
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="req-j"),
            role=ExecutionRole.ENTRY,
            order=state.orders["entry-1"].spec,
        ),
        DispatchAuthorized(
            **new_envelope(session_id="s1", dedupe_key="auth-j"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            prepared_order_digest="d1",
        ),
        SubmissionAttempted(
            **new_envelope(session_id="s1", dedupe_key="att-j"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
        ),
        SubmissionResponseObserved(
            **new_envelope(session_id="s1", dedupe_key="resp-j"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
            accepted=True,
            venue_order_id="our",
            status="live",
        ),
        wrong_applied,
        correction,
    ]
    for event in events:
        journal.append(event)
    restored = ExecutionSessionState()
    for raw in journal.load("s1"):
        restored, _ = reduce_execution_event(restored, raw)
    assert restored.confirmed_position_shares == state.confirmed_position_shares
    assert restored.trades["t1"].shares == Decimal("3")
    round_trip = evidence_from_dict(evidence_to_dict(correction))
    assert round_trip.previous_shares == Decimal("10")
    restored, _ = reduce_execution_event(restored, correction)
    assert restored.confirmed_position_shares == Decimal("3")
    duplicate = TradeAttributionCorrected(
        session_id="s1",
        event_id="fix-dup",
        observed_at=NOW,
        dedupe_key="fix-t1-dup",
        source="readonly_rest",
        venue_trade_id="t1",
        original_event_id="wrong",
        previous_venue_order_id="our",
        previous_side=OrderSide.BUY,
        previous_shares=Decimal("3"),
        previous_price=Decimal("0.40"),
        previous_token_id="yes-old",
        venue_order_id="our",
        order_id="entry-1",
        token_id="yes-old",
        side=OrderSide.BUY,
        shares=Decimal("3"),
        price=Decimal("0.40"),
        status=TradeStatus.CONFIRMED,
    )
    restored, _ = reduce_execution_event(restored, duplicate)
    assert restored.confirmed_position_shares == Decimal("3")
    mismatched = TradeAttributionCorrected(
        session_id="s1",
        event_id="fix-bad",
        observed_at=NOW,
        dedupe_key="fix-t1-bad",
        source="readonly_rest",
        venue_trade_id="t1",
        original_event_id="wrong",
        previous_venue_order_id="someone-else",
        previous_side=OrderSide.BUY,
        previous_shares=Decimal("99"),
        previous_price=Decimal("0.11"),
        previous_token_id="yes-old",
        venue_order_id="our",
        order_id="entry-1",
        token_id="yes-old",
        side=OrderSide.BUY,
        shares=Decimal("2"),
        price=Decimal("0.39"),
        status=TradeStatus.CONFIRMED,
    )
    restored, _ = reduce_execution_event(restored, mismatched)
    assert restored.phase is ExecutionPhase.MANUAL_INTERVENTION
    assert restored.last_error == "correction_previous_mismatch"
    assert restored.confirmed_position_shares == Decimal("3")
    journal.close()


def test_manual_is_terminal_but_not_safe_to_open_entry() -> None:
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(
        state,
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="m",
            window_id="w",
            token_id="t",
        ),
    )
    state, _ = reduce_execution_event(
        state,
        ManualInterventionRequired(
            **new_envelope(session_id="s1", dedupe_key="man"),
            reason="entry_evidence_timeout",
        ),
    )
    assert state.terminal
    assert not state.safe_to_open_entry


class _ScopedGateway:
    def __init__(self, snapshots: dict[str, dict[str, object]]) -> None:
        self.snapshots = snapshots

    async def collateral_balance(self):
        return Decimal("20"), Decimal("20")

    async def conditional_balance(self, token_id):
        row = self.snapshots.get(token_id, {})
        return Decimal(str(row.get("balance", "0"))), Decimal("20")

    async def list_open_orders(self, **kwargs):
        market_id = kwargs.get("market_id")
        return list(self.snapshots.get(f"orders:{market_id}", []))


async def _inspect_runtime(gateway: _ScopedGateway) -> TradingRuntime:
    runtime = bare_runtime()
    runtime.account_state_authority = AccountStateAuthority(
        gateway, policy=AccountStatePolicy()
    )
    runtime.known_dust_residuals = []
    runtime.capabilities = CapabilityController()
    records: list[tuple[str, dict]] = []
    runtime.run_recorder = type(
        "Rec", (), {"record": staticmethod(lambda kind, payload: records.append((kind, payload)))}
    )()
    runtime._inspect_records = records
    runtime.config = type(
        "Cfg", (), {"risk": type("Risk", (), {"maximum_total_debit": Decimal("5")})()}
    )()
    return runtime


@pytest.mark.asyncio
async def test_f20_scoped_clear_old_market_allows_entry() -> None:
    gateway = _ScopedGateway({"yes-old": {"balance": "0"}, "orders:old-market": []})
    runtime = await _inspect_runtime(gateway)
    restored = ExecutionSessionState()
    restored, _ = reduce_execution_event(
        restored,
        SessionOpened(
            **new_envelope(session_id="old", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="old-market",
            window_id="old-w",
            token_id="yes-old",
        ),
    )
    restored, _ = reduce_execution_event(
        restored,
        ManualInterventionRequired(
            **new_envelope(session_id="old", dedupe_key="man"),
            reason="unrepairable",
        ),
    )
    action = await runtime._inspect_retained_session(restored, bound_market_id="new-market")
    assert action == "foreign_inventory_absent"


@pytest.mark.asyncio
async def test_f20b_current_clear_old_exposed_blocks() -> None:
    gateway = _ScopedGateway(
        {
            "yes-old": {"balance": "4"},
            "orders:old-market": [],
            "yes-new": {"balance": "0"},
            "orders:new-market": [],
        }
    )
    runtime = await _inspect_runtime(gateway)
    restored = ExecutionSessionState()
    restored, _ = reduce_execution_event(
        restored,
        SessionOpened(
            **new_envelope(session_id="old", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="old-market",
            window_id="old-w",
            token_id="yes-old",
        ),
    )
    restored, _ = reduce_execution_event(
        restored,
        ManualInterventionRequired(
            **new_envelope(session_id="old", dedupe_key="man"),
            reason="leftover",
        ),
    )
    action = await runtime._inspect_retained_session(restored, bound_market_id="new-market")
    assert action == "block_foreign"


@pytest.mark.asyncio
async def test_f20d_journal_uncertainty_blocks_even_if_snapshot_clear() -> None:
    gateway = _ScopedGateway({"yes-old": {"balance": "0"}, "orders:old-market": []})
    runtime = await _inspect_runtime(gateway)
    restored = _open_with_live_buy()
    restored, _ = reduce_execution_event(
        restored,
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
    # identity market is old-market; make phase MANUAL without clearing attempts
    restored.phase = ExecutionPhase.MANUAL_INTERVENTION
    action = await runtime._inspect_retained_session(restored, bound_market_id="new-market")
    assert action == "block_foreign"
    assert restored.has_journal_uncertainty


@pytest.mark.asyncio
async def test_f20c_stale_or_unavailable_old_market_blocks() -> None:
    from tyrex_pm.execution.account_state import (
        AccountSnapshotStatus,
        AccountStateSnapshot,
        TokenAccountState,
    )

    runtime = await _inspect_runtime(
        _ScopedGateway({"yes-old": {"balance": "0"}, "orders:old-market": []})
    )

    async def stale_observe(**_kwargs):
        return AccountStateSnapshot(
            market_id="old-market",
            token_ids=("yes-old",),
            status=AccountSnapshotStatus.STALE,
            observed_at=NOW,
            observed_monotonic_s=0.0,
            collateral_balance=Decimal("20"),
            collateral_allowance=Decimal("20"),
            tokens=(TokenAccountState("yes-old", Decimal("0"), Decimal("20")),),
            open_order_ids=(),
            blockers=("ACCOUNT_SNAPSHOT_STALE",),
            read_attempt=1,
        )

    runtime.account_state_authority.observe = stale_observe  # type: ignore[method-assign]
    restored = ExecutionSessionState()
    restored, _ = reduce_execution_event(
        restored,
        SessionOpened(
            **new_envelope(session_id="old", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="old-market",
            window_id="old-w",
            token_id="yes-old",
        ),
    )
    restored, _ = reduce_execution_event(
        restored,
        ManualInterventionRequired(
            **new_envelope(session_id="old", dedupe_key="man"),
            reason="leftover",
        ),
    )
    action = await runtime._inspect_retained_session(restored, bound_market_id="new-market")
    assert action == "block_foreign"


@pytest.mark.asyncio
async def test_f_int_prepare_gate_refuses_buy_when_stop_requested() -> None:
    import time
    from types import SimpleNamespace

    from tyrex_pm.execution.final_gate import FinalExecutionGate, FinalGatePolicy
    from tyrex_pm.execution.orders import MarketBuyOrderSpec
    from tyrex_pm.market_data.book_health import SyncHealth

    caps = CapabilityController(
        live_requested=True,
        public_feeds_ready=True,
        active_market_ready=True,
        books_ready=True,
        model_ready=True,
        account_reads_ready=True,
        user_stream_ready=True,
        collateral_ready=True,
        entry_allowance_ready=True,
        order_metadata_ready=True,
        prior_scope_clear=True,
        entry_window_open=True,
    )
    caps.set_blocker("STOP_REQUESTED", "prepare_interrupted")
    leg = SimpleNamespace(
        token_id="yes",
        sync_health=SyncHealth.READY,
        data_age_ms=10,
        tick_size=Decimal("0.01"),
        quote=SimpleNamespace(
            best_ask=Decimal("0.50"),
            best_bid=Decimal("0.49"),
            ask_size_at_touch=Decimal("100"),
        ),
    )
    view = SimpleNamespace(binding_id="binding", up=leg, down=SimpleNamespace(token_id="no"))
    gate = FinalExecutionGate(
        capture_book=lambda: view,
        capabilities=caps,
        active_binding_id=lambda: "binding",
        policy=FinalGatePolicy(),
    )
    spec = MarketBuyOrderSpec(
        order_id="e1",
        market_id="m",
        instrument_id="yes",
        token_id="yes",
        spend_amount=Decimal("5"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.6"),
        estimated_shares=Decimal("10"),
        metadata={"candidate_monotonic_ns": time.monotonic_ns()},
    )
    allowed, reason = await gate(spec)
    assert allowed is False
    assert reason is not None and "STOP_REQUESTED" in reason
