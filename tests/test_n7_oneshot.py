"""N7A one-shot host deterministic acceptance (FakeTransport only)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import MarketId, OrderId, StrategyId
from tyrex_pm.runtime.n6_account_classify import (
    AcknowledgedExternalPosition,
    classify_account,
)
from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_authorization import create_authorization_request, make_test_envelope
from tyrex_pm.runtime.n7_oneshot_host import N7OneShotHost
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport

from helpers_n7 import (
    EVENT_END,
    GIT_HEAD,
    T0,
    fill_order,
    make_enter,
    make_exit,
    make_market,
    make_n7_host,
    make_sealed,
    yes_book,
)


def test_default_off_no_mutation_without_auth():
    host = make_n7_host(approve=False, arm=False)
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "ABORT"
    assert r["abort"] == N7AbortCode.AUTHORIZATION_ABSENT.value
    assert host.inner.real_venue_mutations == 0
    assert host.inner.mutations_dispatched == 0


def test_no_submission_without_exact_authorization():
    host = make_n7_host(approve=True, arm=False)
    # approved but not armed
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "ABORT"


def test_authorization_single_use_and_head_config_worktree():
    sealed = make_sealed()
    host = make_n7_host(arm=True)
    assert (
        host.validate_git_and_config(
            git_head=GIT_HEAD, worktree_clean=True, config_fingerprint=sealed.fingerprint()
        )
        is None
    )
    assert (
        host.validate_git_and_config(git_head="ffff", worktree_clean=True)
        is N7AbortCode.HEAD_MISMATCH
    )
    host2 = make_n7_host(arm=True)
    assert (
        host2.validate_git_and_config(git_head=GIT_HEAD, worktree_clean=False)
        is N7AbortCode.DIRTY_WORKTREE
    )
    # consume once
    host3 = make_n7_host(approve=True, arm=False)
    assert host3.arm_from_envelope_fake() is None
    assert host3.arm_from_envelope_fake() is N7AbortCode.AUTHORIZATION_CONSUMED


def test_envelope_binds_one_window_no_rollover():
    host = make_n7_host()
    other = make_market(market_id="cond-n7-other")
    assert host.refuse_second_window(other.market_id.value)["refused"] is True
    # re-bind refused
    assert host.envelope is not None
    assert (
        host.envelope.bind_market(
            market_id="x", window_id="y", market_family="btc_updown_5m"
        )
        is N7AbortCode.BINDING_ALREADY_SET
    )


def test_at_most_one_entry_lineage_no_reentry():
    host = make_n7_host()
    book = yes_book(host)
    r1 = host.try_enter(make_enter(host), book=book)
    assert r1["status"] == "ACKNOWLEDGED"
    fill_order(host, r1["order_id"], qty="10", price="0.50")
    r2 = host.try_enter(make_enter(host), book=book)
    assert r2["status"] == "ABORT"
    assert r2["abort"] == N7AbortCode.REENTRY_REFUSED.value


def test_fee_inclusive_cap_and_min_above_cap_skip():
    host = make_n7_host(min_valid_order_notional=Decimal("6"))
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "SKIP"
    assert r["abort"] == N7AbortCode.VENUE_MINIMUM_ABOVE_CAP.value

    host2 = make_n7_host()
    # target 5 at 0.50 → qty 10, notional 5.00 OK
    r2 = host2.try_enter(make_enter(host2, notional=Decimal("5")), book=yes_book(host2))
    assert r2["status"] == "ACKNOWLEDGED"
    # over daily notional
    host3 = make_n7_host()
    host3.daily_entry_notional = Decimal("5")
    r3 = host3.try_enter(make_enter(host3), book=yes_book(host3))
    assert r3["abort"] == N7AbortCode.RISK_OR_DAILY_LIMIT_BREACH.value


def test_fills_not_submitted_limit_update_portfolio():
    host = make_n7_host()
    book = yes_book(host, ask="0.50", bid="0.48")
    entered = host.try_enter(make_enter(host), book=book)
    # fill away from a typical limit — portfolio uses fill price as truth
    fill_order(host, entered["order_id"], qty="10", price="0.53")
    pos = host.inner.portfolio.net_quantity(host.market.yes.instrument_id)
    assert pos == Decimal("10")
    held = host.inner.portfolio.get(host.market.yes.instrument_id)
    assert held is not None
    assert held.average_entry_price == Decimal("0.53")
    assert held.average_entry_price != Decimal(entered["limit_price"])


def test_partial_entry_and_inventory_bounded_exit():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    fill_order(host, entered["order_id"], qty="4", price="0.50")
    assert host.inner.portfolio.net_quantity(host.market.yes.instrument_id) == Decimal("4")
    exited = host.try_exit(
        make_exit(host), book=book, limit_price=Decimal("0.49"), quantity=Decimal("10")
    )
    assert exited["status"] == "ACKNOWLEDGED"
    assert Decimal(exited["exit_qty"]) == Decimal("4")


def test_partial_exit_residual_not_labelled_flat():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    fill_order(host, entered["order_id"], qty="10", price="0.50")
    exited = host.try_exit(
        make_exit(host), book=book, limit_price=Decimal("0.49"), quantity=Decimal("4")
    )
    fill_order(host, exited["order_id"], qty="4", price="0.49", side="SELL")
    post = host.inner.post_trade_reconcile()
    assert post["flat"] is False
    assert post["residual"]


def test_rejection_and_ambiguous_entry_blocks_fresh():
    host = make_n7_host()
    host.transport.submit_behavior = "reject"
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "REJECTED"

    host2 = make_n7_host()
    host2.transport.submit_behavior = "timeout"
    r2 = host2.try_enter(make_enter(host2), book=yes_book(host2))
    assert r2["status"] == "AMBIGUOUS"
    # second entry blocked by lineage / entry_lineages
    host2.transport.submit_behavior = "accept"
    r3 = host2.try_enter(make_enter(host2), book=yes_book(host2))
    assert r3["status"] in {"ABORT", "BLOCKED_DUPLICATE", "SKIP"}


def test_lost_response_recon_no_duplicate():
    host = make_n7_host()
    host.transport.submit_behavior = "timeout_later"
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "AMBIGUOUS"
    host.transport.resolve_timeout_as_accepted()
    host.inner.resolve_ambiguous_via_recon()
    # still only one entry lineage counted
    assert host.entry_lineages == 1


def test_exit_ambiguity_recon_before_retry():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    fill_order(host, entered["order_id"], qty="10", price="0.50")
    host.transport.submit_behavior = "timeout"
    ladder = host.run_bounded_exit_ladder(book=book, limit_price=Decimal("0.49"))
    assert ladder["status"] in {"EXIT_SUBMITTED", "AMBIGUOUS"}
    host.transport.submit_behavior = "accept"
    host.inner.resolve_ambiguous_via_recon()


def test_bounded_exit_budget_and_mandatory_flatten():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    fill_order(host, entered["order_id"], qty="10", price="0.50")
    # First ladder call submits an exit; subsequent calls wait on pending
    # without blind re-submit, then exhaust the attempt budget.
    first = host.run_bounded_exit_ladder(book=book, limit_price=Decimal("0.49"))
    assert first["status"] == "EXIT_SUBMITTED"
    exhausted = host.run_bounded_exit_ladder(book=book, limit_price=Decimal("0.49"))
    assert exhausted["status"] == "BUDGET_EXHAUSTED"
    assert host.exit_attempts >= 3

    # Fresh host: mandatory flatten when past flatten start and still ACTIVE.
    host2 = make_n7_host()
    book2 = yes_book(host2)
    entered2 = host2.try_enter(make_enter(host2), book=book2)
    fill_order(host2, entered2["order_id"], qty="10", price="0.50")
    host2.clock.set_utc(EVENT_END - timedelta(seconds=80))
    flat = host2.inner.mandatory_flatten_if_due(book=book2, limit_price=Decimal("0.49"))
    assert flat is not None
    assert flat.get("status") in {"ACKNOWLEDGED", "DISPATCHED", "FLAT"}


def test_stale_strategy_still_exits():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    fill_order(host, entered["order_id"], qty="10", price="0.50")
    out = host.run_bounded_exit_ladder(
        book=book, limit_price=Decimal("0.49"), strategy_stale=True
    )
    assert out.get("strategy_stale_ok") is True
    assert out["status"] == "EXIT_SUBMITTED"


def test_kill_flat_and_active():
    host = make_n7_host()
    host.inner.activate_kill()
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert "kill_active" in (r.get("reasons") or []) or r.get("status") == "SKIP"

    host2 = make_n7_host()
    book = yes_book(host2)
    entered = host2.try_enter(make_enter(host2), book=book)
    fill_order(host2, entered["order_id"], qty="10", price="0.50")
    host2.inner.activate_kill()
    out = host2.run_bounded_exit_ladder(book=book, limit_price=Decimal("0.49"))
    assert out["status"] == "EXIT_SUBMITTED"


def test_crash_disables_mutations_restart_needs_fresh_auth():
    host = make_n7_host()
    crash = host.on_crash()
    assert crash["mutations_armed"] is False
    assert host.inner.oms.mutations_enabled is False
    # cannot re-arm same consumed envelope
    assert host.arm_from_envelope_fake() in {
        N7AbortCode.AUTHORIZATION_CONSUMED,
        N7AbortCode.CRASH_MUTATIONS_DISABLED,
    }


def test_full_lifecycle_flat_and_terminate():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    fill_order(host, entered["order_id"], qty="10", price="0.50")
    exited = host.try_exit(make_exit(host), book=book, limit_price=Decimal("0.49"))
    fill_order(host, exited["order_id"], qty="10", price="0.49", side="SELL")
    post = host.inner.post_trade_reconcile()
    assert post["flat"] is True
    assert host.inner.real_venue_mutations == 0
    host.terminate()
    assert host.terminated
    assert host.mutations_force_off
    # no second window
    r = host.try_enter(make_enter(host), book=book)
    assert r["status"] == "ABORT"


def test_scope_b_redeem_cancel_all_historical_refused():
    host = make_n7_host()
    assert host.refuse_forbidden("scope_b")["abort"] == N7AbortCode.SCOPE_B_REFUSED.value
    assert host.refuse_forbidden("redeem")["abort"] == N7AbortCode.REDEEM_REFUSED.value
    assert host.refuse_forbidden("cancel_all")["abort"] == N7AbortCode.CANCEL_ALL_REFUSED.value
    assert (
        host.refuse_forbidden("historical_as_strategy")["abort"]
        == N7AbortCode.HISTORICAL_AS_STRATEGY_REFUSED.value
    )
    # historical cannot become strategy inventory
    ack = (
        AcknowledgedExternalPosition(label="historical_lol", status="RESOLVED_REDEEMABLE"),
    )
    classified = classify_account(
        open_orders=[],
        positions=[],
        selected_market_token_ids=set(),
        acknowledged=ack,
    )
    assert classified.globally_flat is False
    assert any(
        a["not_strategy_inventory"] for a in classified.acknowledged_external
    )


def test_ctrl_c_disables_and_manages_inventory():
    host = make_n7_host()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    fill_order(host, entered["order_id"], qty="10", price="0.50")
    out = host.handle_ctrl_c(book=book, limit_price=Decimal("0.49"))
    assert out["abort"] == N7AbortCode.CTRL_C_ABORT.value
    assert host.mutations_force_off


def test_adjacent_markets_cannot_mix():
    host = make_n7_host()
    other = make_market(market_id="adjacent")
    sealed = make_sealed()
    env = make_test_envelope(sealed=sealed, git_head=GIT_HEAD, approve=True, now=T0)
    other_host = N7OneShotHost(
        sealed=sealed,
        clock=FakeClock(T0),
        transport=FakeTransport(),
        market=other,
        envelope=env,
    )
    # envelope already bound to first market on host — fresh env binds other
    assert other_host.bind_envelope_to_market() is None
    # original host refuses other id
    assert host.refuse_second_window("adjacent")["refused"]


def test_late_entry_skip():
    host = make_n7_host()
    host.clock.set_utc(EVENT_END - timedelta(seconds=100))
    r = host.try_enter(make_enter(host), book=yes_book(host))
    assert r["status"] == "SKIP"
    assert "late_entry_skipped" in (r.get("reasons") or [])
