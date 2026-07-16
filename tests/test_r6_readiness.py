"""R6 execution readiness prerequisites."""

from __future__ import annotations

from tyrex_pm.execution.polymarket.readiness import ExecutionReadiness, ReadinessReason


def test_ready_only_when_no_blocking_reasons() -> None:
    r = ExecutionReadiness()
    r.deny(ReadinessReason.CREDENTIALS_MISSING)
    assert not r.ready
    r.clear(ReadinessReason.CREDENTIALS_MISSING)
    assert r.ready


def test_user_stream_disconnect_removes_readiness() -> None:
    from datetime import datetime, timezone
    from decimal import Decimal

    from tyrex_pm.core.ids import MarketId
    from tyrex_pm.engine.dispatcher import EventDispatcher
    from tyrex_pm.execution.fill_ledger import FillLedger
    from tyrex_pm.execution.order_store import OrderStore
    from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
    from tyrex_pm.execution.polymarket.live_oms import LiveOMS
    from tyrex_pm.portfolio.portfolio import Portfolio

    disp = EventDispatcher()
    orders = OrderStore()
    ledger = FillLedger()
    portfolio = Portfolio(fill_ledger=ledger, market_id=MarketId("m1"))
    orders.attach(disp)
    transport = FakeTransport()
    oms = LiveOMS(
        transport=transport,
        dispatcher=disp,
        order_store=orders,
        portfolio=portfolio,
        mutations_enabled=False,
    )
    oms.readiness.clear(ReadinessReason.MUTATIONS_DISABLED)
    oms.mark_user_stream(True)
    assert ReadinessReason.USER_STREAM_UNREADY not in oms.readiness.reasons
    oms.mark_user_stream(False)
    assert ReadinessReason.USER_STREAM_UNREADY in oms.readiness.reasons
    assert not oms.readiness.ready


def test_public_md_cannot_override_private_failure() -> None:
    r = ExecutionReadiness()
    r.deny(ReadinessReason.USER_STREAM_UNREADY)
    # Books fresh is irrelevant — readiness stays false
    r.clear(ReadinessReason.BOOKS_STALE)
    assert not r.ready
