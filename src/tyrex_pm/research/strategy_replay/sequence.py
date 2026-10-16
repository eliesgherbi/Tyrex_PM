"""Ordered multi-window replay with one shared account (continuous-runner economics).

Independent one-window replays each start with the scenario's full cash. A continuous
runner does not: cash spent on a holding stays locked until its payout is proven *and*
redeemed, early-sale proceeds return at once, and account-wide limits pause entries.
``run_sequence`` replays windows in chronological order through ``run_window`` with:

* starting cash = initial cash + realized sale proceeds - debits + payouts released
  before the window starts (release = label availability + ``redemption_delay_s``,
  the assumed operator redemption lag; receivables are never spendable);
* entries skipped (recorded, not traded) when a declared limit is reached before the
  window: unresolved holdings count, unredeemed payouts, available collateral below the
  per-episode debit cap, daily (UTC) loss including all open worst-case loss, or a
  consecutive-loss pause.

The per-window strategy and execution simulation is unchanged; this layer only decides
what the shared account allows. It is research evidence about capital continuity, not a
prediction of live fills.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .loader import WindowData

SEQUENCE_VERSION = "ordered_window_capital_v1"
NS = 1_000_000_000


@dataclass(frozen=True)
class SequenceLimits:
    max_debit_per_episode: Decimal = Decimal("5")
    max_unresolved_count: int = 3
    max_unredeemed_count: int = 3
    daily_loss_limit: Decimal | None = None
    max_consecutive_losses: int = 0
    # Assumed operator redemption lag after a payout becomes provable [seconds].
    redemption_delay_s: float = 3600.0


@dataclass
class _Holding:
    window: str
    cost: Decimal
    payout: Decimal | None
    proven_at_ns: int | None
    released_at_ns: int | None


def _day(ns: int):
    return datetime.fromtimestamp(ns / NS, tz=timezone.utc).date()


def run_sequence(
    windows: Sequence[WindowData],
    *,
    strategy_config: Any,
    scenario: Any,
    limits: SequenceLimits,
    initial_cash: Decimal = Decimal("100"),
    run_one: Callable[..., dict] | None = None,
    clock_observations: Callable[[WindowData], list[dict] | None] | None = None,
    execution_policy: Any = None,
) -> dict:
    """Replay ``windows`` (any order; sorted by start) through one shared account."""
    from dataclasses import replace

    if run_one is None:
        from .run import run_window as run_one  # noqa: PLW0127
    ordered = sorted(windows, key=lambda w: w.ref.window_start_ns)
    cash = initial_cash
    holdings: list[_Holding] = []
    realized_by_day: dict[Any, Decimal] = {}
    outcomes: list[Decimal] = []
    rows: list[dict] = []
    for window in ordered:
        start = window.ref.window_start_ns
        # Release payouts redeemed before this window starts.
        for h in holdings:
            if h.released_at_ns is not None and h.released_at_ns <= start and h.payout is not None:
                cash += h.payout
                pnl = h.payout - h.cost
                day = _day(h.released_at_ns)
                realized_by_day[day] = realized_by_day.get(day, Decimal(0)) + min(pnl, Decimal(0))
                outcomes.append(pnl)
                h.payout = None  # consumed
        holdings = [h for h in holdings if h.payout is not None or h.released_at_ns is None]
        unresolved = [h for h in holdings if h.released_at_ns is None or h.released_at_ns > start]
        unredeemed = [
            h for h in unresolved if h.proven_at_ns is not None and h.proven_at_ns <= start
        ]
        open_risk = sum((h.cost for h in unresolved), Decimal(0))
        skip: str | None = None
        if len(unresolved) >= limits.max_unresolved_count:
            skip = "MAX_UNRESOLVED_COUNT"
        elif len(unredeemed) >= limits.max_unredeemed_count:
            skip = "MAX_UNREDEEMED_COUNT"
        elif cash < limits.max_debit_per_episode:
            skip = "AVAILABLE_COLLATERAL_INSUFFICIENT"
        elif limits.daily_loss_limit is not None and (
            -realized_by_day.get(_day(start), Decimal(0)) + open_risk
            + limits.max_debit_per_episode
            > limits.daily_loss_limit
        ):
            skip = "DAILY_LOSS_LIMIT"
        elif limits.max_consecutive_losses:
            trailing = 0
            for pnl in reversed(outcomes):
                if pnl >= 0:
                    break
                trailing += 1
            if trailing >= limits.max_consecutive_losses:
                skip = "CONSECUTIVE_LOSS_PAUSE"
        row: dict[str, Any] = {
            "market_id": window.ref.market_id,
            "slug": window.ref.slug,
            "window_start_ns": start,
            "cash_at_start": str(cash),
            "unresolved_at_start": len(unresolved),
            "unredeemed_at_start": len(unredeemed),
            "skipped": skip,
        }
        if skip is None:
            kwargs: dict[str, Any] = {
                "strategy_config": strategy_config,
                "scenario": replace(scenario, initial_cash=str(cash)),
            }
            if clock_observations is not None:
                kwargs["clock_observations"] = clock_observations(window)
            if execution_policy is not None:
                kwargs["execution_policy"] = execution_policy
            result = run_one(window, **kwargs)
            econ = result["economics"]
            debit = Decimal(str(econ.get("debit") or 0))
            proceeds = Decimal(str(econ.get("exit_proceeds_net") or 0))
            held = Decimal(str(econ.get("final_net_shares") or 0))
            cash += proceeds - debit
            label = result.get("label", {})
            payout = econ.get("payout")
            if held > 0:
                available = label.get("available_at_ns")
                proven = None if available is None else int(available)
                holdings.append(
                    _Holding(
                        window=window.ref.slug,
                        cost=debit - proceeds if debit > proceeds else Decimal(0),
                        payout=None if payout is None else Decimal(str(payout)),
                        proven_at_ns=proven,
                        released_at_ns=None
                        if proven is None
                        else proven + int(limits.redemption_delay_s * NS),
                    )
                )
            elif econ.get("filled"):
                pnl = proceeds - debit
                day = _day(start)
                realized_by_day[day] = realized_by_day.get(day, Decimal(0)) + min(pnl, Decimal(0))
                outcomes.append(pnl)
            rejected = [
                a for a in result.get("attempts", [])
                if a.get("outcome_reason") == "insufficient_available_cash"
            ]
            row.update(
                entered=bool(econ.get("entered")),
                filled=bool(econ.get("filled")),
                debit=str(debit),
                exit_proceeds=str(proceeds),
                held_shares=str(held),
                realized_pnl=econ.get("realized_pnl"),
                cash_rejections=len(rejected),
                runner_version=result.get("runner_version"),
            )
        rows.append(row)
    locked = sum((h.cost for h in holdings if h.payout is not None or h.released_at_ns is None),
                 Decimal(0))
    return {
        "sequence_version": SEQUENCE_VERSION,
        "limits": {k: str(v) for k, v in asdict(limits).items()},
        "initial_cash": str(initial_cash),
        "final_cash": str(cash),
        "locked_in_unredeemed_holdings": str(locked),
        "windows": rows,
        "skipped_by_reason": _count(r["skipped"] for r in rows if r["skipped"]),
        "traded_windows": sum(1 for r in rows if r.get("filled")),
    }


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out
