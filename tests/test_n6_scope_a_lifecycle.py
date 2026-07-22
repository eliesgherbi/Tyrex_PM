"""N6 Gate 2 — Scope A lifecycle scenarios against FakeTransport.

All scenarios are deterministic: FakeClock advances only via explicit
``advance``; fills are confirmed venue trades (fill price is execution truth,
never the submitted limit).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tyrex_pm.core.ids import OrderId
from tyrex_pm.core.intents import OrderSide
from tyrex_pm.execution.lineage import SubmissionAttemptState
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.execution.polymarket.transport import VenueOrderSnapshot
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.persistence.snapshot import PersistenceError
from tyrex_pm.planning.plan import ExecutionPlan, PlanStatus, PlanningResult, new_plan_id

from helpers_n6 import (
    T0,
    fill_order,
    make_enter_intent,
    make_exit_intent,
    make_host,
    make_market,
    yes_book,
)

YES = lambda h: h.market.yes.instrument_id  # noqa: E731


# --- 1. Full entry and full exit → FLAT, pnl from fills -----------------------


def test_full_entry_and_full_exit_flat() -> None:
    h = make_host()
    book = yes_book(h, ask="0.50")
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=book)
    assert res["status"] == "ACKNOWLEDGED"
    assert res["limit_price"] == "0.50"

    # Entry executes below the submitted limit — average entry is fill truth.
    fill_order(h, res["order_id"], qty="10", price="0.48", side="BUY")
    assert h.lifecycle.state is LifecycleState.ACTIVE
    assert h.portfolio.get(YES(h)).average_entry_price == Decimal("0.48")

    ex = h.try_exit(make_exit_intent(h), book=book, limit_price=Decimal("0.50"))
    assert ex["status"] == "ACKNOWLEDGED"
    fill_order(h, ex["order_id"], qty="10", price="0.55", side="SELL")

    assert h.lifecycle.state is LifecycleState.FLAT
    out = h.post_trade_reconcile()
    assert out["flat"] is True
    assert out["residual"] == {}
    # Realized pnl from fills: (0.55 - 0.48) * 10 = 0.70 (not from limit 0.50).
    assert Decimal(out["realized_pnl"][YES(h).value]) == Decimal("0.70")


# --- 2. Partial entry then inventory-bounded exit -----------------------------


def test_partial_entry_then_inventory_bounded_exit() -> None:
    h = make_host()
    book = yes_book(h, ask="0.50")
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=book)
    fill_order(h, res["order_id"], qty="4", price="0.50", side="BUY")
    assert h.portfolio.net_quantity(YES(h)) == Decimal("4")
    assert h.lifecycle.state is LifecycleState.ACTIVE

    # Request more than confirmed inventory; exit is bounded to 4.
    ex = h.try_exit(
        make_exit_intent(h), book=book, limit_price=Decimal("0.50"), quantity=Decimal("10")
    )
    assert ex["exit_qty"] == "4"
    fill_order(h, ex["order_id"], qty="4", price="0.50", side="SELL")
    assert h.lifecycle.state is LifecycleState.FLAT


# --- 3. Partial exit with residual (not reported FLAT) ------------------------


def test_partial_exit_leaves_residual_not_flat() -> None:
    h = make_host()
    book = yes_book(h, ask="0.50")
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=book)
    fill_order(h, res["order_id"], qty="10", price="0.50", side="BUY")

    ex = h.try_exit(
        make_exit_intent(h), book=book, limit_price=Decimal("0.50"), quantity=Decimal("6")
    )
    fill_order(h, ex["order_id"], qty="6", price="0.50", side="SELL")

    out = h.post_trade_reconcile()
    assert out["flat"] is False
    assert out["residual"] == {YES(h).value: "4"}


# --- 4. Order rejection -------------------------------------------------------


def test_entry_rejection() -> None:
    h = make_host()
    h.transport.submit_behavior = "reject"
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "REJECTED"
    assert h.lifecycle.state is LifecycleState.FLAT


# --- 5. Lost/ambiguous entry ack (timeout) → AMBIGUOUS, blocks new entry ------


def test_ambiguous_entry_blocks_new_entry() -> None:
    h = make_host()
    h.transport.submit_behavior = "timeout"
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "AMBIGUOUS"
    assert h.unknown_blocks is True

    res2 = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res2["status"] == "SKIP"
    assert "ambiguous_submission" in res2["reasons"]
    assert "UNKNOWN" in res2["reasons"]


# --- 6. Lost response, recon resolves that the order exists -------------------


def test_lost_response_resolved_via_recon() -> None:
    h = make_host()
    h.transport.submit_behavior = "timeout_later"
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=yes_book(h))
    assert res["status"] == "AMBIGUOUS"

    # Venue later shows the order; reconciliation recovers it.
    h.transport.resolve_timeout_as_accepted()
    h.resolve_ambiguous_via_recon()

    assert h.unknown_blocks is False
    states = [lin.state for lin in h.lineage._by_attempt.values()]
    assert SubmissionAttemptState.RESOLVED in states


# --- 7. No duplicate entry while ambiguous ------------------------------------


def test_no_duplicate_entry_while_ambiguous() -> None:
    h = make_host()
    h.transport.submit_behavior = "timeout"
    intent = make_enter_intent(h)
    h.try_enter(intent, book=yes_book(h))
    dispatched = h.mutations_dispatched

    # Retrying the same intent must not dispatch a second submission.
    res2 = h.try_enter(intent, book=yes_book(h))
    assert res2["status"] == "SKIP"
    assert h.mutations_dispatched == dispatched


# --- 8. User-stream gap blocks entry until cleared + recon --------------------


def test_user_stream_gap_blocks_entry_until_cleared() -> None:
    h = make_host()
    h.mark_user_stream_gap(True)
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "SKIP"
    assert "user_stream_gap" in res["reasons"]

    # Clear the gap and reconcile; a clean recon does not block entry.
    h.mark_user_stream_gap(False)
    report = h.preflight_reconcile()
    assert report.blocks_entry is False
    # Simulate operator/recon clearance of the sticky UNKNOWN latch.
    h.unknown_blocks = False
    ok, reasons = h.readiness_ok_for_entry()
    assert ok, reasons


# --- 9. Open-order disagreement via recon with unknown external order ---------


def test_unknown_external_open_order_blocks() -> None:
    h = make_host()
    h.transport.open_orders["0xexternal999"] = VenueOrderSnapshot(
        venue_order_id="0xexternal999",
        status="live",
        instrument_token_id="some-other-token",
        side="BUY",
        original_size=Decimal("5"),
        size_matched=Decimal("0"),
        price=Decimal("0.50"),
    )
    report = h.preflight_reconcile()
    assert report.blocks_entry is True
    assert "UNKNOWN_EXTERNAL_ORDER" in report.counts()
    assert h.unknown_blocks is True


# --- 10. Balance disagreement → UNKNOWN blocks --------------------------------


def test_balance_disagreement_blocks_entry() -> None:
    h = make_host()
    h.set_balance_disagreement()
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "SKIP"
    assert "UNKNOWN" in res["reasons"]


# --- 11. Inventory disagreement → UNKNOWN blocks ------------------------------


def test_inventory_disagreement_blocks_entry() -> None:
    h = make_host()
    h.set_inventory_disagreement()
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "SKIP"
    assert "UNKNOWN" in res["reasons"]


# --- 12. Restart with pending entry (persist, recover) ------------------------


def test_restart_with_pending_entry(tmp_path) -> None:
    path = tmp_path / "snap.json"
    h1 = make_host(persistence_path=path)
    res = h1.try_enter(make_enter_intent(h1), book=yes_book(h1))
    assert res["status"] == "ACKNOWLEDGED"  # not filled → ENTRY_PENDING persisted

    h2 = make_host(persistence_path=path)
    payload = h2.recover()
    assert payload["lifecycle"] == "ENTRY_PENDING"
    assert len(h2.lineage._by_attempt) >= 1


# --- 13. Restart with active position -----------------------------------------


def test_restart_with_active_position(tmp_path) -> None:
    path = tmp_path / "snap.json"
    h1 = make_host(persistence_path=path)
    res = h1.try_enter(make_enter_intent(h1, target_notional=Decimal("5")), book=yes_book(h1))
    fill_order(h1, res["order_id"], qty="10", price="0.48", side="BUY")
    assert h1.lifecycle.state is LifecycleState.ACTIVE

    h2 = make_host(persistence_path=path)
    payload = h2.recover()
    assert payload["lifecycle"] == "ACTIVE"


# --- 14. Restart config fingerprint mismatch raises PersistenceError ----------


def test_restart_config_fingerprint_mismatch_raises(tmp_path) -> None:
    path = tmp_path / "snap.json"
    h1 = make_host(persistence_path=path)
    h1.try_enter(make_enter_intent(h1), book=yes_book(h1))

    # Different config → different fingerprint → refuse recovery.
    h2 = make_host(persistence_path=path, max_order_notional=Decimal("9"))
    with pytest.raises(PersistenceError):
        h2.recover()


# --- 15. Kill while FLAT blocks entry -----------------------------------------


def test_kill_while_flat_blocks_entry() -> None:
    h = make_host()
    h.activate_kill()
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "SKIP"
    assert "kill_active" in res["reasons"]


# --- 16. Kill while ACTIVE can still exit confirmed qty -----------------------


def test_kill_while_active_can_still_exit() -> None:
    h = make_host()
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=yes_book(h))
    fill_order(h, res["order_id"], qty="10", price="0.48", side="BUY")
    assert h.lifecycle.state is LifecycleState.ACTIVE

    h.activate_kill()
    ex = h.try_exit(make_exit_intent(h), book=yes_book(h), limit_price=Decimal("0.50"))
    assert ex["status"] == "ACKNOWLEDGED"
    fill_order(h, ex["order_id"], qty="10", price="0.50", side="SELL")
    assert h.lifecycle.state is LifecycleState.FLAT


# --- 17. Late entry skipped by the timing ladder ------------------------------


def test_late_entry_skipped_by_ladder() -> None:
    h = make_host()
    # last_allowed_entry_at = event_end - 4min = T0 + 1min. Advance past it.
    h.clock.advance(wall=timedelta(minutes=2))
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "SKIP"
    assert "late_entry_skipped" in res["reasons"]


# --- 18. Mandatory flatten when clock past mandatory_flatten_start ------------


def test_mandatory_flatten_when_due() -> None:
    h = make_host()
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=yes_book(h))
    fill_order(h, res["order_id"], qty="10", price="0.48", side="BUY")
    assert h.lifecycle.state is LifecycleState.ACTIVE

    # mandatory_flatten_start_at = event_end - 2min = T0 + 3min.
    h.clock.advance(wall=timedelta(minutes=3, seconds=30))
    out = h.mandatory_flatten_if_due(book=yes_book(h), limit_price=Decimal("0.50"))
    assert out is not None
    assert out["status"] == "ACKNOWLEDGED"
    fill_order(h, out["order_id"], qty="10", price="0.50", side="SELL")
    assert h.lifecycle.state is LifecycleState.FLAT


# --- 19. Stale inputs while ACTIVE: exit still allowed ------------------------


def test_stale_inputs_active_exit_allowed() -> None:
    h = make_host()
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=yes_book(h))
    fill_order(h, res["order_id"], qty="10", price="0.48", side="BUY")
    assert h.lifecycle.state is LifecycleState.ACTIVE
    assert h.inventory_unknown is False

    stale = yes_book(h, ts=T0 - timedelta(hours=1))
    ex = h.try_exit(make_exit_intent(h), book=stale, limit_price=Decimal("0.50"))
    assert ex["status"] == "ACKNOWLEDGED"


# --- 20. min_valid_order_notional > hard_cap → SKIP ---------------------------


def test_min_valid_order_exceeds_hard_cap_skips() -> None:
    h = make_host(min_valid_order_notional=Decimal("50"))  # cap is 10
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "SKIP"
    assert "min_valid_order_exceeds_hard_cap" in res["reasons"]


# --- 21. Fee-inclusive notional > cap → SKIP ----------------------------------


def test_fee_inclusive_notional_exceeds_cap_skips() -> None:
    h = make_host()  # hard cap 10

    class _OversizedPlanner:
        def plan(self, intent, *, risk, market, book, now, causation_id=None):
            plan = ExecutionPlan(
                plan_id=new_plan_id(),
                intent_id=intent.intent_id,
                instrument_id=market.yes.instrument_id,
                token_id=market.yes.token_id,
                market_id=market.market_id,
                side=OrderSide.BUY,
                quantity=Decimal("20"),
                limit_price=Decimal("1"),  # 20 * 1 = 20 > cap 10
                expected_notional=Decimal("20"),
                book_ts_event=book.ts_event,
                tick_size=market.tick_size,
                min_order_size=None,
                planned_at=now,
                correlation_id=intent.correlation_id,
                causation_id=None,
            )
            return PlanningResult(status=PlanStatus.PLANNED, plan=plan)

    h.planner = _OversizedPlanner()
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=yes_book(h))
    assert res["status"] == "SKIP"
    assert "fee_inclusive_notional_exceeds_cap" in res["reasons"]


# --- 22. Replay: duplicate fill (same execution id) doesn't double ------------


def test_replay_duplicate_fill_does_not_double() -> None:
    h = make_host()
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=yes_book(h))
    trade = fill_order(h, res["order_id"], qty="10", price="0.50", side="BUY")
    assert h.portfolio.net_quantity(YES(h)) == Decimal("10")

    # Re-publish the identical confirmed trade (same venue_trade_id).
    h.ingest_confirmed_trade(order_id=OrderId(res["order_id"]), trade=trade)
    assert h.portfolio.net_quantity(YES(h)) == Decimal("10")
    assert h.order_store.get(OrderId(res["order_id"])).filled_quantity == Decimal("10")


# --- 23. Adjacent market: different host/market_id doesn't share lineage ------


def test_adjacent_market_does_not_share_lineage() -> None:
    ha = make_host(market=make_market(market_id="cond-A", yes_token="yesA", no_token="noA"))
    hb = make_host(market=make_market(market_id="cond-B", yes_token="yesB", no_token="noB"))

    ha.try_enter(make_enter_intent(ha), book=yes_book(ha))
    assert len(ha.lineage._by_attempt) == 1
    assert len(hb.lineage._by_attempt) == 0

    resb = hb.try_enter(make_enter_intent(hb), book=yes_book(hb))
    assert resb["status"] == "ACKNOWLEDGED"
    assert len(hb.lineage._by_attempt) == 1


# --- 24. Scope B / hold-to-resolution / redeem refused ------------------------


def test_scope_b_and_hold_and_redeem_refused() -> None:
    h = make_host()
    for request in ("scope_b", "hold_to_resolution", "redeem"):
        out = h.refuse_scope_b(request)
        assert out["refused"] is True
        assert out["reason"] == "scope_b_unsupported"
        assert out["live_scope"] == "A"


# --- mutations OFF rejects without transport submit ---------------------------


def test_mutations_off_rejects_without_transport_submit() -> None:
    h = make_host(mutations_enabled=False, authorization=None)
    res = h.try_enter(make_enter_intent(h), book=yes_book(h))
    assert res["status"] == "REJECTED"
    assert len(h.transport.submitted) == 0
    assert h.lifecycle.state is LifecycleState.FLAT


# --- real SdkMutationTransport cannot be armed --------------------------------


def test_real_sdk_transport_cannot_be_armed() -> None:
    from tyrex_pm.execution.polymarket.mutation_transport import SdkMutationTransport

    transport = SdkMutationTransport(_client=None)
    h = make_host(transport=transport)  # config asks for mutations + fake authorization
    assert h.status()["mutations_armed"] is False
    with pytest.raises(RuntimeError):
        h.assert_no_real_mutation_transport()


# --- submitted limit != fill price in average_entry_price ---------------------


def test_submitted_limit_is_not_fill_price() -> None:
    h = make_host()
    book = yes_book(h, ask="0.50")
    res = h.try_enter(make_enter_intent(h, target_notional=Decimal("5")), book=book)
    submitted_limit = Decimal(res["limit_price"])
    fill_order(h, res["order_id"], qty="10", price="0.42", side="BUY")

    pos = h.portfolio.get(YES(h))
    assert pos.average_entry_price == Decimal("0.42")
    assert pos.average_entry_price != submitted_limit
