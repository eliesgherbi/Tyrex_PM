"""R7E side-correct exit planning — no network mutations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tyrex_pm.execution.polymarket.lifecycle_exit_plan import (
    ExitPlanStatus,
    ExitPricePolicy,
    ExitRetryPolicy,
    ExitUrgency,
    book_from_clob_levels,
    compute_sell_qty_cap,
    is_fak_no_match_error,
    plan_lifecycle_fak_sell,
)
from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from tyrex_pm.runtime.r7_lifecycle_residuals import (
    LifecycleResidualRecord,
    LifecycleResidualRegistry,
    evaluate_residuals_for_entry,
    read_residual_registry,
    upsert_residual,
    write_residual_registry,
)
from tyrex_pm.runtime.r7b_live_once import TerminalOutcome, run_r7b_live_once
from test_r7b_live_once import _base_args, _exit_book


NOW = datetime(2026, 7, 17, 19, 8, 17, tzinfo=timezone.utc)


def _book(
    *,
    bid: str = "0.50",
    bid_size: str = "100",
    ask: str = "0.51",
    age_ms: int = 0,
    extra_bids: list[tuple[str, str]] | None = None,
) -> Any:
    bids = [{"price": bid, "size": bid_size}]
    if extra_bids:
        for p, s in extra_bids:
            bids.append({"price": p, "size": s})
    return book_from_clob_levels(
        token_id="tok",
        bids=bids,
        asks=[{"price": ask, "size": "100"}],
        ts_event=NOW - timedelta(milliseconds=age_ms),
    )


def test_buy_051_best_bid_050_sell_uses_bid_side() -> None:
    plan = plan_lifecycle_fak_sell(
        book=_book(bid="0.50"),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=NOW,
        entry_buy_limit=Decimal("0.51"),
    )
    assert plan.ok
    assert plan.limit_price == Decimal("0.50")
    assert plan.limit_price != Decimal("0.51")
    assert plan.evidence["entry_buy_limit_not_used"] is True


def test_sell_never_reuses_buy_limit_in_live_once(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    exit_now = datetime(2026, 7, 17, 12, 1, 0, tzinfo=timezone.utc)
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            exit_book_provider=lambda _t: _book(
                bid="0.50", ask="0.51", age_ms=0
            ),
            exit_now_provider=lambda: exit_now,
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    sells = [r for r in spy.submitted if r.side.upper() == "SELL"]
    buys = [r for r in spy.submitted if r.side.upper() == "BUY"]
    assert len(sells) == 1
    assert Decimal(sells[0].price) == Decimal("0.50")
    assert Decimal(sells[0].price) != Decimal(buys[0].price)
    assert result.report["final"]["exit_used_buy_limit"] is False


def test_stale_exit_book_blocks() -> None:
    plan = plan_lifecycle_fak_sell(
        book=_book(bid="0.50", age_ms=5000),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=NOW,
        policy=ExitPricePolicy(max_book_age_ms=2000),
    )
    assert plan.status is ExitPlanStatus.REFUSE_STALE_BOOK
    assert not plan.ok


def test_empty_bid_book_waits_without_submit() -> None:
    book = book_from_clob_levels(
        token_id="tok",
        bids=[],
        asks=[{"price": "0.51", "size": "10"}],
        ts_event=NOW,
    )
    plan = plan_lifecycle_fak_sell(
        book=book,
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=NOW,
    )
    assert plan.status is ExitPlanStatus.WAIT_NO_BIDS
    assert plan.limit_price is None


def test_multi_level_bid_vwap_and_worst() -> None:
    plan = plan_lifecycle_fak_sell(
        book=_book(
            bid="0.50",
            bid_size="5",
            extra_bids=[("0.49", "10")],
        ),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=NOW,
    )
    assert plan.ok
    assert plan.worst_accepted_price == Decimal("0.49")
    assert plan.limit_price == Decimal("0.49")
    assert plan.expected_vwap is not None
    assert plan.expected_vwap < Decimal("0.50")


def test_sell_tick_rounding_side_correct() -> None:
    plan = plan_lifecycle_fak_sell(
        book=_book(bid="0.503"),
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=NOW,
    )
    assert plan.limit_price == Decimal("0.50")  # ROUND_DOWN


def test_confirmed_balance_caps_sell_qty() -> None:
    qty = compute_sell_qty_cap(
        confirmed_acquired=Decimal("9.470587"),
        sellable_balance=Decimal("9.470587"),
        remaining_after_confirmed_exits=Decimal("9.470587"),
    )
    assert qty == Decimal("9.47")


def test_normal_floor_refuses() -> None:
    plan = plan_lifecycle_fak_sell(
        book=_book(bid="0.02", ask="0.03"),
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=NOW,
        policy=ExitPricePolicy(
            normal_floor=Decimal("0.10"),
            max_book_spread=Decimal("0.50"),
        ),
        urgency=ExitUrgency.NORMAL,
    )
    assert plan.status is ExitPlanStatus.REFUSE_FLOOR


def test_emergency_floor_allows_lower() -> None:
    plan = plan_lifecycle_fak_sell(
        book=_book(bid="0.02", ask="0.03"),
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=NOW,
        policy=ExitPricePolicy(
            normal_floor=Decimal("0.10"),
            emergency_floor=Decimal("0.01"),
            max_book_spread=Decimal("0.50"),
        ),
        urgency=ExitUrgency.EMERGENCY,
    )
    assert plan.ok
    assert plan.limit_price == Decimal("0.02")


def test_fak_no_match_then_retry_success(tmp_path: Path) -> None:
    from tyrex_pm.execution.polymarket.transport import SubmitOrderResult

    class FakThenOk(SpyMutationTransport):
        def __init__(self) -> None:
            super().__init__()
            self._sells = 0

        def submit_order(self, request):  # type: ignore[no-untyped-def]
            if request.side.upper() == "SELL":
                self._sells += 1
                self.submitted.append(request)
                self._submit_n += 1
                if self._sells == 1:
                    return SubmitOrderResult(
                        ok=False,
                        venue_order_id=None,
                        status=None,
                        error=(
                            "no orders found to match with FAK order. "
                            "FAK orders are partially filled or killed if no match is found."
                        ),
                    )
                return SubmitOrderResult(
                    ok=True,
                    venue_order_id=f"0xspy{self._submit_n:04d}",
                    status="matched",
                )
            return super().submit_order(request)

    books = [
        _book(bid="0.51"),  # would have been wrong historically
        _book(bid="0.50"),
    ]
    idx = {"i": 0}

    def provider(_tid: str) -> Any:
        b = books[min(idx["i"], len(books) - 1)]
        idx["i"] += 1
        return b

    # Stay inside the synthetic window flatten deadline (noon + 270s).
    exit_now = datetime(2026, 7, 17, 12, 1, 0, tzinfo=timezone.utc)
    spy = FakThenOk()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            exit_book_provider=provider,
            exit_now_provider=lambda: exit_now,
            exit_retry_policy=ExitRetryPolicy(max_attempts=3, cooldown_s=0.0),
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    sells = [r for r in spy.submitted if r.side.upper() == "SELL"]
    assert len(sells) == 2
    assert Decimal(sells[-1].price) == Decimal("0.50")


def test_repeated_no_match_manual_intervention(tmp_path: Path) -> None:
    from tyrex_pm.execution.polymarket.transport import SubmitOrderResult

    class AlwaysFak(SpyMutationTransport):
        def submit_order(self, request):  # type: ignore[no-untyped-def]
            if request.side.upper() == "SELL":
                self.submitted.append(request)
                self._submit_n += 1
                return SubmitOrderResult(
                    ok=False,
                    venue_order_id=None,
                    status=None,
                    error="no orders found to match with FAK order",
                )
            return super().submit_order(request)

    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=AlwaysFak(),
            exit_book_provider=lambda _t: _book(bid="0.50"),
            exit_retry_policy=ExitRetryPolicy(max_attempts=3, cooldown_s=0.0),
        )
    )
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION
    sells = [r for r in result.report["mutations_attempted"] if r["side"] == "SELL"]
    assert len(sells) == 3
    assert "residual" in result.report


def test_second_residual_does_not_overwrite_r7b_dust(tmp_path: Path) -> None:
    now = NOW.isoformat()
    reg = LifecycleResidualRegistry()
    upsert_residual(
        reg,
        LifecycleResidualRecord(
            condition_id="0xold",
            token_id="told",
            originating_run_id="d632b631-166f-4e35-8db8-fe69a3f86795",
            market_slug="btc-updown-5m-1784303100",
            acquired_quantity="9.470587",
            exited_quantity="9.47",
            residual_quantity="0.000587",
            min_tradable="0.01",
            classification="FLAT_WITH_DUST",
            provenance="r7b",
            created_at=now,
            updated_at=now,
            last_reconciliation_source="test",
            tradable=False,
        ),
    )
    upsert_residual(
        reg,
        LifecycleResidualRecord(
            condition_id="0xc60a7a5e093f89e668a25a8a4d43ec2cd175096380f9a98c81ab48d0eea9b7be",
            token_id="31263449814698561223405652464241470284809290399992381387531969085905328780279",
            originating_run_id="76e8470a-72dc-4d6d-b653-760e87bdaf29",
            market_slug="btc-updown-5m-1784315400",
            acquired_quantity="9.470587",
            exited_quantity="9.47",
            residual_quantity="0.000587",
            min_tradable="0.01",
            classification="FLAT_WITH_DUST",
            provenance="r7d2",
            created_at=now,
            updated_at=now,
            last_reconciliation_source="test",
            tradable=False,
        ),
    )
    write_residual_registry(reg, repo_root=tmp_path)
    loaded = read_residual_registry(repo_root=tmp_path)
    assert loaded is not None
    assert len(loaded.residuals) == 2
    assert loaded.residuals[
        "0xold|told|d632b631-166f-4e35-8db8-fe69a3f86795"
    ].provenance == "r7b"


def test_tradable_residual_blocks_entry(tmp_path: Path) -> None:
    now = NOW.isoformat()
    reg = LifecycleResidualRegistry()
    upsert_residual(
        reg,
        LifecycleResidualRecord(
            condition_id="0x1",
            token_id="texpose",
            originating_run_id="run-x",
            market_slug="m",
            acquired_quantity="5",
            exited_quantity="0",
            residual_quantity="5",
            min_tradable="0.01",
            classification="RESIDUAL_EXPOSURE",
            provenance="open",
            created_at=now,
            updated_at=now,
            last_reconciliation_source="test",
            tradable=True,
        ),
    )
    write_residual_registry(reg, repo_root=tmp_path)
    ev = evaluate_residuals_for_entry(reg)
    assert not ev["ok"]
    assert "TRADABLE_RESIDUAL_EXPOSURE" in ev["blockers"]


def test_registered_dust_visible_not_blocking() -> None:
    now = NOW.isoformat()
    reg = LifecycleResidualRegistry()
    upsert_residual(
        reg,
        LifecycleResidualRecord(
            condition_id="0x1",
            token_id="tdust",
            originating_run_id="run-d",
            market_slug="other",
            acquired_quantity="9.47",
            exited_quantity="9.47",
            residual_quantity="0.000587",
            min_tradable="0.01",
            classification="FLAT_WITH_DUST",
            provenance="dust",
            created_at=now,
            updated_at=now,
            last_reconciliation_source="test",
            tradable=False,
        ),
    )
    ev = evaluate_residuals_for_entry(reg, selected_token_id="different")
    assert ev["ok"]
    assert ev["open_count"] == 1


def test_incident_fixture_replay_does_not_sell_at_buy_limit() -> None:
    """Regression: BUY 0.51 + bid 0.50 must not plan SELL at 0.51."""
    plan = plan_lifecycle_fak_sell(
        book=_book(bid="0.50", ask="0.51"),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=NOW,
        entry_buy_limit=Decimal("0.51"),
    )
    assert plan.ok
    assert plan.limit_price == Decimal("0.50")
    # Prove the historical bug condition
    assert not (
        plan.limit_price == Decimal("0.51") and plan.best_bid == Decimal("0.50")
    )


def test_is_fak_no_match_error() -> None:
    assert is_fak_no_match_error(
        "no orders found to match with FAK order. FAK orders are partially filled"
    )
    assert not is_fak_no_match_error("balance: 0")


def test_no_old_import() -> None:
    import tyrex_pm.execution.polymarket.lifecycle_exit_plan as mod
    import inspect

    src = inspect.getsource(mod)
    assert "old/" not in src
    assert "sys.path" not in src
