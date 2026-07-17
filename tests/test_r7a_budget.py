"""R7A LiveBudgetGuard tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.execution.polymarket.live_budget import BudgetError, LiveBudgetGuard


def test_exactly_5_accepted(tmp_path: Path) -> None:
    g = LiveBudgetGuard(path=tmp_path / "b.json")
    g.bind_approval(
        approval_artifact_id="a1",
        market_id="m1",
        instrument_id="t1",
        max_buy_notional=Decimal("5.00"),
    )
    ok, why = g.can_reserve_entry(Decimal("5.00"))
    assert ok and why == "OK"
    g.reserve_working(Decimal("5.00"))
    assert g.state.remaining == Decimal("0")


def test_more_than_5_denied(tmp_path: Path) -> None:
    g = LiveBudgetGuard(path=tmp_path / "b.json")
    g.bind_approval(
        approval_artifact_id="a1", market_id="m1", instrument_id="t1"
    )
    ok, why = g.can_reserve_entry(Decimal("5.01"))
    assert not ok
    assert why == "EXCEEDS_REMAINING_BUDGET"


def test_unknown_submission_reserves_full(tmp_path: Path) -> None:
    g = LiveBudgetGuard(path=tmp_path / "b.json")
    g.bind_approval(
        approval_artifact_id="a1", market_id="m1", instrument_id="t1"
    )
    g.reserve_working(Decimal("5.00"))
    g.mark_uncertain(Decimal("5.00"))
    assert g.state.uncertain_buy_notional == Decimal("5.00")
    assert g.state.working_buy_notional == Decimal("0")
    ok, _ = g.can_reserve_entry(Decimal("0.01"))
    assert not ok


def test_restart_preserves_budget(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    g = LiveBudgetGuard(path=path)
    g.bind_approval(
        approval_artifact_id="a1", market_id="m1", instrument_id="t1"
    )
    g.reserve_working(Decimal("3"))
    g.apply_fill(Decimal("3"))
    g2 = LiveBudgetGuard(path=path)
    g2.load()
    assert g2.state.filled_buy_notional == Decimal("3")
    assert g2.state.entry_authorization_consumed is True
    ok, why = g2.can_reserve_entry(Decimal("1"))
    assert not ok
    assert why == "ENTRY_AUTHORIZATION_CONSUMED"


def test_second_entry_denied(tmp_path: Path) -> None:
    g = LiveBudgetGuard(path=tmp_path / "b.json")
    g.bind_approval(
        approval_artifact_id="a1", market_id="m1", instrument_id="t1"
    )
    g.reserve_working(Decimal("2"))
    with pytest.raises(BudgetError, match="SECOND_ENTRY|EXCEEDS|AUTHORIZATION"):
        # After one attempt with residual working, second reserve denied
        g.reserve_working(Decimal("1"))


def test_bind_rejects_over_5(tmp_path: Path) -> None:
    g = LiveBudgetGuard(path=tmp_path / "b.json")
    with pytest.raises(BudgetError, match="exceeds"):
        g.bind_approval(
            approval_artifact_id="a1",
            market_id="m1",
            instrument_id="t1",
            max_buy_notional=Decimal("5.01"),
        )


def test_new_market_cannot_reset_budget(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    g = LiveBudgetGuard(path=path)
    g.bind_approval(
        approval_artifact_id="a1", market_id="m1", instrument_id="t1"
    )
    g.reserve_working(Decimal("5.00"))
    g.apply_fill(Decimal("5.00"))
    g2 = LiveBudgetGuard(path=path)
    g2.load()
    with pytest.raises(BudgetError):
        g2.bind_approval(
            approval_artifact_id="a2",
            market_id="m2",
            instrument_id="t2",
            max_buy_notional=Decimal("5.00"),
        )
