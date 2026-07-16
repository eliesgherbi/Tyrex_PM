"""Counterfactual survivor episode utilities (M2B.4 research proxies)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import pandas as pd


def _to_decimal(value: Any) -> Decimal:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return Decimal("0")
    return Decimal(str(value))


def _parse_ts(value: Any) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return pd.to_datetime(value, utc=True).to_pydatetime()
    except Exception:
        return None


@dataclass
class PairEntryCosts:
    yes_entry: Decimal
    no_entry: Decimal

    @property
    def total(self) -> Decimal:
        return self.yes_entry + self.no_entry


@dataclass
class SurvivorEpisodeResult:
    market_id: str
    survivor_token_id: str | None
    loser_token_id: str | None
    entry_cost: Decimal
    loser_stop_proceeds: Decimal
    recovery_target: Decimal
    recovery_touched: bool
    never_armed_and_lost: bool
    mfe: Decimal
    mae: Decimal
    max_giveback: Decimal


def compute_recovery_target(*, entry_cost: Decimal, recovery_buffer: Decimal = Decimal("0")) -> Decimal:
    """Research proxy: breakeven + buffer on survivor leg (simplified)."""
    return entry_cost + recovery_buffer


def compute_loser_stop_proceeds(*, loser_entry: Decimal, stop_pct: Decimal) -> Decimal:
    """Proceeds from selling loser at stop_pct loss from entry."""
    return loser_entry * (Decimal("1") - stop_pct)


def synthetic_pair_entry(*, yes_price: Decimal, no_price: Decimal, size: Decimal) -> PairEntryCosts:
    return PairEntryCosts(yes_entry=yes_price * size, no_entry=no_price * size)


def extract_quote_path(bba: pd.DataFrame, token_id: str) -> pd.DataFrame:
    if bba.empty:
        return pd.DataFrame()
    sub = bba.loc[bba["token_id"].astype(str) == str(token_id)].copy()
    if sub.empty:
        return sub
    sub["event_ts"] = sub["source_ts"].where(sub["source_ts"].notna(), sub["recv_ts"])
    sub["best_bid"] = pd.to_numeric(sub["best_bid"], errors="coerce")
    return sub.sort_values("event_ts")


def survivor_path_after_stop(
    *,
    market_id: str,
    survivor_token_id: str,
    loser_token_id: str,
    yes_entry: Decimal,
    no_entry: Decimal,
    survivor_is_yes: bool,
    stop_pct: Decimal,
    recovery_buffer: Decimal,
    bba: pd.DataFrame,
    event_end_ts: datetime,
    size: Decimal = Decimal("5"),
) -> SurvivorEpisodeResult:
    entry = PairEntryCosts(yes_entry=yes_entry, no_entry=no_entry)
    loser_entry = yes_entry if not survivor_is_yes else no_entry
    survivor_entry = no_entry if not survivor_is_yes else yes_entry
    loser_proceeds = compute_loser_stop_proceeds(loser_entry=loser_entry, stop_pct=stop_pct)
    recovery_total = compute_recovery_target(entry_cost=entry.total, recovery_buffer=recovery_buffer)
    recovery_per_share = recovery_total / max(size, Decimal("0.0001"))

    path = extract_quote_path(bba, survivor_token_id)
    mfe = Decimal("0")
    mae = Decimal("0")
    peak = Decimal("0")
    touched = False
    last_bid = Decimal("0")

    for _, row in path.iterrows():
        ts = _parse_ts(row.get("event_ts"))
        if ts and ts > event_end_ts:
            break
        bid = _to_decimal(row.get("best_bid"))
        if bid <= 0:
            continue
        pnl = bid - (survivor_entry / max(size, Decimal("0.0001")))
        mfe = max(mfe, pnl)
        mae = min(mae, pnl)
        peak = max(peak, pnl)
        last_bid = bid
        if bid >= recovery_per_share:
            touched = True

    final_pnl = last_bid - (survivor_entry / max(size, Decimal("0.0001"))) if last_bid > 0 else Decimal("-1")
    never_armed = (not touched) and final_pnl < Decimal("0")
    giveback = peak - final_pnl if peak > 0 else Decimal("0")

    return SurvivorEpisodeResult(
        market_id=market_id,
        survivor_token_id=survivor_token_id,
        loser_token_id=loser_token_id,
        entry_cost=entry.total,
        loser_stop_proceeds=loser_proceeds,
        recovery_target=recovery_total,
        recovery_touched=touched,
        never_armed_and_lost=never_armed,
        mfe=mfe,
        mae=mae,
        max_giveback=giveback,
    )


def iterate_market_windows(markets: pd.DataFrame) -> Iterable[dict[str, Any]]:
    for _, row in markets.iterrows():
        yield {
            "market_id": row["market_id"],
            "event_start_ts": _parse_ts(row.get("event_start_ts")),
            "event_end_ts": _parse_ts(row.get("event_end_ts")),
            "yes_token_id": row.get("yes_token_id"),
            "no_token_id": row.get("no_token_id"),
            "price_to_beat": row.get("price_to_beat"),
        }


def never_armed_and_lost_rate(episodes: list[SurvivorEpisodeResult]) -> float:
    if not episodes:
        return 0.0
    return sum(1 for e in episodes if e.never_armed_and_lost) / len(episodes)
