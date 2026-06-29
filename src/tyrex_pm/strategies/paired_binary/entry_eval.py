"""Pure paired-binary entry evaluation (Phase 4.6)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.strategies.paired_binary.pnl import loss_budget, spread_exceeds_loss_budget


@dataclass(frozen=True)
class LegBook:
    token_id: TokenId
    bid: Decimal | None
    ask: Decimal | None
    stale: bool
    book_age_ms: int | None = None
    book_update_ts: str | None = None


@dataclass(frozen=True)
class EntryEvalInput:
    yes: LegBook
    no: LegBook
    max_pair_entry_cost: Decimal
    max_spread_yes: Decimal
    max_spread_no: Decimal
    pair_stop_loss_pct: Decimal
    slippage_buffer: Decimal
    reject_if_spread_exceeds_loss_budget: bool


@dataclass(frozen=True)
class EntryEvalResult:
    allowed: bool
    reason: str | None
    pair_cost: Decimal | None
    yes_spread: Decimal | None
    no_spread: Decimal | None
    estimated_loss_budget: Decimal | None = None
    slippage_buffer: Decimal | None = None


def read_leg_book(
    market_state: MarketStateStore | None,
    token_id: TokenId,
    *,
    max_book_age_s: float,
) -> LegBook:
    if market_state is None:
        return LegBook(token_id=token_id, bid=None, ask=None, stale=True)
    snap = market_state.snapshot(token_id)
    if snap is None:
        return LegBook(token_id=token_id, bid=None, ask=None, stale=True)
    stale = market_state.is_stale(token_id, max_age_s=max_book_age_s)
    age_ms = int(max(0.0, (utc_now() - snap.ts).total_seconds()) * 1000)
    return LegBook(
        token_id=token_id,
        bid=snap.best_bid,
        ask=snap.best_ask,
        stale=stale,
        book_age_ms=age_ms,
        book_update_ts=snap.ts.isoformat(),
    )


def evaluate_entry(inp: EntryEvalInput) -> EntryEvalResult:
    yes, no = inp.yes, inp.no

    if yes.stale:
        return EntryEvalResult(False, rc.YES_BOOK_STALE, None, None, None)
    if no.stale:
        return EntryEvalResult(False, rc.NO_BOOK_STALE, None, None, None)
    if yes.ask is None:
        return EntryEvalResult(False, rc.MISSING_YES_ASK, None, None, None)
    if no.ask is None:
        return EntryEvalResult(False, rc.MISSING_NO_ASK, None, None, None)
    if yes.bid is None:
        return EntryEvalResult(False, rc.MISSING_YES_BID, None, None, None)
    if no.bid is None:
        return EntryEvalResult(False, rc.MISSING_NO_BID, None, None, None)

    pair_cost = yes.ask + no.ask
    yes_spread = yes.ask - yes.bid
    no_spread = no.ask - no.bid
    estimated_loss_budget = loss_budget(pair_cost, inp.pair_stop_loss_pct)

    if pair_cost > inp.max_pair_entry_cost:
        return EntryEvalResult(
            False,
            rc.PAIR_COST_TOO_HIGH,
            pair_cost,
            yes_spread,
            no_spread,
            estimated_loss_budget,
            inp.slippage_buffer,
        )
    if yes_spread > inp.max_spread_yes:
        return EntryEvalResult(
            False,
            rc.YES_SPREAD_TOO_WIDE,
            pair_cost,
            yes_spread,
            no_spread,
            estimated_loss_budget,
            inp.slippage_buffer,
        )
    if no_spread > inp.max_spread_no:
        return EntryEvalResult(
            False,
            rc.NO_SPREAD_TOO_WIDE,
            pair_cost,
            yes_spread,
            no_spread,
            estimated_loss_budget,
            inp.slippage_buffer,
        )

    if inp.reject_if_spread_exceeds_loss_budget:
        if spread_exceeds_loss_budget(yes_spread, estimated_loss_budget, inp.slippage_buffer):
            return EntryEvalResult(
                False,
                rc.YES_SPREAD_EXCEEDS_LOSS_BUDGET,
                pair_cost,
                yes_spread,
                no_spread,
                estimated_loss_budget,
                inp.slippage_buffer,
            )
        if spread_exceeds_loss_budget(no_spread, estimated_loss_budget, inp.slippage_buffer):
            return EntryEvalResult(
                False,
                rc.NO_SPREAD_EXCEEDS_LOSS_BUDGET,
                pair_cost,
                yes_spread,
                no_spread,
                estimated_loss_budget,
                inp.slippage_buffer,
            )

    return EntryEvalResult(
        True,
        None,
        pair_cost,
        yes_spread,
        no_spread,
        estimated_loss_budget,
        inp.slippage_buffer,
    )
