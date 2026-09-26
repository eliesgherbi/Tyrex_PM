"""Production startup reads and the operator recovery path."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.adapters.polymarket.startup_reads import activity_from_sdk
from tyrex_pm.execution.coordinator import AccountExecutionCoordinator, DispatchBlocked
from tyrex_pm.execution.evidence import (
    ManualInterventionRequired,
    ReconciliationObserved,
    SessionOpened,
    new_envelope,
)
from tyrex_pm.execution.orders import MarketBuyOrderSpec, MarketSellOrderSpec
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.execution.startup_reconciliation import assess
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal, SqliteExecutionJournal
from tyrex_pm.runtime.market_data_runtime import MarketDataSummary

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
WALLET = "0x1111111111111111111111111111111111111111"


class _Page:
    def __init__(self, items, *, has_more: bool = False) -> None:
        self.items = tuple(items)
        self.has_more = has_more
        self.next_cursor = "next" if has_more else None


class _Pages:
    def __init__(self, items, *, has_more: bool = False) -> None:
        self.page = _Page(items, has_more=has_more)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        yield self.page


class _Guard:
    def __init__(self) -> None:
        self.acquires = 0

    async def acquire_sdk_slot(self, *, active: bool):
        self.acquires += 1
        return self

    async def release(self) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self._ctx = SimpleNamespace(wallet=WALLET)
        self.position_size = Decimal("0")
        self.hidden_balance = 7_000_000
        self.order_status = "CANCELED"
        self.activity = ()
        self.positions_truncated = False
        self.closing_size = None
        self.condition_id = "cond"
        self.outcome_index = None
        self.market_closed = False
        self.neg_risk = False
        self.position_sizes = None
        self.closing_orders = ()
        self.accepting_sequence = None
        self._order_reads = 0
        self.order_book = None
        self.book_reads = 0

    def list_positions(self, **_kwargs):
        if self.position_sizes:
            size = self.position_sizes.pop(0)
        else:
            size = self.position_size
            if self.closing_size is not None and getattr(self, "_opened", False):
                size = self.closing_size
            self._opened = True
        return _Pages(
            ()
            if size == 0
            else (
                SimpleNamespace(
                    condition_id=self.condition_id,
                    token_id="token",
                    size=size,
                    redeemable=False,
                    outcome_index=self.outcome_index,
                ),
            ),
            has_more=self.positions_truncated,
        )

    def list_open_orders(self):
        self._order_reads += 1
        if self._order_reads > 1 and self.closing_orders:
            return _Pages(self.closing_orders)
        return _Pages(())

    def list_activity(self):
        return _Pages(self.activity)

    def list_markets(self, **_kwargs):
        accepting = not self.market_closed
        if self.accepting_sequence:
            accepting = self.accepting_sequence.pop(0)
        state = SimpleNamespace(
            closed=not accepting,
            accepting_orders=accepting,
            neg_risk=self.neg_risk,
        )
        return _Pages((SimpleNamespace(state=state, resolution=None),))

    async def get_order(self, *, order_id: str):
        return SimpleNamespace(id=order_id, status=self.order_status, size_matched="0")

    async def get_balance_allowance(self, *, asset_type: str, token_id: str):
        assert asset_type == "CONDITIONAL"
        assert token_id == "token"
        return SimpleNamespace(balance=self.hidden_balance, allowances={})

    async def get_order_book(self, *, token_id: str):
        assert token_id == "token"
        self.book_reads += 1
        if self.order_book is None:
            raise AssertionError("order book was not configured")
        return self.order_book

    async def close(self) -> None:
        return None


def test_sdk_trade_activity_keeps_shares_distinct_from_the_transaction() -> None:
    from polymarket.models.data.activity import TradeActivity

    item = TradeActivity.model_validate(
        {
            "proxyWallet": WALLET,
            "timestamp": 1_700_000_000,
            "transactionHash": "0x" + "ab" * 32,
            "type": "TRADE",
            "conditionId": "0x" + "cd" * 32,
            "asset": (
                "62309964239662605377457060000611795939595538068012823038251470731929773764268"
            ),
            "side": "SELL",
            "size": "7",
            "amount": "3",
            "price": "0.4",
            "outcome": "Yes",
            "outcomeIndex": 0,
            "title": "Example",
            "slug": "example",
            "eventSlug": "example-event",
        }
    )
    viewed = activity_from_sdk(item)
    assert viewed.shares == Decimal("7")
    assert viewed.side == "SELL"
    assert viewed.transaction_hash.startswith("0xab")
    assert viewed.venue_trade_id is None
    assert viewed.activity_id != viewed.transaction_hash


@pytest.mark.asyncio
async def test_production_reader_checks_identity_orders_and_missing_tokens() -> None:
    from tyrex_pm.execution.evidence import ExecutionRole
    from tyrex_pm.execution.orders import MarketSellOrderSpec
    from tyrex_pm.execution.session_state import (
        ExecutionSessionState,
        OrderExecutionState,
        SessionIdentity,
    )

    journal = MemoryExecutionJournal()
    journal.set_meta("account.wallet", "0xother")
    session = ExecutionSessionState(
        identity=SessionIdentity(
            session_id="s1",
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    session.orders["local"] = OrderExecutionState(
        role=ExecutionRole.ENTRY,
        spec=MarketSellOrderSpec(
            order_id="local",
            market_id="cond",
            instrument_id="token",
            token_id="token",
            shares=Decimal("1"),
            minimum_price=Decimal("0.01"),
        ),
        venue_order_id="venue-1",
        ambiguous=True,
    )
    client = _Client()
    guard = _Guard()
    gateway = PolymarketAsyncGateway(client=client)
    gateway.client_guard = guard
    facts = await gateway.startup_inspection(journal=journal, sessions=(session,))
    assert guard.acquires == 1
    assert facts.bound_wallet == "0xother"
    assert facts.identity is not None and facts.identity.wallet == WALLET
    blocked = assess(facts)
    assert any(issue.code == "WALLET_MISMATCH" for issue in blocked.issues)
    assert facts.order_lookups["venue-1"].status == "CANCELED"
    assert client.hidden_balance == 7_000_000
    matched = [
        holding
        for holding in facts.holdings
        if holding.token_id == "token" and holding.shares == Decimal("7")
    ]
    assert matched
    assert facts.lifecycles["cond"].status == "TRADABLE"
    assert facts.lifecycles["cond"].proof_supported is False
    assert journal.get_meta("account.wallet") == "0xother"


@pytest.mark.asyncio
async def test_truncated_pages_and_legacy_association_do_not_clear(tmp_path) -> None:
    from tyrex_pm.adapters.polymarket.startup_reads import associate_legacy_journal
    from tyrex_pm.execution.session_state import (
        ExecutionPhase,
        ExecutionSessionState,
        SessionIdentity,
    )

    client = _Client()
    client.positions_truncated = True
    gateway = PolymarketAsyncGateway(client=client)
    truncated = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert truncated.coverage.complete is False
    assert "pagination" in truncated.coverage.reason

    journal = SqliteExecutionJournal(tmp_path / "execution.sqlite3")
    session = ExecutionSessionState(
        identity=SessionIdentity(
            session_id="s1",
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        ),
        phase=ExecutionPhase.MANUAL_INTERVENTION,
        confirmed_position_shares=Decimal("7"),
    )
    unbound = await gateway.startup_inspection(journal=journal, sessions=(session,))
    assert unbound.legacy_unbound is True
    assert journal.get_meta("account.wallet") is None
    associate_legacy_journal(journal, WALLET, at=NOW)
    associated = await gateway.startup_inspection(journal=journal, sessions=(session,))
    assert associated.legacy_unbound is False
    assert associated.bound_wallet == WALLET
    assert session.confirmed_position_shares == Decimal("7")
    assert session.operational_disposition.value == "UNRESOLVED"
    journal.close()


@pytest.mark.asyncio
async def test_closing_read_rejects_a_moved_balance() -> None:
    client = _Client()
    client.position_size = Decimal("7")
    client.closing_size = Decimal("3")
    client.hidden_balance = Decimal("0")
    gateway = PolymarketAsyncGateway(client=client)
    facts = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert facts.closing_holdings is not None
    assessment = assess(facts)
    assert any(issue.code == "INVENTORY_CHANGED_DURING_ASSESSMENT" for issue in assessment.issues)


def _book():
    leg = SimpleNamespace(
        token_id="token",
        sync_health=SyncHealth.READY,
        data_age_ms=10,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        quote=SimpleNamespace(
            best_ask=Decimal("0.50"),
            best_bid=Decimal("0.40"),
            ask_size_at_touch=Decimal("100"),
        ),
    )
    return SimpleNamespace(binding_id="old-binding", up=leg, down=SimpleNamespace(token_id="no"))


class _RecoveryVenue:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.balance = Decimal("7")
        self.client = _Client()

    async def startup_inspection(self, *, journal=None, sessions=()):  # noqa: ANN001
        from tyrex_pm.execution.startup_reconciliation import (
            AccountIdentity,
            Holding,
            InspectionFacts,
            MarketLifecycleObservation,
            ReadCoverage,
        )

        return InspectionFacts(
            identity=AccountIdentity(chain_id=137, environment="production", wallet=WALLET),
            coverage=ReadCoverage(
                complete=True,
                positions_complete=True,
                orders_complete=True,
                activity_complete=True,
            ),
            holdings=(
                Holding(condition_id="cond", token_id="token", shares=self.balance),
            ),
            bound_wallet=WALLET,
            lifecycles={
                "cond": MarketLifecycleObservation(
                    condition_id="cond", status="TRADABLE", proof_supported=False
                )
            },
        )

    async def recovery_book(self, condition_id: str, token_id: str):
        assert condition_id == "cond"
        assert token_id == "token"
        return _book()

    async def prepare_order(self, spec):  # noqa: ANN001
        from tyrex_pm.execution.coordinator import PreparedOrder

        return PreparedOrder(spec.order_id, spec.order_id, spec)

    async def post_order(self, prepared):  # noqa: ANN001
        self.posts.append(prepared.payload.side.value)
        self.balance = Decimal("0")
        from tyrex_pm.execution.coordinator import GatewaySubmissionResult

        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id="venue-exit",
            status="matched",
            cumulative_matched_shares=prepared.payload.shares,
            trade_ids=("trade-exit",),
        )

    async def discard_prepared(self, _prepared) -> None:
        return None

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        from tyrex_pm.execution.cancel import cancel_result_from_sdk

        return cancel_result_from_sdk({"canceled": list(order_ids), "not_canceled": {}})

    async def list_open_orders(self, **_kwargs):
        return ()

    async def list_account_trades(self, **_kwargs):
        return (
            SimpleNamespace(
                id="trade-exit",
                taker_order_id="venue-exit",
                side="SELL",
                size=Decimal("7"),
                price=Decimal("0.40"),
                asset_id="token",
                status="CONFIRMED",
            ),
        )

    async def conditional_balance(self, _token_id):
        return self.balance, Decimal("100")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_live_run_recovers_then_starts_strategy(tmp_path, monkeypatch) -> None:
    from tyrex_pm.runtime.run_config import load_trading_run_config
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    journal = SqliteExecutionJournal(state_dir / "execution.sqlite3")
    coordinator = AccountExecutionCoordinator(
        journal=journal,
        gateway=_RecoveryVenue(),
        final_gate=lambda _spec: (True, None),
    )
    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="old", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    await coordinator.apply(
        ReconciliationObserved(
            **new_envelope(session_id="old", dedupe_key="pos"),
            source="test",
            complete=True,
            confirmed_position_shares=Decimal("7"),
            sellable_shares=Decimal("7"),
            open_order_ids=(),
        )
    )
    await coordinator.apply(
        ManualInterventionRequired(
            **new_envelope(session_id="old", dedupe_key="manual"),
            reason="restored",
        )
    )
    journal.set_meta("account.wallet", WALLET)
    await coordinator.close()
    journal.close()

    started: list[str] = []

    async def _market_data(**kwargs):  # noqa: ANN003
        started.append("strategy")
        return MarketDataSummary(
            run_id=str(kwargs["run_id"]),
            out_dir=str(kwargs["out_dir"]),
            started_at=NOW.isoformat(),
            finished_at=NOW.isoformat(),
        )

    monkeypatch.setattr(
        "tyrex_pm.runtime.trading_runtime.run_market_data_runtime", _market_data
    )
    config = load_trading_run_config(__import__("pathlib").Path("config/runs/z_gap_tiny_live.yaml"))
    from dataclasses import replace

    config = replace(config, state_directory=state_dir, report_directory=tmp_path / "runs")
    venue = _RecoveryVenue()

    async def _approve(_prompt: str) -> str:
        return "approve"

    runtime = await TradingRuntime.create(
        config=config,
        output_directory=tmp_path / "out",
        env={},
        gateway=venue,
    )
    runtime.decision_reader = _approve
    result = await runtime.run()
    assert venue.posts == ["SELL"]
    assert started == ["strategy"]
    assert result.outcome != "STARTUP_ACTION_REQUIRED"
    report = (tmp_path / "out" / "run_summary.json").read_text(encoding="utf-8")
    assert "old" in report
    assert runtime._startup_monitor is None


@pytest.mark.asyncio
async def test_readiness_expires_at_the_buy_gate_and_the_monitor_stops(tmp_path) -> None:
    from dataclasses import replace

    from tyrex_pm.runtime.run_config import load_trading_run_config
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    config = replace(
        load_trading_run_config(__import__("pathlib").Path("config/runs/z_gap_tiny_live.yaml")),
        state_directory=tmp_path / "state",
        report_directory=tmp_path / "runs",
        startup=replace(
            load_trading_run_config(
                __import__("pathlib").Path("config/runs/z_gap_tiny_live.yaml")
            ).startup,
            monitor_interval_s=0.01,
            inspection_budget_s=0.05,
        ),
    )
    venue = _RecoveryVenue()
    venue.balance = Decimal("0")
    runtime = await TradingRuntime.create(
        config=config,
        output_directory=tmp_path / "out",
        env={},
        gateway=venue,
        live_requested=False,
    )
    inspected = await runtime._assess_startup()
    assert inspected.ready
    report = (tmp_path / "out" / "startup_report.json").read_text(encoding="utf-8")
    assert '"live_requested": false' in report
    runtime.capabilities.live_requested = True
    runtime._assessment_valid_until = NOW
    runtime._assessment_generation = runtime.run_instance_id
    runtime.time_authority = SimpleNamespace(now_corrected_utc=lambda: NOW + timedelta(seconds=20))
    buy = MarketBuyOrderSpec(
        order_id="buy",
        market_id="cond",
        instrument_id="token",
        token_id="token",
        spend_amount=Decimal("1"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.5"),
        metadata={"candidate_monotonic_ns": 1},
    )
    allowed, reason = await runtime.final_gate(buy)
    assert allowed is False
    assert reason == "STARTUP_ASSESSMENT_EXPIRED"
    sell = MarketSellOrderSpec(
        order_id="sell",
        market_id="cond",
        instrument_id="token",
        token_id="token",
        shares=Decimal("1"),
        minimum_price=Decimal("0.01"),
        metadata={"candidate_monotonic_ns": 1},
    )
    _allowed, sell_reason = await runtime.final_gate(sell)
    assert sell_reason != "STARTUP_ASSESSMENT_EXPIRED"
    runtime._mark_startup_fresh()
    runtime._startup_invalidated_reason = None
    runtime.time_authority = SimpleNamespace(now_corrected_utc=lambda: datetime.now(timezone.utc))
    runtime._startup_monitor = asyncio.create_task(runtime._startup_monitor_loop())
    await asyncio.sleep(0.05)
    assert runtime._assessment_valid_until is not None
    venue.startup_inspection = _fail_inspection
    await asyncio.sleep(0.05)
    assert runtime._startup_invalidated_reason == "STARTUP_RECHECK_FAILED"
    await runtime._stop_startup_monitor()
    assert runtime._startup_monitor is None
    runtime._release_state_lock()
    await runtime.coordinator.close()


async def _fail_inspection(**_kwargs):
    raise RuntimeError("refresh failed")


@pytest.mark.asyncio
async def test_inspection_budget_does_not_become_an_empty_account(tmp_path) -> None:
    from dataclasses import replace

    from tyrex_pm.runtime.run_config import load_trading_run_config
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    config = replace(
        load_trading_run_config(__import__("pathlib").Path("config/runs/z_gap_tiny_live.yaml")),
        state_directory=tmp_path / "state",
        report_directory=tmp_path / "runs",
        startup=replace(
            load_trading_run_config(
                __import__("pathlib").Path("config/runs/z_gap_tiny_live.yaml")
            ).startup,
            inspection_budget_s=0.05,
        ),
    )
    venue = _RecoveryVenue()

    async def _stall(**_kwargs):
        await asyncio.sleep(1)
        raise AssertionError("the budget should have cancelled this read")

    venue.startup_inspection = _stall
    runtime = await TradingRuntime.create(
        config=config,
        output_directory=tmp_path / "out",
        env={},
        gateway=venue,
    )
    facts = await runtime._read_startup_facts()
    assert facts is None
    assessment = await runtime._assess_startup()
    assert assessment.status.value == "BLOCKED"
    assert any(issue.code == "ACCOUNT_VISIBILITY_INCOMPLETE" for issue in assessment.issues)
    runtime._release_state_lock()
    await runtime.coordinator.close()


@pytest.mark.asyncio
async def test_historical_cancel_rejects_a_foreign_order_and_a_repeat(tmp_path) -> None:
    from tyrex_pm.execution.session_state import ExecutionSessionState, SessionIdentity
    from tyrex_pm.execution.startup_reconciliation import RecoveryProposal
    from tyrex_pm.runtime.startup_controller import StartupController

    journal = MemoryExecutionJournal()

    async def _allow(_spec):
        return True, None

    coordinator = AccountExecutionCoordinator(
        journal=journal, gateway=_RecoveryVenue(), final_gate=_allow
    )
    state = ExecutionSessionState(
        identity=SessionIdentity(
            session_id="old",
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    coordinator.states["old"] = state
    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="old", dedupe_key="open"),
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    await coordinator.apply(
        ReconciliationObserved(
            **new_envelope(session_id="old", dedupe_key="pos"),
            source="test",
            complete=True,
            confirmed_position_shares=Decimal("7"),
            sellable_shares=Decimal("7"),
            open_order_ids=(),
        )
    )
    await coordinator.apply(
        ManualInterventionRequired(
            **new_envelope(session_id="old", dedupe_key="manual"),
            reason="restored",
        )
    )
    controller = StartupController(coordinator=coordinator, now=lambda: NOW, generation="gen")
    proposal = RecoveryProposal(
        issue_id="cancel:old",
        session_id="old",
        condition_id="cond",
        token_id="token",
        action="cancel",
        max_shares=Decimal("0"),
        min_price=Decimal("0.01"),
        venue_order_ids=("owned-order",),
    )
    await controller.authorize(proposal, approved=True, live=True)
    coordinator.recovery_now = lambda: NOW
    with pytest.raises(DispatchBlocked, match="cancel_order_not_owned"):
        await coordinator.cancel(
            session_id="old",
            venue_order_ids=("foreign-order",),
            reason="test",
        )
    await controller.finish(proposal, "done")
    with pytest.raises(DispatchBlocked, match="historical_recovery_not_authorized"):
        await coordinator.submit(
            session_id="old",
            role=__import__(
                "tyrex_pm.execution.evidence", fromlist=["ExecutionRole"]
            ).ExecutionRole.EXIT,
            spec=MarketSellOrderSpec(
                order_id="sell",
                market_id="cond",
                instrument_id="token",
                token_id="token",
                shares=Decimal("1"),
                minimum_price=Decimal("0.2"),
            ),
            historical_recovery=True,
        )
    await coordinator.close()


def _book_payload(*, bid: str = "0.40", minimum: str = "5"):
    return SimpleNamespace(
        market="cond",
        condition_id="cond",
        token_id="token",
        timestamp=str(int(NOW.timestamp() * 1000)),
        bids=(SimpleNamespace(price=bid, size="100"),),
        asks=(SimpleNamespace(price="0.50", size="80"),),
        min_order_size=minimum,
        tick_size="0.01",
        hash="book-hash",
        neg_risk=False,
        last_trade_price=bid,
    )


async def _seed_manual_journal(state_dir, *, bind_wallet: bool) -> None:
    state_dir.mkdir()
    journal = SqliteExecutionJournal(state_dir / "execution.sqlite3")
    coordinator = AccountExecutionCoordinator(
        journal=journal,
        gateway=_RecoveryVenue(),
        final_gate=lambda _spec: (True, None),
    )
    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="old", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    await coordinator.apply(
        ReconciliationObserved(
            **new_envelope(session_id="old", dedupe_key="pos"),
            source="test",
            complete=True,
            confirmed_position_shares=Decimal("7"),
            sellable_shares=Decimal("7"),
            open_order_ids=(),
        )
    )
    await coordinator.apply(
        ManualInterventionRequired(
            **new_envelope(session_id="old", dedupe_key="manual"),
            reason="restored",
        )
    )
    if bind_wallet:
        journal.set_meta("account.wallet", WALLET)
    await coordinator.close()


def _runtime_config(tmp_path):
    from dataclasses import replace
    from pathlib import Path

    from tyrex_pm.runtime.run_config import load_trading_run_config

    return replace(
        load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml")),
        state_directory=tmp_path / "state",
        report_directory=tmp_path / "runs",
    )


@pytest.mark.asyncio
async def test_conditional_balance_micros_are_shares() -> None:
    from polymarket.models.clob.account import BalanceAllowance

    from tyrex_pm.execution.session_state import ExecutionSessionState, SessionIdentity

    client = _Client()

    async def _allowance(*, asset_type: str, token_id: str):
        assert asset_type == "CONDITIONAL"
        assert token_id == "token"
        return BalanceAllowance(balance=7_000_000, allowances={"0xspend": 7_000_000})

    client.get_balance_allowance = _allowance
    gateway = PolymarketAsyncGateway(client=client)
    session = ExecutionSessionState(
        identity=SessionIdentity(
            session_id="s1",
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    facts = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=(session,))
    assert facts.holdings[0].shares == Decimal("7")
    assert facts.closing_holdings is not None
    assert facts.closing_holdings[0].shares == Decimal("7")


@pytest.mark.asyncio
async def test_closing_buy_blocks_ready_on_the_production_reader() -> None:
    from tyrex_pm.runtime.startup_controller import StartupController

    client = _Client()
    client.closing_orders = (
        SimpleNamespace(
            id="new-buy",
            condition_id="cond",
            token_id="token",
            side="BUY",
            original_size="7",
            size_matched="0",
        ),
    )
    gateway = PolymarketAsyncGateway(client=client)
    facts = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert facts.open_orders == ()
    assert facts.closing_open_orders is not None
    assert facts.closing_open_orders[0].venue_order_id == "new-buy"
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=gateway,
        final_gate=lambda _spec: (True, None),
    )
    checked = await StartupController(coordinator=coordinator, generation="g").inspect(facts)
    assert checked.status.value == "BLOCKED"
    assert any(issue.code == "INVENTORY_CHANGED_DURING_ASSESSMENT" for issue in checked.issues)


@pytest.mark.asyncio
async def test_running_session_is_not_foreign_inventory(tmp_path) -> None:
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    venue = _RecoveryVenue()
    venue.balance = Decimal("7")
    runtime = await TradingRuntime.create(
        config=_runtime_config(tmp_path),
        output_directory=tmp_path / "out",
        env={},
        gateway=venue,
        live_requested=False,
    )
    await runtime.coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="live", dedupe_key="open-live"),
            strategy_id="z_gap",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    assessment = await runtime._assess_startup()
    codes = {issue.code for issue in assessment.issues}
    assert "EXTERNAL_INVENTORY" not in codes
    assert "UNEXPECTED_HOLDING" in codes
    runtime._release_state_lock()
    await runtime.coordinator.close()


@pytest.mark.asyncio
async def test_production_gateway_recovery_book_uses_sdk_order_book() -> None:
    from polymarket.models.clob.order_book import OrderBook

    client = _Client()
    client.order_book = OrderBook.model_validate(
        {
            "market": "cond",
            "condition_id": "cond",
            "token_id": "token",
            "timestamp": str(int(NOW.timestamp() * 1000)),
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "80"}],
            "min_order_size": "5",
            "tick_size": "0.01",
            "neg_risk": False,
            "last_trade_price": "0.40",
            "hash": "book-hash",
        }
    )
    gateway = PolymarketAsyncGateway(client=client)
    view = await gateway.recovery_book("cond", "token")
    assert view is not None
    assert view.up.token_id == "token"
    assert view.up.quote.best_bid == Decimal("0.40")
    assert view.up.min_order_size == Decimal("5")
    assert view.up.tick_size == Decimal("0.01")
    mismatched = await gateway.recovery_book("other-market", "token")
    assert mismatched is None


@pytest.mark.asyncio
async def test_post_approval_reread_refuses_a_larger_balance(
    tmp_path, monkeypatch
) -> None:
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    async def _market_data(**_kwargs):
        raise AssertionError("strategy must not start")

    monkeypatch.setattr("tyrex_pm.runtime.trading_runtime.run_market_data_runtime", _market_data)
    await _seed_manual_journal(tmp_path / "state", bind_wallet=True)
    client = _Client()
    client.position_sizes = [Decimal("7"), Decimal("7"), Decimal("9"), Decimal("9")]
    client.order_book = _book_payload()
    gateway = PolymarketAsyncGateway(client=client)
    posts: list[str] = []

    async def _post(_prepared):
        posts.append("SELL")
        raise AssertionError("recovery posted after the balance grew")

    gateway.post_order = _post
    runtime = await TradingRuntime.create(
        config=_runtime_config(tmp_path),
        output_directory=tmp_path / "out",
        env={},
        gateway=gateway,
    )

    async def _approve(_prompt: str) -> str:
        return "approve"

    runtime.decision_reader = _approve
    result = await runtime.run()
    assert posts == []
    assert client.position_sizes == []
    assert result.outcome != "COMPLETED"
    assert runtime._strategy_started is False


@pytest.mark.asyncio
async def test_post_approval_reread_refuses_a_bid_below_the_floor(tmp_path, monkeypatch) -> None:
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    async def _market_data(**_kwargs):
        raise AssertionError("strategy must not start")

    monkeypatch.setattr("tyrex_pm.runtime.trading_runtime.run_market_data_runtime", _market_data)
    await _seed_manual_journal(tmp_path / "state", bind_wallet=True)
    client = _Client()
    client.position_size = Decimal("7")
    client.order_book = _book_payload(bid="0.001")
    gateway = PolymarketAsyncGateway(client=client)
    posts: list[str] = []

    async def _post(_prepared):
        posts.append("SELL")
        raise AssertionError("recovery posted below the price floor")

    gateway.post_order = _post
    runtime = await TradingRuntime.create(
        config=_runtime_config(tmp_path),
        output_directory=tmp_path / "out",
        env={},
        gateway=gateway,
    )

    async def _approve(_prompt: str) -> str:
        return "approve"

    runtime.decision_reader = _approve
    await runtime.run()
    assert posts == []
    assert client.book_reads == 1
    assert runtime._strategy_started is False


@pytest.mark.asyncio
async def test_associate_records_provenance_and_does_not_trade(tmp_path, monkeypatch) -> None:
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    async def _market_data(**_kwargs):
        raise AssertionError("strategy must not start")

    monkeypatch.setattr("tyrex_pm.runtime.trading_runtime.run_market_data_runtime", _market_data)
    await _seed_manual_journal(tmp_path / "state", bind_wallet=False)
    client = _Client()
    client.position_size = Decimal("7")
    gateway = PolymarketAsyncGateway(client=client)
    posts: list[str] = []

    async def _post(_prepared):
        posts.append("SELL")
        raise AssertionError("association must not place an order")

    gateway.post_order = _post
    runtime = await TradingRuntime.create(
        config=_runtime_config(tmp_path),
        output_directory=tmp_path / "out",
        env={},
        gateway=gateway,
    )

    async def _associate(_prompt: str) -> str:
        return "associate"

    runtime.decision_reader = _associate
    await runtime.run()
    assert posts == []
    journal = SqliteExecutionJournal(tmp_path / "state" / "execution.sqlite3")
    assert journal.get_meta("account.wallet") == WALLET
    assert journal.get_meta("account.association") == "operator"
    assert journal.load("old")
    journal.close()


@pytest.mark.asyncio
async def test_ctf_payout_marks_standard_binary_final_and_skips_neg_risk() -> None:
    from dataclasses import replace

    from tyrex_pm.adapters.polymarket.ctf_payout import CONDITIONAL_TOKENS_ADDRESS
    from tyrex_pm.execution.session_state import (
        ExecutionPhase,
        ExecutionSessionState,
        SessionIdentity,
    )

    condition = "0x" + "11" * 32
    calls: list[str] = []

    async def eth_call(*, to, data):
        assert to == CONDITIONAL_TOKENS_ADDRESS
        calls.append(data)
        if data[2:10] == "dd34de67":
            return "0x" + "0" * 63 + "1"
        index = int(data[-64:], 16)
        return "0x" + format(1 if index == 0 else 0, "064x")

    client = _Client()
    client.condition_id = condition
    client.position_size = Decimal("7")
    client.outcome_index = 0
    client.market_closed = True
    gateway = PolymarketAsyncGateway(client=client, payout_eth_call=eth_call)
    session = ExecutionSessionState(
        identity=SessionIdentity(
            session_id="s1",
            strategy_id="ask70",
            market_id=condition,
            window_id="w",
            token_id="token",
        ),
        phase=ExecutionPhase.MANUAL_INTERVENTION,
        confirmed_position_shares=Decimal("7"),
    )
    facts = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=(session,))
    assessed = assess(replace(facts, sessions=(session,)))
    assert facts.lifecycles[condition].status == "RESOLVED_FINAL"
    assert facts.lifecycles[condition].proof_supported is True
    assert facts.lifecycles[condition].token_payouts["token"] == Decimal("1")
    assert any(issue.code == "SETTLEMENT_HOLDING" for issue in assessed.issues)
    assert calls

    async def _refuse(**_kwargs):
        raise AssertionError("neg-risk must not call the standard CTF")

    client.neg_risk = True
    client._opened = False
    gateway.payout_eth_call = _refuse
    neg = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=(session,))
    assert neg.lifecycles[condition].status == "UNKNOWN"
    assert neg.lifecycles[condition].proof_supported is False


@pytest.mark.asyncio
async def test_position_negative_risk_flag_blocks_without_a_payout() -> None:
    client = _Client()
    client.position_size = Decimal("7")
    client.outcome_index = 0
    client.market_closed = True

    async def eth_call(*, to, data):
        raise AssertionError("an unproven neg-risk token must not be paid from CTF")

    original = client.list_positions

    def list_positions(**kwargs):
        page = original(**kwargs)
        items = []
        for item in page.page.items:
            items.append(
                SimpleNamespace(
                    condition_id=item.condition_id,
                    token_id=item.token_id,
                    size=item.size,
                    redeemable=False,
                    outcome_index=0,
                    negative_risk=True,
                )
            )
        return _Pages(items)

    client.list_positions = list_positions
    gateway = PolymarketAsyncGateway(client=client, payout_eth_call=eth_call)
    facts = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert facts.lifecycles["cond"].status == "UNKNOWN"
    assert facts.lifecycles["cond"].proof_supported is False
    assert facts.holdings[0].negative_risk is True
