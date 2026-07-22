"""N7 one-shot host deterministic acceptance (FakeTransport; no ceremony)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.runtime.n6_account_classify import (
    AcknowledgedExternalPosition,
    classify_account,
)
from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_oneshot_host import N7OneShotHost
from tyrex_pm.runtime.n7_sizing import FeeInclusiveEntrySize

from helpers_n7 import (
    EVENT_END,
    T0,
    fill_order,
    make_enter,
    make_exit,
    make_market,
    make_n7_host,
    make_sealed,
    yes_book,
)


def test_no_envelope_required_and_default_off():
    host = make_n7_host(arm=False)
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "ABORT"
    assert r["abort"] == N7AbortCode.AUTHORIZATION_ABSENT.value
    assert host.inner.real_venue_mutations == 0
    assert host.inner.mutations_dispatched == 0


def test_importing_cannot_submit_without_arm():
    host = make_n7_host(arm=False)
    assert host.inner.oms.mutations_enabled is False
    assert host._mutations_ready() is False


def test_ci_cannot_arm_operator_live(monkeypatch):
    host = make_n7_host(arm=False)
    # Swap to non-fake transport name via subclass
    class _T:
        def enable_network(self, arm):  # noqa: ANN001
            self.arm = arm

        def disable_network(self):
            pass

    host.transport = _T()  # type: ignore[assignment]
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_ci_cannot_arm_operator_live")
    assert host.arm_operator_live() is N7AbortCode.AUTHORIZATION_MISMATCH


def test_fee_inclusive_cap_on_enter():
    host = make_n7_host()
    book = yes_book(host, ask="0.50", bid="0.48")
    r = host.try_enter(make_enter(host, notional=Decimal("5")), book=book)
    assert r["status"] == "ACKNOWLEDGED"
    sizing = r["sizing"]
    assert Decimal(sizing["max_fee_inclusive_debit"]) <= Decimal("5.00")
    assert isinstance(host.size_entry(worst_price=Decimal("0.50")), FeeInclusiveEntrySize)


def test_at_most_one_entry_no_reentry_no_second_market():
    host = make_n7_host()
    book = yes_book(host)
    r1 = host.try_enter(make_enter(host), book=book)
    assert r1["status"] == "ACKNOWLEDGED"
    qty = r1["sizing"]["quantity"]
    fill_order(host, r1["order_id"], qty=qty, price="0.50")
    r2 = host.try_enter(make_enter(host), book=book)
    assert r2["abort"] == N7AbortCode.REENTRY_REFUSED.value
    assert host.refuse_second_window("other")["refused"] is True


def test_fills_update_portfolio_not_limit():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    qty = entered["sizing"]["quantity"]
    fill_order(host, entered["order_id"], qty=qty, price="0.53")
    held = host.inner.portfolio.get(host.market.yes.instrument_id)
    assert held is not None
    assert held.average_entry_price == Decimal("0.53")


def test_inventory_bounded_exit_and_residual_not_flat():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    qty = Decimal(entered["sizing"]["quantity"])
    fill_order(host, entered["order_id"], qty=qty, price="0.50")
    half = (qty / 2).quantize(Decimal("0.000001"))
    exited = host.try_exit(
        make_exit(host), book=book, limit_price=Decimal("0.49"), quantity=half
    )
    assert Decimal(exited["exit_qty"]) == half
    fill_order(host, exited["order_id"], qty=half, price="0.49", side="SELL")
    post = host.inner.post_trade_reconcile()
    assert post["flat"] is False


def test_ambiguous_entry_no_duplicate():
    host = make_n7_host()
    host.transport.submit_behavior = "timeout"
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "AMBIGUOUS"
    host.transport.submit_behavior = "accept"
    r2 = host.try_enter(make_enter(host), book=yes_book(host))
    assert r2["status"] in {"ABORT", "BLOCKED_DUPLICATE", "SKIP"}


def test_stale_strategy_exit_and_kill():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    fill_order(
        host, entered["order_id"], qty=entered["sizing"]["quantity"], price="0.50"
    )
    out = host.run_bounded_exit_ladder(
        book=book, limit_price=Decimal("0.49"), strategy_stale=True
    )
    assert out.get("strategy_stale_ok") is True

    host2 = make_n7_host()
    host2.inner.activate_kill()
    r = host2.try_enter(make_enter(host2), book=yes_book(host2))
    assert "kill_active" in (r.get("reasons") or []) or r.get("status") == "SKIP"


def test_crash_and_ctrl_c():
    host = make_n7_host()
    assert host.on_crash()["mutations_armed"] is False
    host2 = make_n7_host()
    book = yes_book(host2)
    entered = host2.try_enter(make_enter(host2), book=book)
    fill_order(
        host2, entered["order_id"], qty=entered["sizing"]["quantity"], price="0.50"
    )
    out = host2.handle_ctrl_c(book=book, limit_price=Decimal("0.49"))
    assert out["abort"] == N7AbortCode.CTRL_C_ABORT.value
    assert host2.mutations_force_off


def test_full_lifecycle_flat():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    qty = entered["sizing"]["quantity"]
    fill_order(host, entered["order_id"], qty=qty, price="0.50")
    exited = host.try_exit(make_exit(host), book=book, limit_price=Decimal("0.49"))
    fill_order(host, exited["order_id"], qty=exited["exit_qty"], price="0.49", side="SELL")
    econ = host.economics_report()
    assert econ["flat"] is True
    assert Decimal(econ["max_fee_inclusive_entry_debit"]) <= Decimal("5.00")
    host.terminate()
    assert host.mutations_force_off


def test_scope_b_redeem_historical_refused():
    host = make_n7_host()
    assert host.refuse_forbidden("scope_b")["abort"] == N7AbortCode.SCOPE_B_REFUSED.value
    assert host.refuse_forbidden("redeem")["abort"] == N7AbortCode.REDEEM_REFUSED.value
    classified = classify_account(
        open_orders=[],
        positions=[],
        selected_market_token_ids=set(),
        acknowledged=(
            AcknowledgedExternalPosition(label="historical_lol", status="RESOLVED_REDEEMABLE"),
        ),
    )
    assert classified.globally_flat is False
    assert all(a["not_strategy_inventory"] for a in classified.acknowledged_external)


def test_late_entry_skip():
    host = make_n7_host()
    host.clock.set_utc(EVENT_END - timedelta(seconds=100))
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "SKIP"
    assert "late_entry_skipped" in (r.get("reasons") or [])


def test_min_above_cap_skip():
    host = make_n7_host(min_valid_order_notional=Decimal("5"))
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "SKIP"
    assert r["abort"] == N7AbortCode.VENUE_MINIMUM_ABOVE_CAP.value
