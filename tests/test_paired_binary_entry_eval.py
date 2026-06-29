"""Paired binary entry evaluation tests (Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.ids import TokenId
from tyrex_pm.strategies.paired_binary.entry_eval import EntryEvalInput, LegBook, evaluate_entry


def _leg(tid: str, bid: str, ask: str, *, stale: bool = False) -> LegBook:
    return LegBook(
        TokenId(tid),
        Decimal(bid),
        Decimal(ask),
        stale,
    )


def _inp(yes: LegBook, no: LegBook) -> EntryEvalInput:
    return EntryEvalInput(
        yes=yes,
        no=no,
        max_pair_entry_cost=Decimal("1.02"),
        max_spread_yes=Decimal("0.02"),
        max_spread_no=Decimal("0.02"),
        pair_stop_loss_pct=Decimal("0.02"),
        slippage_buffer=Decimal("0.005"),
        reject_if_spread_exceeds_loss_budget=False,
    )


def test_entry_eval_accepts_good_pair_cost() -> None:
    yes = _leg("y", "0.48", "0.49")
    no = _leg("n", "0.50", "0.51")
    r = evaluate_entry(_inp(yes, no))
    assert r.allowed is True
    assert r.pair_cost == Decimal("1.00")


def test_entry_eval_rejects_high_pair_cost() -> None:
    yes = _leg("y", "0.55", "0.56")
    no = _leg("n", "0.55", "0.56")
    r = evaluate_entry(_inp(yes, no))
    assert r.allowed is False
    assert r.reason == rc.PAIR_COST_TOO_HIGH


def test_entry_eval_rejects_wide_yes_spread() -> None:
    yes = _leg("y", "0.40", "0.49")
    no = _leg("n", "0.50", "0.51")
    r = evaluate_entry(_inp(yes, no))
    assert r.allowed is False
    assert r.reason == rc.YES_SPREAD_TOO_WIDE


def test_entry_eval_rejects_wide_no_spread() -> None:
    yes = _leg("y", "0.48", "0.49")
    no = _leg("n", "0.40", "0.51")
    r = evaluate_entry(_inp(yes, no))
    assert r.allowed is False
    assert r.reason == rc.NO_SPREAD_TOO_WIDE


def test_entry_eval_rejects_stale_book() -> None:
    yes = _leg("y", "0.48", "0.49", stale=True)
    no = _leg("n", "0.50", "0.51")
    r = evaluate_entry(_inp(yes, no))
    assert r.allowed is False
    assert r.reason == rc.YES_BOOK_STALE
