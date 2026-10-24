"""Fake-backed live execution path for retry / exit / continuity tests.

A real ``TradingRuntime`` (real coordinator, lifecycle, reconciler, journal, settlement
hooks and ledger) driven through a scripted venue double. Only the account-state read
authority, the final dispatch gate and market binding are stubbed; every venue
interaction goes through the production submission path.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId, TokenId
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.intents import EnterIntent, ExitIntent, HoldToResolutionIntent, new_intent_id
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.execution.account_state import AccountSnapshotStatus
from tyrex_pm.execution.coordinator import GatewaySubmissionResult, PreparedOrder
from tyrex_pm.runtime.run_config import load_trading_run_config
from tyrex_pm.runtime.trading_runtime import TradingRuntime
from tyrex_pm.strategies.base import BaseDriver
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "config" / "runs" / "settlement_value_trial.yaml"
CURVE = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))
GAMMA_FEES = {"exponent": 1, "rate": "0.07", "rebateRate": "0.2", "takerOnly": True}
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class ScriptedVenue:
    """Each POST consumes one scripted behaviour: fill | zero | reject | timeout."""

    def __init__(self, script: list[str], *, fill_shares: str = "10", price: str = "0.30"):
        self.script = list(script)
        self.posts: list[str] = []
        self.balance: dict[str, Decimal] = {}
        self.trades: list[SimpleNamespace] = []
        self.fill_shares = Decimal(fill_shares)
        self.price = Decimal(price)
        self.prepare_failures = 0
        self.fail_prepare_next = 0
        self.trade_status = "CONFIRMED"

    async def prepare_order(self, spec):  # noqa: ANN001
        if self.fail_prepare_next:
            self.fail_prepare_next -= 1
            self.prepare_failures += 1
            raise ValueError("simulated signing failure")
        return PreparedOrder(local_order_id=spec.order_id, digest=spec.order_id, payload=spec)

    async def discard_prepared(self, prepared):  # noqa: ANN001
        del prepared

    async def post_order(self, prepared):  # noqa: ANN001
        spec = prepared.payload
        side = spec.side.value
        self.posts.append(side)
        behaviour = self.script.pop(0) if self.script else "fill"
        venue_id = f"v-{len(self.posts)}"
        if behaviour == "timeout":
            raise TimeoutError("no http result")
        if behaviour == "reject":
            return GatewaySubmissionResult(accepted=False, venue_order_id=None, status="rejected")
        if behaviour == "zero":
            return GatewaySubmissionResult(accepted=True, venue_order_id=venue_id,
                                           status="unmatched", cumulative_matched_shares=0)
        shares = self.fill_shares if side == "BUY" else spec.shares
        held = self.balance.get(spec.token_id, Decimal(0))
        self.balance[spec.token_id] = held + shares if side == "BUY" else held - shares
        trade = self.trade(venue_id, side, shares, spec.token_id)
        return GatewaySubmissionResult(accepted=True, venue_order_id=venue_id, status="matched",
                                       cumulative_matched_shares=shares, trade_ids=(trade.id,))

    def trade(self, venue_id: str, side: str, shares: Decimal, token: str) -> SimpleNamespace:
        trade = SimpleNamespace(
            id=f"t-{len(self.trades) + 1}", taker_order_id=venue_id, maker_orders=(),
            status=self.trade_status, side=side, size=shares,
            price=self.price if side == "BUY" else Decimal("0.45"), asset_id=token,
            updated_at=NOW,
        )
        self.trades.append(trade)
        return trade

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        from tyrex_pm.execution.cancel import cancel_result_from_sdk

        return cancel_result_from_sdk({"canceled": tuple(order_ids), "not_canceled": {}})

    async def list_open_orders(self, **_kwargs):
        return ()

    async def list_account_trades(self, **kwargs):
        token = kwargs.get("token_id")
        return tuple(t for t in self.trades if token is None or t.asset_id == token)

    async def conditional_balance(self, token_id):
        held = self.balance.get(token_id, Decimal(0))
        return held, held

    async def collateral_balance(self):
        return Decimal("100"), Decimal("100")

    async def start_user_stream(self, **_kwargs):
        return None

    async def stop_user_stream(self):
        return None

    async def close(self):
        return None


class _Hold:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> bool:
        return False


class AccountStub:
    """READY account snapshots that follow the venue's balances."""

    def __init__(self, venue: ScriptedVenue):
        self.venue = venue
        self.observed_at = NOW

    def current(self, _market_id: str):
        venue = self.venue

        def token(token_id: str):
            held = venue.balance.get(token_id, Decimal(0))
            return SimpleNamespace(balance_shares=held, allowance_shares=held)

        # A fresh read each time (re-entry requires a snapshot newer than the release).
        self.observed_at = datetime.now(timezone.utc)
        return SimpleNamespace(
            status=AccountSnapshotStatus.READY, blockers=(), token=token, open_order_ids=(),
            observed_at=self.observed_at, collateral_balance=Decimal("100"),
            collateral_allowance=Decimal("100"), read_complete=True,
        )

    def hold_ready_snapshot(self, _market_id: str):
        return _Hold()

    def prepare(self, **_kwargs):
        return None

    def retire(self, _market_id):
        return None

    async def close(self):
        return None


class DriverStub(BaseDriver):
    def __init__(self) -> None:
        self.entry_outcomes: list[Any] = []
        self.exit_outcomes: list[Any] = []
        self.fee_curve = CURVE

    def on_entry_outcome(self, outcome) -> None:  # noqa: ANN001
        self.entry_outcomes.append(outcome)

    def on_exit_outcome(self, outcome) -> None:  # noqa: ANN001
        self.exit_outcomes.append(outcome)


def market(window: str = "w1") -> SimpleNamespace:
    mid = MarketId(f"m-{window}")
    yes = Instrument(instrument_id=InstrumentId(f"up-{window}"), market_id=mid,
                     token_id=TokenId(f"up-{window}"), outcome=OutcomeSide.YES)
    no = Instrument(instrument_id=InstrumentId(f"down-{window}"), market_id=mid,
                    token_id=TokenId(f"down-{window}"), outcome=OutcomeSide.NO)
    legs = [SimpleNamespace(token_id=yes.token_id, outcome_index=0),
            SimpleNamespace(token_id=no.token_id, outcome_index=1)]
    return SimpleNamespace(
        window_id=window,
        entry_enabled=True,
        binding=SimpleNamespace(outcomes=SimpleNamespace(legs=legs)),
        market=SimpleNamespace(condition_id=f"0x{window}", market_id=mid, yes=yes, no=no,
                               tick_size=Decimal("0.01"), min_order_size=Decimal("5"),
                               event_end=NOW + timedelta(minutes=3)),
    )


async def make_runtime(
    tmp_path: Path,
    venue: ScriptedVenue,
    *,
    strategy: dict | None = None,
    run_mode: str = "single_window",
    lifecycle_mode: str | None = None,
    tau_s: float = 120.0,
) -> tuple[TradingRuntime, DriverStub]:
    config = load_trading_run_config(TRIAL)
    params = {"entry": {"target_mode": "cap_only"}}
    for key, value in (strategy or {}).items():
        params[key] = value
    strat = settlement_value_config_from_parameters(params)
    from tyrex_pm.execution.settlement.governor import RunLimits

    limits = RunLimits(
        daily_loss_limit=Decimal("50"), max_open_worst_case_loss=Decimal("50"),
        max_reserved_debit=Decimal("50"), max_unredeemed_count=5,
        max_unredeemed_cost=Decimal("50"), max_consecutive_losses=0,
    )
    trial = replace(config.settlement.trial, max_unresolved_count=5,
                    max_unresolved_cost=Decimal("50"), trial_loss_budget=Decimal("50"))
    config = replace(
        config,
        strategy=strat,
        state_directory=tmp_path / "state",
        report_directory=tmp_path / "runs",
        market=replace(config.market, run_mode=run_mode),
        lifecycle=replace(config.lifecycle, evidence_timeout_s=0.3,
                          reconciliation_interval_s=0.01, exit_retry_budget_s=1.0),
        settlement=replace(config.settlement, ledger_path=tmp_path / "ledger.jsonl",
                           trial=trial, run_limits=limits),
        lifecycle_mode=lifecycle_mode or config.lifecycle_mode,
    )
    runtime = await TradingRuntime.create(
        config=config, output_directory=tmp_path / "out", env={}, gateway=venue,
    )

    async def allow(_spec):
        return True, None

    runtime.coordinator.final_gate = allow
    runtime.account_state_authority = AccountStub(venue)
    runtime._current_entry_tau_s = lambda: tau_s
    # Market metadata normally read from the live BookView.
    runtime._held_min_order_size = lambda: Decimal("5")
    runtime._held_best_bid = lambda _market=None: Decimal("0.45")

    async def fees(_slug, _condition):
        return GAMMA_FEES

    runtime.settlement_hooks.fee_schedule_source = fees
    driver = DriverStub()
    runtime._market_runtime_ref = SimpleNamespace(
        window_driver=lambda _w=None: driver, _strategy_by_window={}, driver=driver
    )
    bind(runtime, market("w1"))
    await runtime.coordinator.start()
    return runtime, driver


def bind(runtime: TradingRuntime, session: SimpleNamespace, stream: str | None = None) -> None:
    runtime.active_market = session
    runtime.stream_session_id = stream or f"stream-{session.window_id}"
    runtime.lifecycle.stream_alias = runtime.stream_session_id
    runtime.lifecycle.new_window()


def entry(runtime: TradingRuntime, *, episode: int = 1, attempt: int = 1,
          side: str = "up") -> EnterIntent:
    mkt = runtime.active_market.market
    instrument = mkt.yes if side == "up" else mkt.no
    return EnterIntent(
        intent_id=new_intent_id(), strategy_id=StrategyId("settlement_value"),
        instrument_id=instrument.instrument_id, market_id=mkt.market_id, created_at=NOW,
        correlation_id=CorrelationId("c"), causation_id=None, reason_code="EDGE_CLEARS_COST",
        target_notional=Decimal("4"), outcome=instrument.outcome, max_price=Decimal("0.35"),
        evidence={"episode_seq": episode, "attempt_seq": attempt,
                  **({"attempt_id": f"e{episode}a{attempt}"}
                     if episode > 1 or attempt > 1 else {})},
    )


def exit_intent(runtime: TradingRuntime, reason: str = "VALUE_EXIT") -> ExitIntent:
    mkt = runtime.active_market.market
    return ExitIntent(
        intent_id=new_intent_id(), strategy_id=StrategyId("settlement_value"),
        instrument_id=mkt.yes.instrument_id, market_id=mkt.market_id, created_at=NOW,
        correlation_id=CorrelationId("c"), causation_id=None, reason_code=reason,
        min_price=Decimal("0.40"),
    )


def hold_intent(runtime: TradingRuntime) -> HoldToResolutionIntent:
    mkt = runtime.active_market.market
    return HoldToResolutionIntent(
        intent_id=new_intent_id(), strategy_id=StrategyId("settlement_value"),
        instrument_id=mkt.yes.instrument_id, market_id=mkt.market_id, created_at=NOW,
        correlation_id=CorrelationId("c"), causation_id=None, reason_code="HOLD_TO_SETTLEMENT",
        window_id=runtime.active_market.window_id,
    )


async def close(runtime: TradingRuntime) -> None:
    await runtime.coordinator.close()
    runtime._release_state_lock()
    await asyncio.sleep(0)
