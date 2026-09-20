"""G1 leftover flatten: remaining size, venue-legal clip, and dust terminal."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.intents import EnterIntent, ExitIntent, new_intent_id
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.evidence import (
    DustResidualRecorded,
    ExecutionRole,
    ReconciliationObserved,
    TradeStatus,
    evidence_from_dict,
    evidence_to_dict,
    new_envelope,
)
from tyrex_pm.execution.exit_sizing import (
    can_post_exit,
    classify_dust_leftover,
    is_exempt_prior_position_blocker,
    remaining_exit_shares,
    trade_remaining_after_exits,
    venue_legal_sell_shares,
)
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.orders import MarketSellOrderSpec, TimeInForce
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.reducer import reduce_execution_event
from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
    KnownDustResidual,
    OrderExecutionState,
    SessionIdentity,
    TradeRecord,
)
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.reporting.run_report import RunReportInput, build_run_report
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
LIVE_TP_BUY = Decimal("7.101448")
LIVE_TP_SELL = Decimal("7.10")
LIVE_TP_LEFT = Decimal("0.001448")
LIVE_TRAIL_BUY = Decimal("6.712325")
LIVE_TRAIL_SELL = Decimal("6.71")
LIVE_TRAIL_LEFT = Decimal("0.002325")


def _policy() -> LifecyclePolicy:
    return LifecyclePolicy(
        evidence_timeout_s=1,
        reconciliation_interval_s=0.001,
        exit_retry_limit=3,
        exit_retry_budget_s=1,
        sell_size_increment=Decimal("0.01"),
        dust_abs_shares=Decimal("0.01"),
        dust_max_notional=Decimal("0.05"),
    )


def _entry() -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("ask70"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("correlation"),
        causation_id=None,
        reason_code="ENTRY_CANDIDATE",
        target_notional=Decimal("5"),
        outcome=OutcomeSide.YES,
        max_price=Decimal("0.70"),
    )


def _exit() -> ExitIntent:
    return ExitIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("ask70"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("correlation"),
        causation_id=None,
        reason_code="PROTECTION_TP",
    )


def _state_with_buy(*, buy: Decimal, sellable: Decimal, exit_hwm: Decimal = Decimal("0")) -> ExecutionSessionState:
    state = ExecutionSessionState()
    state.identity = SessionIdentity(
        session_id="s1",
        strategy_id="ask70",
        market_id="market-1",
        window_id="btc-5m-1",
        token_id="yes-token",
    )
    state.phase = ExecutionPhase.POSITION_OPEN
    state.confirmed_position_shares = buy
    state.sellable_shares = sellable
    state.trades["t-buy"] = TradeRecord(
        venue_trade_id="t-buy",
        venue_order_id="venue-entry",
        local_order_id="entry-1",
        token_id="yes-token",
        side="BUY",
        shares=buy,
        price=Decimal("0.69"),
        status=TradeStatus.CONFIRMED,
        position_applied=True,
    )
    if exit_hwm > 0:
        spec = MarketSellOrderSpec(
            order_id="exit-1",
            market_id="market-1",
            instrument_id="yes",
            token_id="yes-token",
            shares=exit_hwm,
            minimum_price=Decimal("0.01"),
            time_in_force=TimeInForce.FAK,
        )
        order = OrderExecutionState(role=ExecutionRole.EXIT, spec=spec)
        order.venue_order_id = "venue-exit"
        order.cumulative_matched_hwm = exit_hwm
        state.orders[spec.order_id] = order
        state.exit_order_ids.append(spec.order_id)
    return state


def test_run_configs_require_explicit_dust_policy() -> None:
    cfg = load_trading_run_config(Path("config/runs/ask70_protection_tiny_live.yaml"))
    assert cfg.lifecycle.sell_size_increment == Decimal("0.01")
    assert cfg.lifecycle.dust_abs_shares == Decimal("0.01")
    assert cfg.lifecycle.dust_max_notional == Decimal("0.05")


def test_run_config_rejects_missing_dust_fields(tmp_path: Path) -> None:
    raw = Path("config/runs/z_gap_tiny_live.yaml").read_text(encoding="utf-8")
    for key in ("sell_size_increment", "dust_abs_shares", "dust_max_notional"):
        raw = raw.replace(f'  {key}: "0.01"\n', "").replace(f'  {key}: "0.05"\n', "")
    path = tmp_path / "no_dust.yaml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(RunConfigError, match="lifecycle."):
        load_trading_run_config(path)


def test_run_config_rejects_missing_prepare_latency_fields(tmp_path: Path) -> None:
    raw = Path("config/runs/z_gap_tiny_live.yaml").read_text(encoding="utf-8")
    raw = raw.replace("  prepare_budget_ms: 2000\n", "")
    raw = raw.replace("  refresh_wait_budget_ms: 500\n", "")
    path = tmp_path / "no_prepare_budget.yaml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(RunConfigError, match="lifecycle."):
        load_trading_run_config(path)


def test_venue_legal_sell_rounds_down_to_two_decimals() -> None:
    assert venue_legal_sell_shares(LIVE_TP_BUY, Decimal("0.01")) == LIVE_TP_SELL
    assert venue_legal_sell_shares(LIVE_TRAIL_BUY, Decimal("0.01")) == LIVE_TRAIL_SELL
    assert venue_legal_sell_shares(Decimal("6.00"), Decimal("0.01")) == Decimal("6.00")
    assert venue_legal_sell_shares(LIVE_TP_LEFT, Decimal("0.01")) == Decimal("0.00")


def test_remaining_uses_matched_hwm_not_original_clip() -> None:
    # Live retry-2 shape: sellable still looks like the buy, EXIT already matched 7.10.
    state = _state_with_buy(buy=LIVE_TP_BUY, sellable=LIVE_TP_BUY, exit_hwm=LIVE_TP_SELL)
    assert remaining_exit_shares(state) == LIVE_TP_LEFT
    assert trade_remaining_after_exits(state) == LIVE_TP_LEFT

    trail = _state_with_buy(buy=LIVE_TRAIL_BUY, sellable=LIVE_TRAIL_BUY, exit_hwm=LIVE_TRAIL_SELL)
    assert remaining_exit_shares(trail) == LIVE_TRAIL_LEFT


def test_remaining_uses_updated_balance_after_almost_full_sell() -> None:
    state = _state_with_buy(buy=LIVE_TP_BUY, sellable=LIVE_TP_LEFT, exit_hwm=LIVE_TP_SELL)
    assert remaining_exit_shares(state) == LIVE_TP_LEFT


def test_confirmed_sell_does_not_double_count() -> None:
    state = _state_with_buy(buy=LIVE_TP_BUY, sellable=LIVE_TP_LEFT, exit_hwm=LIVE_TP_SELL)
    state.trades["t-sell"] = TradeRecord(
        venue_trade_id="t-sell",
        venue_order_id="venue-exit",
        local_order_id="exit-1",
        token_id="yes-token",
        side="SELL",
        shares=LIVE_TP_SELL,
        price=Decimal("0.86"),
        status=TradeStatus.CONFIRMED,
        position_applied=True,
    )
    state.confirmed_position_shares = LIVE_TP_LEFT
    assert state.confirmed_trade_net_shares == LIVE_TP_LEFT
    assert trade_remaining_after_exits(state) == LIVE_TP_LEFT
    assert remaining_exit_shares(state) == LIVE_TP_LEFT


def test_classify_dust_for_live_leftovers_and_min_size_edges() -> None:
    kwargs = dict(
        increment=Decimal("0.01"),
        min_order_size=Decimal("5"),
        last_bid=Decimal("0.86"),
        dust_abs_shares=Decimal("0.01"),
        dust_max_notional=Decimal("0.05"),
    )
    assert classify_dust_leftover(LIVE_TP_LEFT, **kwargs).code == "below_dust_abs_shares"
    assert classify_dust_leftover(LIVE_TRAIL_LEFT, **kwargs).code == "below_dust_abs_shares"
    assert classify_dust_leftover(Decimal("0.01"), **kwargs).code == "below_dust_abs_shares"
    assert classify_dust_leftover(Decimal("6.00"), **kwargs) is None
    assert can_post_exit(legal_shares=Decimal("6.00"), min_order_size=Decimal("5"))
    assert not can_post_exit(legal_shares=Decimal("0.01"), min_order_size=Decimal("5"))
    assert not can_post_exit(legal_shares=Decimal("6.00"), min_order_size=None)
    large = classify_dust_leftover(Decimal("4.00"), **kwargs)
    assert large is None


def test_known_dust_exempts_prior_position_on_same_qty() -> None:
    residual = KnownDustResidual(
        token_id="old-token",
        shares=LIVE_TP_LEFT,
        market_id="old-market",
        window_id="old-window",
        classification="below_dust_abs_shares",
    )
    assert is_exempt_prior_position_blocker(
        f"PRIOR_POSITION:old-token:{LIVE_TP_LEFT}", (residual,)
    )
    assert not is_exempt_prior_position_blocker(
        f"PRIOR_POSITION:old-token:{LIVE_TP_BUY}", (residual,)
    )
    assert not is_exempt_prior_position_blocker("PRIOR_OPEN_ORDER:abc", (residual,))


def test_dust_residual_evidence_roundtrip() -> None:
    event = DustResidualRecorded(
        **new_envelope(session_id="s1", dedupe_key="dust:yes-token:0.001448"),
        token_id="yes-token",
        shares=LIVE_TP_LEFT,
        market_id="market-1",
        window_id="btc-5m-1",
        classification="below_dust_abs_shares",
        last_bid=Decimal("0.86"),
        min_order_size=Decimal("5"),
        sell_size_increment=Decimal("0.01"),
        notional=LIVE_TP_LEFT * Decimal("0.86"),
    )
    restored = evidence_from_dict(evidence_to_dict(event))
    assert isinstance(restored, DustResidualRecorded)
    assert restored.shares == LIVE_TP_LEFT
    assert restored.classification == "below_dust_abs_shares"


def test_dust_terminal_stays_locked_after_late_recon() -> None:
    state = _state_with_buy(buy=LIVE_TP_BUY, sellable=LIVE_TP_LEFT, exit_hwm=LIVE_TP_SELL)
    state.confirmed_position_shares = LIVE_TP_LEFT
    reduce_execution_event(
        state,
        DustResidualRecorded(
            **new_envelope(session_id="s1", dedupe_key="dust"),
            token_id="yes-token",
            shares=LIVE_TP_LEFT,
            market_id="market-1",
            window_id="btc-5m-1",
            classification="below_dust_abs_shares",
        ),
    )
    assert state.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST
    reduce_execution_event(
        state,
        ReconciliationObserved(
            **new_envelope(session_id="s1", dedupe_key="recon-late"),
            source="readonly_rest",
            complete=True,
            confirmed_position_shares=LIVE_TP_LEFT,
            sellable_shares=LIVE_TP_LEFT,
            open_order_ids=(),
        ),
    )
    assert state.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST
    assert state.terminal
    assert not state.has_exposure


def test_report_completed_flat_with_dust_is_ok() -> None:
    state = _state_with_buy(buy=LIVE_TP_BUY, sellable=LIVE_TP_LEFT, exit_hwm=LIVE_TP_SELL)
    state.phase = ExecutionPhase.COMPLETED_FLAT_WITH_DUST
    state.confirmed_position_shares = LIVE_TP_LEFT
    state.dust_residual = KnownDustResidual(
        token_id="yes-token",
        shares=LIVE_TP_LEFT,
        market_id="market-1",
        window_id="btc-5m-1",
        classification="below_dust_abs_shares",
        last_bid=Decimal("0.86"),
        min_order_size=Decimal("5"),
        notional=LIVE_TP_LEFT * Decimal("0.86"),
    )
    report = build_run_report(
        RunReportInput(
            run_instance_id="run",
            configured_run_name="ask70",
            live_requested=True,
            state=state,
            mutation_attempts=2,
            execution_timeline=(),
            capabilities={},
            market_data=None,
            run_evidence=(),
            runtime_errors=(),
        )
    )
    assert report["outcome"] == "COMPLETED_FLAT_WITH_DUST"
    assert report["ok"] is True
    assert report["execution"]["dust_residual"]["shares"] == str(LIVE_TP_LEFT)
    assert report["execution"]["dust_residual"]["classification"] == "below_dust_abs_shares"


class _DustVenue:
    def __init__(
        self,
        fill_shares: Decimal,
        *,
        confirm_sell: bool = True,
        min_balance: Decimal | None = None,
    ) -> None:
        self.balance = Decimal("0")
        self.posts: list[tuple[str, Decimal]] = []
        self.trades: list[SimpleNamespace] = []
        self.fill_shares = fill_shares
        self.confirm_sell = confirm_sell
        self.min_balance = min_balance

    async def prepare_order(self, spec):  # noqa: ANN001
        return PreparedOrder(spec.order_id, spec.order_id, spec)

    async def post_order(self, prepared):  # noqa: ANN001
        spec = prepared.payload
        if spec.side.value == "BUY":
            shares = self.fill_shares
            self.balance += shares
            self.posts.append(("BUY", shares))
            status = "CONFIRMED"
        else:
            shares = spec.shares
            self.posts.append(("SELL", shares))
            if shares > self.balance:
                return GatewaySubmissionResult(
                    accepted=False,
                    venue_order_id=None,
                    status="rejected",
                    error_code="not enough balance / allowance",
                    message=f"order amount: {shares}",
                )
            self.balance -= shares
            status = "CONFIRMED" if self.confirm_sell else "MATCHED"
        venue_id = f"venue-{spec.side.value.lower()}-{len(self.posts)}"
        trade = SimpleNamespace(
            id=f"trade-{len(self.trades)}",
            taker_order_id=venue_id,
            maker_orders=(),
            status=status,
            side=spec.side.value,
            size=shares,
            price=Decimal("0.69"),
            updated_at=NOW,
        )
        self.trades.append(trade)
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id=venue_id,
            status="matched",
            cumulative_matched_shares=shares,
            trade_ids=(trade.id,),
        )

    async def discard_prepared(self, prepared):  # noqa: ANN001
        return None

    async def list_open_orders(self, **_kwargs):
        return ()

    async def list_account_trades(self, **_kwargs):
        return tuple(self.trades)

    async def conditional_balance(self, _token_id):
        if self.min_balance is not None:
            return max(self.balance, self.min_balance), Decimal("100")
        return self.balance, Decimal("100")

    async def close(self):
        return None


def _lifecycle(venue: _DustVenue) -> ExecutionLifecycle:
    capabilities = CapabilityController(
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
        selected_token_sellable=True,
    )
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=venue,
        final_gate=lambda _spec: asyncio.sleep(0, result=(True, None)),
    )
    return ExecutionLifecycle(
        coordinator=coordinator,
        reconciler=SessionReconciler(venue),
        planner=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))),
        capabilities=capabilities,
        policy=_policy(),
    )


async def _run_entry_exit(
    venue: _DustVenue,
    *,
    min_order_size: Decimal,
    last_bid: Decimal = Decimal("0.86"),
) -> ExecutionSessionState:
    lifecycle = _lifecycle(venue)
    await lifecycle.open(
        session_id="session",
        strategy_id="ask70",
        market_id="market",
        window_id="window",
        token_id="token",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    state = await lifecycle.submit_entry(_entry(), token_id="token")
    assert state.phase is ExecutionPhase.POSITION_OPEN
    try:
        return await lifecycle.submit_exit(
            _exit(),
            token_id="token",
            min_order_size=min_order_size,
            last_bid=last_bid,
        )
    finally:
        await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_live_tp_leftover_is_dust_not_retry_of_original_clip() -> None:
    venue = _DustVenue(LIVE_TP_BUY)
    state = await _run_entry_exit(venue, min_order_size=Decimal("5"))
    sells = [shares for side, shares in venue.posts if side == "SELL"]
    assert sells == [LIVE_TP_SELL]
    assert state.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST
    assert state.dust_residual is not None
    assert state.dust_residual.shares == LIVE_TP_LEFT
    assert state.last_error != "exit_retry_budget_exhausted"


@pytest.mark.asyncio
async def test_live_trail_leftover_resizes_then_dusts() -> None:
    venue = _DustVenue(LIVE_TRAIL_BUY)
    state = await _run_entry_exit(venue, min_order_size=Decimal("5"), last_bid=Decimal("0.73"))
    sells = [shares for side, shares in venue.posts if side == "SELL"]
    assert sells == [LIVE_TRAIL_SELL]
    assert state.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST
    assert state.dust_residual is not None
    assert state.dust_residual.shares == LIVE_TRAIL_LEFT


@pytest.mark.asyncio
async def test_matched_unconfirmed_sell_does_not_repost_original_clip() -> None:
    venue = _DustVenue(LIVE_TP_BUY, confirm_sell=False)
    state = await _run_entry_exit(venue, min_order_size=Decimal("5"))
    sells = [shares for side, shares in venue.posts if side == "SELL"]
    assert sells == [LIVE_TP_SELL]
    assert state.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST


@pytest.mark.asyncio
async def test_remaining_above_min_order_size_is_posted() -> None:
    venue = _DustVenue(Decimal("6.00"))
    state = await _run_entry_exit(venue, min_order_size=Decimal("5"))
    sells = [shares for side, shares in venue.posts if side == "SELL"]
    assert sells == [Decimal("6.00")]
    assert state.phase is ExecutionPhase.COMPLETED_FLAT


@pytest.mark.asyncio
async def test_remaining_below_min_order_size_is_not_posted() -> None:
    venue = _DustVenue(Decimal("0.01"))
    state = await _run_entry_exit(venue, min_order_size=Decimal("5"))
    sells = [shares for side, shares in venue.posts if side == "SELL"]
    assert sells == []
    assert state.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST
    assert state.dust_residual is not None
    assert state.dust_residual.shares == Decimal("0.01")
