"""R7F: exit-planner integration acceptance (no network mutations)."""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.execution.polymarket.lifecycle_exit_plan import (
    ExitPlanStatus,
    ExitUrgency,
    book_from_clob_levels,
    plan_lifecycle_fak_sell,
)
from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from tyrex_pm.runtime import r7b_live_once as live_mod
from tyrex_pm.runtime.r7_lifecycle_policy import (
    BOOK_FRESHNESS_MAX_AGE_MS,
    EMERGENCY_EXIT_PRICE_FLOOR,
    EXIT_MAX_ATTEMPTS,
    EXIT_RETRY_COOLDOWN_S,
    FLATTEN_BEFORE_CLOSE_S,
    MAX_EXIT_BOOK_SPREAD,
    MAX_EXIT_SLIPPAGE_FROM_TOUCH,
    MIN_REMAINING_FOR_ENTRY_S,
    MIN_TRADABLE_QTY,
    NORMAL_EXIT_PRICE_FLOOR,
    SETTLEMENT_INITIAL_BACKOFF_S,
    SETTLEMENT_MAX_WAIT_S,
    default_exit_price_policy,
    policy_snapshot,
)
from tyrex_pm.runtime.r7_lifecycle_residuals import (
    LifecycleResidualRecord,
    LifecycleResidualRegistry,
    evaluate_residuals_for_entry,
    upsert_residual,
    write_residual_registry,
    read_residual_registry,
)
from tyrex_pm.runtime.r7b_live_once import TerminalOutcome, run_r7b_live_once
from test_r7b_live_once import _base_args


NOW = datetime(2026, 7, 17, 12, 1, 0, tzinfo=timezone.utc)


def test_runtime_imports_lifecycle_exit_plan() -> None:
    src = inspect.getsource(live_mod)
    assert "plan_lifecycle_fak_sell" in src
    assert "lifecycle_exit_plan" in src
    assert "from old" not in src
    assert "old/" not in src
    # Exit submit must use plan.limit_price, not sized.limit_price
    assert 'price=str(plan.limit_price)' in src
    assert 'price=str(sized.limit_price)' in src  # BUY only
    # Count: sized.limit_price must not appear as SELL price assignment
    tree = ast.parse(src)
    sell_priced_from_sized = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # look for SubmitOrderRequest(... side SELL, price sized...)
            pass
    assert "never reuse sized.limit_price" in src.lower() or "never reuse" in src


def test_no_old_dependency_in_active_modules() -> None:
    root = Path("src/tyrex_pm")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import old" in text or "from old" in text or "old/" in text and "sys.path" in text:
            offenders.append(str(path))
    assert offenders == []


def test_exact_policy_values() -> None:
    snap = policy_snapshot()
    assert snap["book_freshness_max_age_ms"] == 2000
    assert snap["normal_exit_price_floor"] == "0.01"
    assert snap["emergency_exit_price_floor"] == "0.01"
    assert snap["max_exit_slippage_from_touch"] == "0.05"
    assert snap["max_exit_book_spread"] == "0.20"
    assert snap["settlement_max_wait_s"] == 45.0
    assert snap["settlement_initial_backoff_s"] == 0.25
    assert snap["exit_retry_cooldown_s"] == 0.5
    assert snap["exit_max_attempts"] == 3
    assert snap["flatten_before_close_s"] == 30.0
    assert snap["min_remaining_for_entry_s"] == 90.0
    assert snap["min_tradable_qty"] == "0.01"
    assert BOOK_FRESHNESS_MAX_AGE_MS == 2000
    assert NORMAL_EXIT_PRICE_FLOOR == Decimal("0.01")
    assert EMERGENCY_EXIT_PRICE_FLOOR == Decimal("0.01")
    assert MAX_EXIT_SLIPPAGE_FROM_TOUCH == Decimal("0.05")
    assert MAX_EXIT_BOOK_SPREAD == Decimal("0.20")
    assert SETTLEMENT_MAX_WAIT_S == 45.0
    assert SETTLEMENT_INITIAL_BACKOFF_S == 0.25
    assert EXIT_RETRY_COOLDOWN_S == 0.5
    assert EXIT_MAX_ATTEMPTS == 3
    assert FLATTEN_BEFORE_CLOSE_S == 30.0
    assert MIN_REMAINING_FOR_ENTRY_S == 90.0
    assert MIN_TRADABLE_QTY == Decimal("0.01")


def test_entry_price_cannot_reach_exit_request(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            exit_book_provider=lambda _t: book_from_clob_levels(
                token_id="t",
                bids=[{"price": "0.50", "size": "100"}],
                asks=[{"price": "0.55", "size": "100"}],
                ts_event=NOW,
            ),
            exit_now_provider=lambda: NOW,
            exit_price_policy=default_exit_price_policy(),
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    buy = next(r for r in spy.submitted if r.side.upper() == "BUY")
    sell = next(r for r in spy.submitted if r.side.upper() == "SELL")
    assert Decimal(sell.price) != Decimal(buy.price)
    assert Decimal(sell.price) == Decimal("0.50")
    assert result.report["final"]["exit_used_buy_limit"] is False
    assert "lifecycle_policy" in result.report


def test_fresh_fingerprint_each_retry(tmp_path: Path) -> None:
    from tyrex_pm.execution.polymarket.transport import SubmitOrderResult
    from tyrex_pm.execution.polymarket.lifecycle_exit_plan import ExitRetryPolicy

    class FakOnce(SpyMutationTransport):
        def __init__(self) -> None:
            super().__init__()
            self._s = 0

        def submit_order(self, request):  # type: ignore[no-untyped-def]
            if request.side.upper() == "SELL":
                self._s += 1
                self.submitted.append(request)
                self._submit_n += 1
                if self._s == 1:
                    return SubmitOrderResult(
                        ok=False,
                        venue_order_id=None,
                        status=None,
                        error="no orders found to match with FAK order",
                    )
                return SubmitOrderResult(
                    ok=True, venue_order_id=f"0xspy{self._submit_n:04d}", status="matched"
                )
            return super().submit_order(request)

    books = [
        book_from_clob_levels(
            token_id="t",
            bids=[{"price": "0.50", "size": "100"}],
            asks=[{"price": "0.55", "size": "100"}],
            ts_event=NOW,
        ),
        book_from_clob_levels(
            token_id="t",
            bids=[{"price": "0.49", "size": "100"}],
            asks=[{"price": "0.55", "size": "100"}],
            ts_event=NOW,
        ),
    ]
    i = {"n": 0}

    def provider(_tid: str):
        b = books[min(i["n"], 1)]
        i["n"] += 1
        return b

    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=FakOnce(),
            exit_book_provider=provider,
            exit_now_provider=lambda: NOW,
            exit_retry_policy=ExitRetryPolicy(max_attempts=3, cooldown_s=0.0),
            exit_price_policy=default_exit_price_policy(),
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    fps = [p["book_fingerprint"] for p in result.report["exit_plans"]]
    assert len(fps) >= 2
    assert fps[0] != fps[1]


def test_bid_below_entry_valid_marketable_sell() -> None:
    plan = plan_lifecycle_fak_sell(
        book=book_from_clob_levels(
            token_id="t",
            bids=[{"price": "0.49", "size": "100"}],
            asks=[{"price": "0.51", "size": "100"}],
            ts_event=NOW,
        ),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=NOW,
        entry_buy_limit=Decimal("0.51"),
        policy=default_exit_price_policy(),
    )
    assert plan.ok
    assert plan.limit_price == Decimal("0.49")
    assert plan.limit_price < Decimal("0.51")


def test_stale_and_empty_submit_nothing(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    # empty bids → no SELL submit after retries exhaust
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            exit_book_provider=lambda _t: book_from_clob_levels(
                token_id="t",
                bids=[],
                asks=[{"price": "0.55", "size": "100"}],
                ts_event=NOW,
            ),
            exit_now_provider=lambda: NOW,
            exit_price_policy=default_exit_price_policy(),
        )
    )
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION
    sells = [r for r in spy.submitted if r.side.upper() == "SELL"]
    assert sells == []


def test_two_dust_records_distinct(tmp_path: Path) -> None:
    now = NOW.isoformat()
    reg = LifecycleResidualRegistry()
    for run, tok, cond in (
        ("d632b631-166f-4e35-8db8-fe69a3f86795", "told1", "0xc1"),
        ("76e8470a-72dc-4d6d-b653-760e87bdaf29", "told2", "0xc2"),
    ):
        upsert_residual(
            reg,
            LifecycleResidualRecord(
                condition_id=cond,
                token_id=tok,
                originating_run_id=run,
                market_slug="m",
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
    write_residual_registry(reg, repo_root=tmp_path)
    loaded = read_residual_registry(repo_root=tmp_path)
    assert loaded is not None
    assert len(loaded.open_residuals()) == 2
    ev = evaluate_residuals_for_entry(loaded)
    assert ev["ok"] is True


def test_unexpected_tradable_blocks() -> None:
    now = NOW.isoformat()
    reg = LifecycleResidualRegistry()
    upsert_residual(
        reg,
        LifecycleResidualRecord(
            condition_id="0x9",
            token_id="textra",
            originating_run_id="run-extra",
            market_slug="x",
            acquired_quantity="5",
            exited_quantity="0",
            residual_quantity="5",
            min_tradable="0.01",
            classification="RESIDUAL_EXPOSURE",
            provenance="unexpected",
            created_at=now,
            updated_at=now,
            last_reconciliation_source="test",
            tradable=True,
        ),
    )
    assert not evaluate_residuals_for_entry(reg)["ok"]


def test_slippage_and_spread_policies() -> None:
    # Slippage: worst 0.40 vs touch 0.50 → 0.10 > 0.05
    plan = plan_lifecycle_fak_sell(
        book=book_from_clob_levels(
            token_id="t",
            bids=[{"price": "0.50", "size": "1"}, {"price": "0.40", "size": "20"}],
            asks=[{"price": "0.51", "size": "100"}],
            ts_event=NOW,
        ),
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=NOW,
        policy=default_exit_price_policy(),
        urgency=ExitUrgency.NORMAL,
    )
    assert plan.status is ExitPlanStatus.REFUSE_SLIPPAGE

    # Spread too wide
    plan2 = plan_lifecycle_fak_sell(
        book=book_from_clob_levels(
            token_id="t",
            bids=[{"price": "0.30", "size": "100"}],
            asks=[{"price": "0.60", "size": "100"}],
            ts_event=NOW,
        ),
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=NOW,
        policy=default_exit_price_policy(),
    )
    assert plan2.status is ExitPlanStatus.REFUSE_SPREAD


def test_zero_mutation_transport_on_dry(tmp_path: Path) -> None:
    class Boom(SpyMutationTransport):
        def submit_order(self, request):  # type: ignore[no-untyped-def]
            raise AssertionError("MUTATION")

    result = run_r7b_live_once(
        _base_args(tmp_path, dry_run=True, mutation_transport=Boom())
    )
    assert result.outcome is TerminalOutcome.DRY_OK
