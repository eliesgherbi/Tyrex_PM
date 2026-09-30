"""Inspection stays off the execution client, and stale snapshots are not published."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.execution.coordinator import AccountExecutionCoordinator, DispatchBlocked
from tyrex_pm.execution.evidence import (
    ExecutionRole,
    ExternalPositionAdjustmentObserved,
    OrderPreDispatchFailed,
    OrderPrepared,
    OrderRequested,
    PreDispatchStage,
    SessionOpened,
    new_envelope,
)
from tyrex_pm.execution.orders import MarketBuyOrderSpec
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.execution.reducer import reduce_execution_event
from tyrex_pm.execution.session_state import ExecutionSessionState
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.reporting.run_report import RunReportInput, build_run_report
from tyrex_pm.runtime.trading_runtime import TradingRuntime

WALLET = "0x" + "ab" * 20
CONDITION = "0x" + "11" * 32


class _Page:
    def __init__(self, items, *, has_more: bool) -> None:
        self.items = items
        self.has_more = has_more


class _Pages:
    def __init__(self, items, *, has_more: bool = False) -> None:
        self.page = _Page(items, has_more=has_more)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        yield self.page


class _HoldPages:
    def __init__(self, items, entered: asyncio.Event, hold: asyncio.Event) -> None:
        self.items = items
        self.entered = entered
        self.hold = hold

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        self.entered.set()
        await self.hold.wait()
        yield _Page(self.items, has_more=False)


class _ExecutionClient:
    def __init__(self) -> None:
        self.created = 0
        self.posted = 0
        self.activity = 0

    async def create_market_order(self, **_kwargs):
        self.created += 1
        return {"signed": True}

    async def post_order(self, _signed):
        self.posted += 1
        raise AssertionError("inspection isolation must not POST")

    def list_activity(self, **_kwargs):
        self.activity += 1
        raise AssertionError("inspection must not read activity on the execution client")

    async def close(self) -> None:
        return None


class _InspectionClient:
    def __init__(self, *, activity=None, eth_call=None) -> None:
        self.wallet = WALLET
        self.activity_source = activity
        self.eth_call = eth_call
        self.position_reads = 0
        self.order_reads = 0
        self.activity_calls: list[dict] = []
        self.created = 0
        self.posted = 0

    def list_positions(self, **_kwargs):
        self.position_reads += 1
        if self.eth_call is None:
            return _Pages(())
        return _Pages(
            (
                {
                    "condition_id": CONDITION,
                    "token_id": "token",
                    "size": "7",
                    "outcome_index": 0,
                    "redeemable": False,
                },
            )
        )

    def list_open_orders(self, **_kwargs):
        self.order_reads += 1
        return _Pages(())

    def list_activity(self, **kwargs):
        self.activity_calls.append(dict(kwargs))
        if self.activity_source is not None:
            return self.activity_source
        return _Pages(())

    def list_markets(self, **_kwargs):
        state = SimpleNamespace(closed=True, accepting_orders=False, neg_risk=False)
        return _Pages((SimpleNamespace(state=state, resolution=None),))

    async def create_market_order(self, **_kwargs):
        self.created += 1
        raise AssertionError("inspection client must not sign")

    async def post_order(self, _signed):
        self.posted += 1
        raise AssertionError("inspection client must not POST")

    async def close(self) -> None:
        return None


def _buy() -> MarketBuyOrderSpec:
    return MarketBuyOrderSpec(
        order_id="entry-1",
        market_id="cond",
        instrument_id="token",
        token_id="token",
        spend_amount=Decimal("1"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.5"),
        metadata={"candidate_monotonic_ns": 1},
    )


async def _runtime(tmp_path, gateway):
    from dataclasses import replace

    from tyrex_pm.runtime.run_config import load_trading_run_config

    config = replace(
        load_trading_run_config(__import__("pathlib").Path("config/runs/z_gap_tiny_live.yaml")),
        state_directory=tmp_path / "state",
        report_directory=tmp_path / "runs",
    )
    runtime = await TradingRuntime.create(
        config=config,
        output_directory=tmp_path / "out",
        env={},
        gateway=gateway,
        live_requested=False,
    )
    runtime.gateway.tick_size_resolver = lambda _token: Decimal("0.01")
    runtime.gateway.tick_size_fetcher = None
    return runtime


async def _release(runtime) -> None:
    runtime._release_state_lock()
    await runtime.coordinator.close()


@pytest.mark.asyncio
async def test_paused_activity_pagination_does_not_block_preparation(tmp_path) -> None:
    entered = asyncio.Event()
    hold = asyncio.Event()
    execution = _ExecutionClient()
    inspection = _InspectionClient(activity=_HoldPages((), entered, hold))
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=inspection)
    runtime = await _runtime(tmp_path, gateway)
    assert runtime.gateway.client_guard is runtime.account_state_authority
    assert runtime.account_state_authority.refresh_status() is None
    try:
        await runtime.coordinator.apply(
            SessionOpened(
                **new_envelope(session_id="s1", dedupe_key="open"),
                strategy_id="ask70",
                market_id="cond",
                window_id="w",
                token_id="token",
            )
        )
        inspection_task = asyncio.create_task(
            runtime.gateway.startup_inspection(
                journal=runtime.coordinator.journal,
                sessions=(),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        with pytest.raises(DispatchBlocked):
            await asyncio.wait_for(
                runtime.coordinator.submit(
                    session_id="s1",
                    role=ExecutionRole.ENTRY,
                    spec=_buy(),
                ),
                timeout=1,
            )
        assert not hold.is_set()
        assert execution.created == 1
        assert execution.posted == 0
        assert execution.activity == 0
        assert inspection.created == 0
        assert inspection.posted == 0
        prepared = [
            event
            for event in runtime.coordinator.journal.load("s1")
            if isinstance(event, OrderPrepared)
        ]
        assert prepared
        hold.set()
        await inspection_task
    finally:
        hold.set()
        await _release(runtime)


@pytest.mark.asyncio
async def test_paused_payout_read_does_not_block_preparation(tmp_path) -> None:
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def eth_call(*, to, data):  # noqa: ARG001
        entered.set()
        await hold.wait()
        if data[2:10] == "dd34de67":
            return "0x" + "0" * 63 + "1"
        return "0x" + "0" * 63 + "1"

    execution = _ExecutionClient()
    inspection = _InspectionClient(eth_call=eth_call)
    gateway = PolymarketAsyncGateway(
        client=execution,
        inspection_client=inspection,
        payout_eth_call=eth_call,
    )
    runtime = await _runtime(tmp_path, gateway)
    try:
        await runtime.coordinator.apply(
            SessionOpened(
                **new_envelope(session_id="s1", dedupe_key="open"),
                strategy_id="ask70",
                market_id="cond",
                window_id="w",
                token_id="token",
            )
        )
        inspection_task = asyncio.create_task(
            runtime.gateway.startup_inspection(
                journal=runtime.coordinator.journal,
                sessions=(),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        with pytest.raises(DispatchBlocked):
            await asyncio.wait_for(
                runtime.coordinator.submit(
                    session_id="s1",
                    role=ExecutionRole.ENTRY,
                    spec=_buy(),
                ),
                timeout=1,
            )
        assert not hold.is_set()
        assert execution.created == 1
        assert execution.activity == 0
        assert inspection.created == 0
        hold.set()
        await inspection_task
    finally:
        hold.set()
        await _release(runtime)


@pytest.mark.asyncio
async def test_order_during_inspection_discards_stale_clearance(tmp_path) -> None:
    from tyrex_pm.execution.startup_reconciliation import (
        AccountIdentity,
        ActivityView,
        Holding,
        InspectionFacts,
        ReadCoverage,
    )

    entered = asyncio.Event()
    hold = asyncio.Event()
    execution = _ExecutionClient()
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=_InspectionClient())
    runtime = await _runtime(tmp_path, gateway)
    facts = InspectionFacts(
        identity=AccountIdentity(chain_id=137, environment="production", wallet=WALLET),
        coverage=ReadCoverage(
            complete=True,
            positions_complete=True,
            orders_complete=True,
            activity_complete=True,
        ),
        sessions=(
            _exposed_session(),
        ),
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
        bound_wallet=WALLET,
        closing_holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
        closing_open_orders=(),
        activity_ledger={"checkpoint": {"schema": 1}, "records": []},
    )

    async def startup_inspection(**_kwargs):
        entered.set()
        await hold.wait()
        return facts

    runtime.gateway.startup_inspection = startup_inspection
    runtime._mark_startup_fresh()
    deadline = runtime._assessment_valid_until
    try:
        assessment_task = asyncio.create_task(runtime._assess_startup(write_report=False))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await runtime.coordinator.apply(
            SessionOpened(
                **new_envelope(session_id="other", dedupe_key="other"),
                strategy_id="ask70",
                market_id="cond",
                window_id="w",
                token_id="other-token",
            )
        )
        hold.set()
        await assessment_task
        assert runtime._last_inspection_publication == "discarded"
        assert runtime._assessment_valid_until == deadline
        assert runtime.coordinator.journal.get_meta("account.activity.ledger") is None
        adjustments = [
            event
            for event in runtime.coordinator.journal.load("s-exposed")
            if isinstance(event, ExternalPositionAdjustmentObserved)
        ]
        assert adjustments == []
    finally:
        hold.set()
        await _release(runtime)


def _exposed_session():
    from tyrex_pm.execution.evidence import TradeStatus
    from tyrex_pm.execution.session_state import (
        ExecutionPhase,
        SessionIdentity,
        TradeRecord,
    )

    state = ExecutionSessionState(
        identity=SessionIdentity(
            session_id="s-exposed",
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        ),
        phase=ExecutionPhase.MANUAL_INTERVENTION,
        confirmed_position_shares=Decimal("7"),
    )
    state.trades["buy-1"] = TradeRecord(
        venue_trade_id="buy-1",
        venue_order_id="venue-buy",
        local_order_id="local-buy",
        token_id="token",
        side="BUY",
        shares=Decimal("7"),
        price=Decimal("0.50"),
        status=TradeStatus.CONFIRMED,
    )
    return state


@pytest.mark.asyncio
async def test_unresolved_attempt_withholds_publication(tmp_path) -> None:
    from tyrex_pm.execution.orders import MarketBuyOrderSpec as Buy
    from tyrex_pm.execution.session_state import OrderExecutionState, SessionIdentity
    from tyrex_pm.execution.startup_reconciliation import (
        AccountIdentity,
        InspectionFacts,
        ReadCoverage,
    )

    execution = _ExecutionClient()
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=_InspectionClient())
    runtime = await _runtime(tmp_path, gateway)
    inflight = ExecutionSessionState(
        identity=SessionIdentity(
            session_id="inflight",
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    inflight.orders["local"] = OrderExecutionState(
        role=ExecutionRole.ENTRY,
        spec=Buy(
            order_id="local",
            market_id="cond",
            instrument_id="token",
            token_id="token",
            spend_amount=Decimal("1"),
            maximum_total_debit=Decimal("5"),
            worst_price=Decimal("0.5"),
        ),
        attempt_ids=["attempt-1"],
        accepted=None,
    )
    runtime.coordinator.states["inflight"] = inflight

    async def startup_inspection(**_kwargs):
        return InspectionFacts(
            identity=AccountIdentity(chain_id=137, environment="production", wallet=WALLET),
            coverage=ReadCoverage(
                complete=True,
                positions_complete=True,
                orders_complete=True,
                activity_complete=True,
            ),
            sessions=(_exposed_session(),),
            bound_wallet=WALLET,
            closing_holdings=(),
            closing_open_orders=(),
            activity_ledger={"checkpoint": {"schema": 1}, "records": []},
        )

    runtime.gateway.startup_inspection = startup_inspection
    runtime._mark_startup_fresh()
    deadline = runtime._assessment_valid_until
    try:
        await runtime._assess_startup(write_report=False)
        assert runtime._last_inspection_publication == "withheld"
        assert runtime._assessment_valid_until == deadline
        assert runtime.coordinator.journal.load("s-exposed") == ()
        assert runtime.coordinator.journal.get_meta("account.activity.ledger") is None
    finally:
        await _release(runtime)


def test_owned_fill_is_not_external_inventory() -> None:
    from tyrex_pm.execution.evidence import TradeStatus
    from tyrex_pm.execution.session_state import ExecutionPhase, SessionIdentity, TradeRecord
    from tyrex_pm.execution.startup_reconciliation import (
        AccountIdentity,
        ActivityView,
        Holding,
        InspectionFacts,
        ReadCoverage,
        assess,
    )

    state = ExecutionSessionState(
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
    state.trades["owned-sell"] = TradeRecord(
        venue_trade_id="owned-sell",
        venue_order_id="venue-sell",
        local_order_id="local-sell",
        token_id="token",
        side="SELL",
        shares=Decimal("7"),
        price=Decimal("0.40"),
        status=TradeStatus.CONFIRMED,
    )
    state.trades["buy-1"] = TradeRecord(
        venue_trade_id="buy-1",
        venue_order_id="venue-buy",
        local_order_id="local-buy",
        token_id="token",
        side="BUY",
        shares=Decimal("7"),
        price=Decimal("0.50"),
        status=TradeStatus.CONFIRMED,
    )
    assessment = assess(
        InspectionFacts(
            identity=AccountIdentity(chain_id=137, environment="production", wallet=WALLET),
            coverage=ReadCoverage(
                complete=True,
                positions_complete=True,
                orders_complete=True,
                activity_complete=True,
            ),
            sessions=(state,),
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
            activities=(
                ActivityView(
                    activity_id="act-owned",
                    condition_id="cond",
                    token_id="token",
                    shares=Decimal("7"),
                    cause="sale",
                    side="SELL",
                    venue_trade_id="owned-sell",
                    transaction_hash="0x" + "11" * 32,
                ),
            ),
            bound_wallet=WALLET,
        )
    )
    assert all(issue.code != "EXTERNAL_DISPOSAL" for issue in assessment.issues)


@pytest.mark.asyncio
async def test_final_gate_rejection_keeps_prepare_timings() -> None:
    journal = MemoryExecutionJournal()
    async def _close() -> None:
        return None

    coordinator = AccountExecutionCoordinator(
        journal=journal,
        gateway=SimpleNamespace(close=_close),
        final_gate=lambda _spec: (True, None),
    )
    await coordinator.start()
    spec = _buy()
    requested = new_envelope(session_id="s1", dedupe_key="requested")
    requested["observed_monotonic_ns"] = 1_000
    prepared = new_envelope(session_id="s1", dedupe_key="prepared")
    prepared["observed_monotonic_ns"] = 2_000
    rejected = new_envelope(session_id="s1", dedupe_key="rejected")
    rejected["observed_monotonic_ns"] = 3_000
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
        OrderRequested(
            **requested,
            role=ExecutionRole.ENTRY,
            order=spec,
        )
    )
    await coordinator.apply(
        OrderPrepared(
            **prepared,
            role=ExecutionRole.ENTRY,
            order_id=spec.order_id,
            prepared_order_digest="digest",
            prepare_queue_wait_ms=17672.0,
            tick_resolve_ms=0.0,
            create_market_order_ms=0.0,
            refresh_wait_ms=0.0,
            refresh_elapsed_ms=None,
            order_metadata_cache_miss=True,
        )
    )
    await coordinator.apply(
        OrderPreDispatchFailed(
            **rejected,
            role=ExecutionRole.ENTRY,
            order_id=spec.order_id,
            stage=PreDispatchStage.FINAL_GATE,
            error_class="DispatchBlocked",
            error_code="FINAL_GATE_REJECTED",
            message="entry_price_moved",
        )
    )
    replayed = ExecutionSessionState()
    for event in journal.load("s1"):
        replayed, _effects = reduce_execution_event(replayed, event)
    order = replayed.orders[spec.order_id]
    assert order.prepare_queue_wait_ms == 17672.0
    assert order.tick_resolve_ms == 0.0
    assert order.create_market_order_ms == 0.0
    assert order.refresh_wait_ms == 0.0
    assert order.refresh_elapsed_ms is None
    assert order.order_metadata_cache_miss is True
    host = SimpleNamespace(coordinator=coordinator)
    timeline = TradingRuntime._build_execution_timeline(host, replayed)
    assert timeline[0]["prepare_queue_wait_ms"] == 17672.0
    assert timeline[0]["create_market_order_ms"] == 0.0
    assert timeline[0]["order_metadata_cache_miss"] is True
    report = build_run_report(
        RunReportInput(
            run_instance_id="run",
            configured_run_name="ask70",
            live_requested=True,
            state=replayed,
            mutation_attempts=0,
            execution_timeline=timeline,
            capabilities={},
            market_data=None,
            run_evidence=(),
            runtime_errors=(),
        )
    )
    reported = report["execution"]["orders"][0]
    raw = next(event for event in journal.load("s1") if isinstance(event, OrderPrepared))
    assert reported["prepare_queue_wait_ms"] == raw.prepare_queue_wait_ms
    assert reported["create_market_order_ms"] == raw.create_market_order_ms == 0.0
    assert report["execution_timeline"][0]["prepare_queue_wait_ms"] == 17672.0
    await coordinator.close()


@pytest.mark.asyncio
async def test_shutdown_closes_inspection_after_draining_abandoned_prepares() -> None:
    order: list[str] = []
    release = asyncio.Event()

    class _Closable:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail
            self.closed = False

        async def close(self) -> None:
            self.closed = True
            if self.fail:
                raise RuntimeError("execution close failed")
            order.append(self.name)

    async def abandoned() -> None:
        await release.wait()
        order.append("drain")

    execution = _Closable("execution")
    inspection = _Closable("inspection")
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=inspection)
    task = asyncio.create_task(abandoned())
    gateway._abandoned_prepares.add(task)
    release.set()
    await gateway.close()
    assert order == ["drain", "execution", "inspection"]
    assert inspection.closed

    execution_fail = _Closable("execution", fail=True)
    inspection_fail = _Closable("inspection")
    failing = PolymarketAsyncGateway(client=execution_fail, inspection_client=inspection_fail)
    with pytest.raises(RuntimeError, match="execution close failed"):
        await failing.close()
    assert inspection_fail.closed


@pytest.mark.asyncio
async def test_inspection_client_failure_closes_the_execution_client(monkeypatch) -> None:
    created: list[SimpleNamespace] = []

    async def build(env=None):  # noqa: ARG001
        client = SimpleNamespace(closed=False)

        async def close() -> None:
            client.closed = True

        client.close = close
        created.append(client)
        if len(created) == 2:
            raise RuntimeError("inspection client failed")
        return client

    monkeypatch.setattr(
        "tyrex_pm.execution.polymarket.gateway.build_async_secure_client",
        build,
    )
    with pytest.raises(RuntimeError, match="inspection client failed"):
        await PolymarketAsyncGateway.from_env({})
    assert created[0].closed is True


def _ledger() -> dict:
    return {"checkpoint": {"schema": 1, "coverage_through_unix": 1}, "records": []}


def _watch_timings(runtime) -> list[dict]:
    seen: list[dict] = []
    original = runtime.run_recorder.record

    def record(event: str, payload: dict) -> None:
        if event == "INSPECTION_TIMING":
            seen.append(dict(payload))
        original(event, payload)

    runtime.run_recorder.record = record
    return seen


def _coverage(*, complete: bool, positions: bool = True, orders: bool = True) -> object:
    from tyrex_pm.execution.startup_reconciliation import ReadCoverage

    return ReadCoverage(
        complete=complete,
        positions_complete=positions,
        orders_complete=orders,
        activity_complete=True,
    )


def _facts(**overrides):
    from tyrex_pm.execution.startup_reconciliation import AccountIdentity, InspectionFacts

    payload = {
        "identity": AccountIdentity(chain_id=137, environment="production", wallet=WALLET),
        "coverage": _coverage(complete=True),
        "bound_wallet": WALLET,
        "closing_holdings": (),
        "closing_open_orders": (),
        "activity_ledger": _ledger(),
    }
    payload.update(overrides)
    return InspectionFacts(**payload)


async def _publish(runtime, facts):
    async def startup_inspection(**_kwargs):
        return facts

    runtime.gateway.startup_inspection = startup_inspection
    await runtime._assess_startup(write_report=False)


@pytest.mark.asyncio
async def test_incomplete_or_inconsistent_evidence_does_not_advance_checkpoint(tmp_path) -> None:
    from tyrex_pm.execution.startup_reconciliation import Holding, OpenOrderView

    execution = _ExecutionClient()
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=_InspectionClient())
    runtime = await _runtime(tmp_path, gateway)
    timings = _watch_timings(runtime)
    order = OpenOrderView(
        venue_order_id="foreign",
        condition_id="cond",
        token_id="token",
        side="BUY",
    )
    cases = (
        _facts(coverage=_coverage(complete=False, orders=False)),
        _facts(coverage=_coverage(complete=False, positions=False)),
        _facts(closing_holdings=None, closing_open_orders=None),
        _facts(
            holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("1")),),
            closing_holdings=(Holding(condition_id="cond", token_id="token", shares=Decimal("0")),),
        ),
        _facts(open_orders=(order,), closing_open_orders=()),
    )
    expected = ("incomplete", "incomplete", "incomplete", "inconsistent", "inconsistent")
    try:
        for facts, evidence in zip(cases, expected, strict=True):
            await _publish(runtime, facts)
            timing = timings[-1]
            assert timing["execution"] == "accepted"
            assert timing["evidence"] == evidence
            assert timing["publication"] == evidence
            assert timing["checkpoint"] == "unchanged"
            assert timing["ready"] is False
            assert runtime._last_inspection_publication == evidence
            assert runtime.coordinator.journal.get_meta("account.activity.ledger") is None
    finally:
        await _release(runtime)


@pytest.mark.asyncio
async def test_complete_blocker_accepts_evidence_without_becoming_ready(tmp_path) -> None:
    from tyrex_pm.execution.startup_reconciliation import OpenOrderView

    execution = _ExecutionClient()
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=_InspectionClient())
    runtime = await _runtime(tmp_path, gateway)
    timings = _watch_timings(runtime)
    order = OpenOrderView(
        venue_order_id="foreign",
        condition_id="cond",
        token_id="token",
        side="BUY",
    )
    try:
        await _publish(
            runtime,
            _facts(open_orders=(order,), closing_open_orders=(order,)),
        )
        timing = timings[-1]
        assert timing["execution"] == "accepted"
        assert timing["evidence"] == "accepted"
        assert timing["publication"] == "accepted"
        assert timing["checkpoint"] == "advanced"
        assert timing["ready"] is False
        assert runtime.coordinator.journal.get_meta("account.activity.ledger") is not None
    finally:
        await _release(runtime)


@pytest.mark.asyncio
async def test_accepted_settlement_persists_evidence_before_checkpoint(tmp_path) -> None:
    from tyrex_pm.execution.evidence import MarketSettlementObserved
    from tyrex_pm.execution.session_state import ExecutionPhase, SessionIdentity
    from tyrex_pm.execution.startup_reconciliation import (
        Holding,
        MarketLifecycleObservation,
    )

    execution = _ExecutionClient()
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=_InspectionClient())
    runtime = await _runtime(tmp_path, gateway)
    timings = _watch_timings(runtime)
    await runtime.coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open-s1"),
            strategy_id="ask70",
            market_id="cond",
            window_id="w",
            token_id="token",
        )
    )
    state = runtime.coordinator.states["s1"]
    state.confirmed_position_shares = Decimal("7")
    state.phase = ExecutionPhase.MANUAL_INTERVENTION
    assert state.identity == SessionIdentity(
        session_id="s1",
        strategy_id="ask70",
        market_id="cond",
        window_id="w",
        token_id="token",
    )
    holding = Holding(condition_id="cond", token_id="token", shares=Decimal("7"))
    try:
        await _publish(
            runtime,
            _facts(
                sessions=(state,),
                holdings=(holding,),
                closing_holdings=(holding,),
                lifecycles={
                    "cond": MarketLifecycleObservation(
                        condition_id="cond",
                        status="RESOLVED_FINAL",
                        payout_per_share=Decimal("1"),
                        proof_supported=True,
                        reason="supported_finality",
                        token_payouts={"token": Decimal("1")},
                    )
                },
            ),
        )
        settlements = [
            event
            for event in runtime.coordinator.journal.load("s1")
            if isinstance(event, MarketSettlementObserved)
        ]
        assert len(settlements) == 1
        assert settlements[0].payout_per_share == Decimal("1")
        assert settlements[0].supported is True
        assert runtime.coordinator.journal.get_meta("account.activity.ledger") is not None
        timing = timings[-1]
        assert timing["publication"] == "accepted"
        assert timing["checkpoint"] == "advanced"
        assert timing["ready"] is True
    finally:
        await _release(runtime)


@pytest.mark.asyncio
async def test_foreign_event_during_publication_does_not_advance_checkpoint(tmp_path) -> None:
    execution = _ExecutionClient()
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=_InspectionClient())
    runtime = await _runtime(tmp_path, gateway)
    timings = _watch_timings(runtime)

    def event_ids_after(_sequence: int) -> tuple[str, ...]:
        return ("not-authored",)

    runtime.coordinator.journal.event_ids_after = event_ids_after
    try:
        await _publish(runtime, _facts())
        assert timings[-1]["execution"] == "accepted"
        assert timings[-1]["evidence"] == "accepted"
        assert timings[-1]["publication"] == "discarded"
        assert timings[-1]["checkpoint"] == "unchanged"
        assert timings[-1]["ready"] is False
        assert runtime.coordinator.journal.get_meta("account.activity.ledger") is None
    finally:
        await _release(runtime)


@pytest.mark.asyncio
async def test_publication_gate_keeps_a_concurrent_apply_behind_the_checkpoint(
    tmp_path, monkeypatch
) -> None:
    from tyrex_pm.runtime.startup_controller import StartupController

    execution = _ExecutionClient()
    gateway = PolymarketAsyncGateway(client=execution, inspection_client=_InspectionClient())
    runtime = await _runtime(tmp_path, gateway)
    order: list[str] = []
    started = asyncio.Event()
    original = StartupController.inspect

    async def wrapped(self, facts):
        started.set()
        await asyncio.sleep(0)
        result = await original(self, facts)
        order.append("inspect")
        return result

    monkeypatch.setattr(StartupController, "inspect", wrapped)

    async def intrude() -> None:
        await started.wait()
        await runtime.coordinator.apply(
            SessionOpened(
                **new_envelope(session_id="intruder", dedupe_key="intruder"),
                strategy_id="ask70",
                market_id="cond",
                window_id="w",
                token_id="other",
            )
        )
        order.append("intruder")

    intruder = asyncio.create_task(intrude())
    try:
        await asyncio.wait_for(_publish(runtime, _facts()), timeout=3)
        await asyncio.wait_for(intruder, timeout=3)
        assert order == ["inspect", "intruder"]
        assert runtime._last_inspection_publication == "accepted"
        assert runtime.coordinator.journal.get_meta("account.activity.ledger") is not None
        assert runtime.coordinator.journal.load("intruder")
    finally:
        intruder.cancel()
        await _release(runtime)


def test_authored_publication_requires_only_this_inspection_events() -> None:
    from tyrex_pm.execution.inspection_sync import authored_publication_holds

    authored = frozenset({"e1"})
    assert authored_publication_holds(
        before_unresolved=frozenset(),
        after_unresolved=frozenset(),
        authored_event_ids=authored,
        observed_event_ids=authored,
    )
    assert not authored_publication_holds(
        before_unresolved=frozenset(),
        after_unresolved=frozenset(),
        authored_event_ids=authored,
        observed_event_ids=frozenset({"e1", "foreign"}),
    )
    assert not authored_publication_holds(
        before_unresolved=frozenset(),
        after_unresolved=frozenset({"attempt-1"}),
        authored_event_ids=authored,
        observed_event_ids=authored,
    )
