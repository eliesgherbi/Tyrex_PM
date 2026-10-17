"""Position accounting: one purchase is never counted twice, pending exposure never reads as flat.

Regression for the 2026-10-16 incident (session ...0cdf5f07): a reconciliation saw the 156 bought
shares in the wallet, then the CONFIRMED status of the same trade added another 156 (internal
position 312). Settlement removed the real 156 and left 156 phantom shares that kept the startup
check blocked. The position is now derived from separate evidence instead of being overwritten by
balance observations and incremented by trade confirmations.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.execution.evidence import (
    DispatchAuthorized,
    ExecutionRole,
    ExternalPositionAdjustmentObserved,
    ManualInterventionRequired,
    MarketSettlementObserved,
    OrderRequested,
    ReconciliationObserved,
    SessionOpened,
    SubmissionAttempted,
    SubmissionResponseObserved,
    TradeStatus,
    TradeStatusObserved,
    new_envelope,
)
from tyrex_pm.execution.orders import LimitOrderSpec, MarketSellOrderSpec, OrderSide, TimeInForce
from tyrex_pm.execution.reducer import ReducerInvariantError, reduce_execution_event
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.execution.startup_reconciliation import (
    AccountIdentity,
    Holding,
    InspectionFacts,
    MarketLifecycleObservation,
    ReadCoverage,
    assess,
)
from tyrex_pm.persistence.execution_journal import SqliteExecutionJournal

SID = "s-incident"
COND = "cond-1"
TOKEN = "token-up"
D = Decimal


class Session:
    """Small event builder around the real reducer (every event goes through the journal format)."""

    def __init__(self) -> None:
        self.state = ExecutionSessionState()
        self.events: list[object] = []
        self._n = 0

    def apply(self, event: object) -> tuple:
        self.events.append(event)
        self.state, effects = reduce_execution_event(self.state, event)
        return effects

    def _env(self, key: str) -> dict:
        self._n += 1
        return new_envelope(session_id=SID, dedupe_key=f"{key}:{self._n}")

    def open_entry(self, shares: str = "156") -> "Session":
        self.apply(
            SessionOpened(
                **self._env("open"),
                strategy_id="settlement_value",
                market_id=COND,
                window_id="w1",
                token_id=TOKEN,
            )
        )
        spec = LimitOrderSpec(
            order_id="entry-1",
            market_id=COND,
            instrument_id=TOKEN,
            token_id=TOKEN,
            side=OrderSide.BUY,
            limit_price=D("0.03"),
            shares=D(shares),
            time_in_force=TimeInForce.GTC,
        )
        self.apply(OrderRequested(**self._env("req"), role=ExecutionRole.ENTRY, order=spec))
        self.apply(
            DispatchAuthorized(
                **self._env("auth"),
                role=ExecutionRole.ENTRY,
                order_id="entry-1",
                prepared_order_digest="d",
            )
        )
        self.apply(
            SubmissionAttempted(
                **self._env("att"), role=ExecutionRole.ENTRY, order_id="entry-1", attempt_id="a1"
            )
        )
        return self

    def response(self, matched: str = "156") -> None:
        self.apply(
            SubmissionResponseObserved(
                **self._env("resp"),
                role=ExecutionRole.ENTRY,
                order_id="entry-1",
                attempt_id="a1",
                accepted=True,
                venue_order_id="v-entry",
                status="matched",
                cumulative_matched_shares=D(matched),
            )
        )

    def trade(
        self,
        status: TradeStatus,
        *,
        trade_id: str = "t1",
        shares: str = "156",
        side: OrderSide = OrderSide.BUY,
        source: str = "user_stream",
        venue_order_id: str = "v-entry",
        order_id: str = "entry-1",
    ) -> tuple:
        return self.apply(
            TradeStatusObserved(
                **self._env(f"trade-{trade_id}-{status.value}-{source}"),
                source=source,
                venue_trade_id=trade_id,
                venue_order_id=venue_order_id,
                order_id=order_id,
                token_id=TOKEN,
                side=side,
                shares=D(shares),
                price=D("0.03"),
                status=status,
            )
        )

    def reconcile(self, position: str, sellable: str | None = None) -> tuple:
        return self.apply(
            ReconciliationObserved(
                **self._env("recon"),
                source="readonly_rest",
                complete=True,
                confirmed_position_shares=D(position),
                sellable_shares=D(sellable or position),
                open_order_ids=(),
            )
        )

    def settle(self, holding: str = "156") -> None:
        self.apply(
            MarketSettlementObserved(
                **self._env("settle"),
                condition_id=COND,
                token_id=TOKEN,
                holding_shares=D(holding),
                payout_per_share=D("0"),
                proof_source="payoutNumerators",
                supported=True,
            )
        )

    def exit_order(
        self, shares: str, order_id: str = "exit-1", venue_order_id: str = "v-exit"
    ) -> None:
        spec = MarketSellOrderSpec(
            order_id=order_id,
            market_id=COND,
            instrument_id=TOKEN,
            token_id=TOKEN,
            shares=D(shares),
            minimum_price=D("0.01"),
            time_in_force=TimeInForce.FAK,
        )
        self.apply(OrderRequested(**self._env("exit-req"), role=ExecutionRole.EXIT, order=spec))
        self.apply(
            DispatchAuthorized(
                **self._env("exit-auth"),
                role=ExecutionRole.EXIT,
                order_id=order_id,
                prepared_order_digest="dx",
            )
        )
        self.apply(
            SubmissionAttempted(
                **self._env("exit-att"), role=ExecutionRole.EXIT, order_id=order_id, attempt_id="ax"
            )
        )
        self.apply(
            SubmissionResponseObserved(
                **self._env("exit-resp"),
                role=ExecutionRole.EXIT,
                order_id=order_id,
                attempt_id="ax",
                accepted=True,
                venue_order_id=venue_order_id,
                status="matched",
                cumulative_matched_shares=D(shares),
            )
        )


def _incident() -> Session:
    """The recorded event order of session ...0cdf5f07 (balance seen before CONFIRMED)."""
    s = Session().open_entry()
    s.trade(TradeStatus.MATCHED)  # 6  user stream
    s.response()  # 7
    s.reconcile("0")  # 8  balance not there yet
    s.trade(TradeStatus.MATCHED, source="readonly_rest")  # 9
    s.trade(TradeStatus.MINED)  # 10
    s.trade(TradeStatus.MINED, source="readonly_rest")  # 11
    s.reconcile("156")  # 12 wallet shows the purchase
    s.trade(TradeStatus.CONFIRMED)  # 13 same purchase confirmed
    s.trade(TradeStatus.CONFIRMED, source="readonly_rest")  # 14 confirmed again via REST
    return s


# --- incident ------------------------------------------------------------------------------------


def test_incident_balance_before_confirmation_counts_once() -> None:
    s = _incident()
    assert s.state.confirmed_position_shares == D("156")  # was 312
    assert s.state.phase is ExecutionPhase.POSITION_OPEN


def test_incident_settlement_leaves_no_phantom_and_startup_check_passes() -> None:
    s = _incident()
    s.apply(ManualInterventionRequired(**s._env("manual"), reason="exit_retry_budget_exhausted"))
    s.settle("156")
    assert s.state.confirmed_position_shares == D("0")  # was 156 phantom
    assert s.state.safe_to_open_entry
    facts = InspectionFacts(
        identity=AccountIdentity(chain_id=137, environment="production", wallet="0xabc"),
        coverage=ReadCoverage(
            complete=True, positions_complete=True, orders_complete=True, activity_complete=True
        ),
        sessions=(s.state,),
        # the losing tokens are still in the wallet (settled, not redeemed)
        holdings=(Holding(condition_id=COND, token_id=TOKEN, shares=D("156")),),
        lifecycles={
            COND: MarketLifecycleObservation(condition_id=COND, status="CLOSED_PENDING_RESOLUTION")
        },
        bound_wallet="0xabc",
    )
    assessment = assess(facts)
    assert not [i for i in assessment.issues if i.severity.value == "BLOCKED"], assessment.issues


def test_confirmation_before_balance_counts_once() -> None:
    s = Session().open_entry()
    s.response()
    s.trade(TradeStatus.CONFIRMED)
    assert s.state.confirmed_position_shares == D("156")
    s.reconcile("156")
    assert s.state.confirmed_position_shares == D("156")


def test_repeated_confirmations_and_observations_are_idempotent() -> None:
    s = _incident()
    for _ in range(3):
        s.trade(TradeStatus.CONFIRMED)
        s.trade(TradeStatus.CONFIRMED, source="readonly_rest")
        s.reconcile("156")
    assert s.state.confirmed_position_shares == D("156")
    assert len(s.state.trades) == 1


# --- pending exposure ----------------------------------------------------------------------------


def test_matched_trade_is_pending_exposure_not_flat() -> None:
    s = Session().open_entry()
    s.trade(TradeStatus.MATCHED)
    s.response()
    s.reconcile("0")  # balance has not caught up
    st = s.state
    assert st.confirmed_position_shares == D("0")
    assert st.pending_buy_shares == D("156")
    assert st.exposure_upper_bound_shares == D("156")
    assert st.has_journal_uncertainty and st.needs_venue_followup
    assert not st.safe_to_open_entry
    assert not st.clearance_prerequisites_hold
    assert st.phase is ExecutionPhase.RECONCILING


def test_pending_exposure_blocks_a_second_buy_in_the_session() -> None:
    s = Session().open_entry()
    s.trade(TradeStatus.MINED)
    spec = LimitOrderSpec(
        order_id="entry-2",
        market_id=COND,
        instrument_id=TOKEN,
        token_id=TOKEN,
        side=OrderSide.BUY,
        limit_price=D("0.03"),
        shares=D("10"),
        time_in_force=TimeInForce.GTC,
    )
    with pytest.raises(ReducerInvariantError):
        s.apply(OrderRequested(**s._env("req2"), role=ExecutionRole.ENTRY, order=spec))


def test_partial_fill_keeps_unconfirmed_part_pending() -> None:
    s = Session().open_entry()
    s.response("156")
    s.trade(TradeStatus.CONFIRMED, trade_id="t1", shares="100")
    s.trade(TradeStatus.MINED, trade_id="t2", shares="56")
    s.reconcile("156")  # wallet already holds both clips
    st = s.state
    assert st.confirmed_position_shares == D("156")
    assert st.pending_buy_shares == D("56")
    assert st.has_pending_trades and not st.safe_to_open_entry
    s.trade(TradeStatus.CONFIRMED, trade_id="t2", shares="56")
    assert s.state.confirmed_position_shares == D("156")  # not 212
    assert not s.state.has_pending_trades


def test_failed_trade_stops_being_pending() -> None:
    s = Session().open_entry()
    s.trade(TradeStatus.MATCHED)
    s.trade(TradeStatus.FAILED)
    assert not s.state.has_pending_trades
    assert s.state.confirmed_position_shares == D("0")


# --- balance lag, sells, adjustments -------------------------------------------------------------


def test_zero_balance_does_not_erase_confirmed_buy() -> None:
    s = Session().open_entry()
    s.response()
    s.trade(TradeStatus.CONFIRMED)
    effects = s.reconcile("0")
    assert s.state.confirmed_position_shares == D("156")
    assert s.state.phase is ExecutionPhase.RECONCILING
    assert any(e.reason == "balance_lags_entry" for e in effects)


def _bought_and_observed() -> Session:
    s = Session().open_entry()
    s.response()
    s.trade(TradeStatus.CONFIRMED)
    s.reconcile("156")
    return s


def test_confirmed_sell_after_observation_reduces_position() -> None:
    s = _bought_and_observed()
    s.exit_order("100")
    s.trade(
        TradeStatus.CONFIRMED,
        trade_id="t-sell",
        shares="100",
        side=OrderSide.SELL,
        venue_order_id="v-exit",
        order_id="exit-1",
    )
    assert s.state.confirmed_position_shares == D("56")


def test_sell_seen_in_balance_before_confirmation_is_not_subtracted_twice() -> None:
    s = _bought_and_observed()
    s.exit_order("100")
    s.reconcile("56")  # wallet already reflects the sell
    s.trade(
        TradeStatus.CONFIRMED,
        trade_id="t-sell",
        shares="100",
        side=OrderSide.SELL,
        venue_order_id="v-exit",
        order_id="exit-1",
    )
    assert s.state.confirmed_position_shares == D("56")  # not 0 or negative


def test_full_sell_confirmed_then_flat_observation_completes() -> None:
    s = _bought_and_observed()
    s.exit_order("156")
    s.trade(
        TradeStatus.CONFIRMED,
        trade_id="t-sell",
        shares="156",
        side=OrderSide.SELL,
        venue_order_id="v-exit",
        order_id="exit-1",
    )
    assert s.state.confirmed_position_shares == D("0")
    s.reconcile("0")
    assert s.state.phase is ExecutionPhase.COMPLETED_FLAT


def test_external_disposal_is_removed_once() -> None:
    s = _bought_and_observed()
    s.apply(
        ExternalPositionAdjustmentObserved(
            **s._env("ext"),
            activity_id="act-1",
            token_id=TOKEN,
            condition_id=COND,
            shares=D("50"),
            cause="transfer",
        )
    )
    assert s.state.confirmed_position_shares == D("106")
    s.reconcile("106")  # balance now reflects the disposal
    assert s.state.confirmed_position_shares == D("106")
    s.reconcile("156")  # a stale read cannot add it back twice
    assert s.state.confirmed_position_shares == D("106")


def test_settlement_then_unredeemed_balance_observation_stays_flat() -> None:
    s = _bought_and_observed()
    s.settle("156")
    s.reconcile("156")  # losing tokens still in the wallet
    assert s.state.confirmed_position_shares == D("0")


# --- restart recovery ----------------------------------------------------------------------------


def test_restart_replays_the_journal_to_the_corrected_state(tmp_path) -> None:
    s = _incident()
    s.apply(ManualInterventionRequired(**s._env("manual"), reason="exit_retry_budget_exhausted"))
    s.settle("156")
    path = tmp_path / "execution.sqlite3"
    journal = SqliteExecutionJournal(path)
    for event in s.events:
        assert journal.append(event)
    journal.close()

    reopened = SqliteExecutionJournal(path)  # process restart
    restored = ExecutionSessionState()
    for event in reopened.load(SID):
        restored, _ = reduce_execution_event(restored, event)
    reopened.close()
    assert restored.confirmed_position_shares == D("0")
    assert restored.safe_to_open_entry
    assert restored.trades["t1"].status is TradeStatus.CONFIRMED
    # replaying the same evidence twice changes nothing (applied event ids)
    for event in s.events:
        restored, _ = reduce_execution_event(restored, event)
    assert restored.confirmed_position_shares == D("0")


def test_directly_built_state_keeps_its_position() -> None:
    """States not built by replay (tools, older code) keep the stored position as an observation."""
    st = ExecutionSessionState()
    st, _ = reduce_execution_event(
        st,
        SessionOpened(
            **new_envelope(session_id=SID, dedupe_key="o"),
            strategy_id="x",
            market_id=COND,
            window_id="w",
            token_id=TOKEN,
        ),
    )
    st.confirmed_position_shares = D("12")
    st, _ = reduce_execution_event(
        st,
        ManualInterventionRequired(**new_envelope(session_id=SID, dedupe_key="m"), reason="manual"),
    )
    assert st.confirmed_position_shares == D("12")


# --- committed hold detach -----------------------------------------------------------------------


def test_committed_hold_does_not_detach_while_a_clip_is_still_pending() -> None:
    from types import SimpleNamespace

    from tyrex_pm.execution.lifecycle import ExecutionLifecycle

    s = Session().open_entry()
    s.response("156")
    s.trade(TradeStatus.CONFIRMED, trade_id="t1", shares="100")
    s.trade(TradeStatus.MINED, trade_id="t2", shares="56")
    coordinator = SimpleNamespace(states={SID: s.state}, listeners=[])
    lifecycle = ExecutionLifecycle(
        coordinator=coordinator, reconciler=None, planner=None, capabilities=None
    )
    lifecycle.session_id = SID
    with pytest.raises(RuntimeError, match="pending work"):
        lifecycle.detach_committed(reason="hold_committed")
    s.trade(TradeStatus.CONFIRMED, trade_id="t2", shares="56")
    coordinator.states[SID] = s.state
    assert lifecycle.detach_committed(reason="hold_committed") == SID
