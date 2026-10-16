"""Shared teaching calculations; production Books remains the reconstruction authority."""

from __future__ import annotations

import heapq
from decimal import Decimal, localcontext
from functools import cached_property

import numpy as np
import pandas as pd

from tyrex_pm.data_warehouse.contracts import decode
from tyrex_pm.data_warehouse.query.reader import BINANCE, order
from tyrex_pm.data_warehouse.reconstruction.books import Books

from .data import NS, Sample

TAPE_COLUMNS = [
    "received_at",
    "record_id",
    "instrument_id",
    "label",
    "valid",
    "reason",
    "epoch_id",
    "session_id",
    "connection_id",
    "assurance",
    "book_updated_at",
    "bid",
    "ask",
    "bid_quantity",
    "ask_quantity",
    "mid",
    "spread",
    "imbalance",
    "ofi",
    "event_kind",
]


def plot_style() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.figsize": (10, 4),
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.2,
            "font.size": 10,
        }
    )


def finish_plot(fig, sample: Sample, note: str = "") -> None:
    """All figures identify the source, interval and any transformation."""
    import matplotlib.pyplot as plt

    start = pd.Timestamp(sample.start, tz="UTC")
    end = pd.Timestamp(sample.end, tz="UTC")
    caption = (
        f"Recorded collection {sample.manifest['collection_id'][:8]} | "
        f"{start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC | {note}"
    )
    fig.text(0.01, 0.01, caption, fontsize=8, wrap=True)
    fig.tight_layout(rect=(0, 0.09, 1, 1))
    plt.show()


def _summary(book, iid: str, label: str) -> dict:
    usable = bool(book and book.valid and book.bids and book.asks)
    bid = float(max(book.bids)) if usable else np.nan
    ask = float(min(book.asks)) if usable else np.nan
    bq = float(book.bids[max(book.bids)]) if usable else np.nan
    aq = float(book.asks[min(book.asks)]) if usable else np.nan
    return {
        "instrument_id": iid,
        "label": label,
        "valid": usable,
        "reason": (
            "one_sided_or_empty"
            if book and book.valid and not usable
            else book.reason
            if book
            else "no_seed"
        ),
        "epoch_id": book.epoch_id if book else None,
        "session_id": book.session_id if book else None,
        "connection_id": book.connection_id if book else None,
        "assurance": book.assurance if book else "UNUSABLE",
        "book_updated_at": book.last_update if book else None,
        "bid": bid,
        "ask": ask,
        "bid_quantity": bq,
        "ask_quantity": aq,
        "mid": (bid + ask) / 2,
        "spread": ask - bid,
        "imbalance": (bq - aq) / (bq + aq) if usable and bq + aq > 0 else np.nan,
    }


def ofi_increment(previous: dict, current: dict) -> float:
    """Cont-style top-of-book OFI in shares, only inside one usable socket epoch."""
    key = ("epoch_id", "session_id", "connection_id")
    if not previous["valid"] or not current["valid"]:
        return np.nan
    if any(previous[k] != current[k] for k in key):
        return np.nan
    b, a = current["bid"], current["ask"]
    pb, pa = previous["bid"], previous["ask"]
    return float(
        (b >= pb) * current["bid_quantity"]
        - (b <= pb) * previous["bid_quantity"]
        - (a <= pa) * current["ask_quantity"]
        + (a >= pa) * previous["ask_quantity"]
    )


class BookStudy:
    """One causal pass over two outcome books, reused by every lesson."""

    def __init__(self, sample: Sample):
        self.sample = sample

    def events(self):
        def tagged(dataset):
            for row in self.sample.records(dataset):
                yield dataset, row

        yield from heapq.merge(
            *(tagged(d) for d in ("book_snapshots", "book_updates", "operational_events")),
            key=lambda item: order(item[1]),
        )

    @cached_property
    def reconstructed(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        state = Books(retain_epochs=False)
        tape, flow = [], []
        ids = self.sample.manifest["instrument_ids"]
        history_row = None
        entered_window = False

        def append_history_state():
            if history_row is not None:
                for iid in ids:
                    tape.append(
                        {
                            **_summary(state.books.get(iid), iid, self.sample.labels[iid]),
                            "received_at": history_row["received_at"],
                            "record_id": history_row["record_id"],
                            "event_kind": "state_at_history_horizon",
                            "ofi": np.nan,
                        }
                    )

        for dataset, row in self.events():
            if dataset == "book_snapshots" and row["snapshot_origin"] == "rest_audit":
                continue
            if row["received_at"] < self.sample.start:
                state.apply(dataset, row)
                history_row = row
                continue
            if not entered_window:
                entered_window = True
                append_history_state()
            touched = (
                ids
                if dataset == "operational_events"
                else [row["instrument_id"]]
                if dataset == "book_snapshots"
                else sorted({c["instrument_id"] for c in row["changes"]})
            )
            before = {
                iid: _summary(state.books.get(iid), iid, self.sample.labels.get(iid, iid))
                for iid in touched
            }
            deltas = []
            if dataset == "book_updates":
                # Repeated changes to a level in one payload use its final quantity.
                final = {(c["instrument_id"], c["side"], c["price"]): c for c in row["changes"]}
                for (iid, side, price), change in final.items():
                    stream = state.streams.get((iid, row["session_id"], row["connection_id"]))
                    if stream is not None and stream is state.books.get(iid) and stream.valid:
                        levels = stream.bids if side == "BUY" else stream.asks
                        deltas.append(
                            (
                                iid,
                                side,
                                price,
                                levels.get(price, Decimal(0)),
                                change["new_quantity"],
                                stream.epoch_id,
                            )
                        )
            state.apply(dataset, row)
            after = {
                iid: _summary(state.books.get(iid), iid, self.sample.labels.get(iid, iid))
                for iid in touched
            }
            for iid, current in after.items():
                tape.append(
                    {
                        **current,
                        "received_at": row["received_at"],
                        "record_id": row["record_id"],
                        "event_kind": dataset,
                        "ofi": ofi_increment(before[iid], current)
                        if dataset == "book_updates"
                        else np.nan,
                    }
                )
            for iid, side, price, old, new, epoch in deltas:
                current = after[iid]
                if current["valid"] and current["epoch_id"] == epoch:
                    flow.append(
                        {
                            "received_at": row["received_at"],
                            "instrument_id": iid,
                            "side": side,
                            "price": float(price),
                            "quantity_change": float(new - old),
                            "epoch_id": epoch,
                            "record_id": row["record_id"],
                        }
                    )
        if not entered_window:
            append_history_state()
        quotes = pd.DataFrame(tape, columns=TAPE_COLUMNS)
        for column in ("received_at", "book_updated_at"):
            # nullable integer avoids nanosecond loss through float conversion
            values = pd.array([r[column] for r in tape], dtype="Int64")
            quotes[column] = pd.to_datetime(values, unit="ns", utc=True)
        changes = pd.DataFrame(
            flow,
            columns=[
                "received_at",
                "instrument_id",
                "side",
                "price",
                "quantity_change",
                "epoch_id",
                "record_id",
            ],
        )
        changes["received_at"] = pd.to_datetime(changes["received_at"], unit="ns", utc=True)
        return quotes, changes

    @property
    def tape(self) -> pd.DataFrame:
        return self.reconstructed[0]

    @property
    def level_changes(self) -> pd.DataFrame:
        return self.reconstructed[1]

    def grid(self, seconds: int = 1, max_age_s: float = 5) -> pd.DataFrame:
        """Backward as-of sampling. Explicit invalid rows are never skipped/filled over."""
        times = pd.date_range(
            pd.Timestamp(self.sample.start, tz="UTC"),
            pd.Timestamp(self.sample.end, tz="UTC"),
            freq=f"{seconds}s",
            inclusive="left",
        )
        frames = []
        for iid in self.sample.manifest["instrument_ids"]:
            history = (
                self.tape[self.tape.instrument_id == iid]
                .sort_values("received_at", kind="stable")
                .rename(columns={"received_at": "observation_at"})
            )
            matched = pd.merge_asof(
                pd.DataFrame({"time": times}),
                history,
                left_on="time",
                right_on="observation_at",
                direction="backward",
            )
            matched["instrument_id"] = iid
            matched["label"] = self.sample.labels[iid]
            matched["age_s"] = (matched.time - matched.book_updated_at).dt.total_seconds()
            matched["analysis_eligible"] = matched.valid.fillna(False).astype(
                bool
            ) & matched.age_s.between(0, max_age_s)
            matched.loc[
                ~matched.analysis_eligible,
                ["bid", "ask", "mid", "spread", "imbalance", "bid_quantity", "ask_quantity"],
            ] = np.nan
            frames.append(matched)
        return pd.concat(frames, ignore_index=True)

    def book_at(self, iid: str, received_at: int) -> dict:
        if not self.sample.start <= received_at < self.sample.end:
            raise ValueError("Choose a time inside the tutorial window.")
        state = Books(retain_epochs=False)
        for dataset, row in self.events():
            if row["received_at"] > received_at:
                break
            state.apply(dataset, row)
        book = state.books.get(iid)
        if not book or not book.valid or not book.bids or not book.asks:
            raise ValueError("No usable two-sided book at this time. Choose another valid time.")
        return book.view()


def paired_quotes(grid: pd.DataFrame) -> pd.DataFrame:
    """Compare simultaneous grid points; eligibility and freshness are inherited."""
    labels = {str(label).lower(): label for label in grid.label.unique()}
    if "up" not in labels or "down" not in labels:
        raise ValueError("Both labeled Up and Down outcomes are needed.")
    up = grid[grid.label == labels["up"]].set_index("time")
    down = grid[grid.label == labels["down"]].set_index("time")
    return pd.DataFrame(
        {
            "ask_sum": up.ask + down.ask,
            "bid_sum": up.bid + down.bid,
            "mid_sum": up.mid + down.mid,
            "both_eligible": up.analysis_eligible & down.analysis_eligible,
        }
    )


def walk_book(book: dict, shares, side: str = "buy") -> dict:
    """Exact displayed-depth arithmetic, not a fill prediction or fee calculation."""
    amount = Decimal(str(shares))
    if not amount.is_finite() or amount <= 0 or side not in ("buy", "sell"):
        raise ValueError("Positive finite shares and side buy/sell required.")
    if not book.get("valid"):
        raise ValueError("Cannot walk an invalid book.")
    levels = sorted(
        book["asks" if side == "buy" else "bids"], key=lambda r: r["price"], reverse=side == "sell"
    )
    with localcontext() as context:
        context.prec = 100
        remaining, cash = amount, Decimal(0)
        for level in levels:
            quantity = min(remaining, level["quantity"])
            cash += quantity * level["price"]
            remaining -= quantity
            if remaining == 0:
                break
        filled = amount - remaining
        return {
            "requested_shares": amount,
            "displayed_fill_shares": filled,
            "unfilled_shares": remaining,
            "cash_before_fees": cash,
            "vwap": cash / filled if filled else None,
            "complete": remaining == 0,
        }


def trade_frame(sample: Sample, iid: str = BINANCE) -> pd.DataFrame:
    frame = sample.frame("trade_prints")
    frame = frame[frame.instrument_id == iid].copy()
    frame["price_float"] = frame.price.map(lambda x: float(x) if x is not None else np.nan)
    frame["quantity_float"] = frame.quantity.map(lambda x: float(x) if x is not None else np.nan)
    frame["time"] = frame.received_at
    return frame.sort_values("time", kind="stable")


def btc_grid(sample: Sample, seconds: int = 1, max_age_s: float = 2) -> pd.DataFrame:
    """Decision-time last trade, no future observations and no indefinite forward filling."""
    trades = trade_frame(sample)
    times = pd.date_range(
        pd.Timestamp(sample.start, tz="UTC"),
        pd.Timestamp(sample.end, tz="UTC"),
        freq=f"{seconds}s",
        inclusive="left",
    )
    left = pd.DataFrame({"time": times})
    right = trades[["time", "price_float", "session_id", "connection_id"]].rename(
        columns={"time": "trade_at"}
    )
    out = pd.merge_asof(left, right, left_on="time", right_on="trade_at", direction="backward")
    out["age_s"] = (out.time - out.trade_at).dt.total_seconds()
    out["eligible"] = out.age_s.between(0, max_age_s) & (out.price_float > 0)
    out.loc[~out.eligible, "price_float"] = np.nan
    # A return is suppressed across observed transport breaks, even if the break is short.
    ops = sample.records("operational_events")
    breaks = []
    for row in ops:
        event = decode(row["details"])
        kind = event.get("type")
        if kind in ("session_started", "session_interrupted", "session_stopped") or (
            kind in ("disconnected", "sequence_gap") and event.get("source") == "binance"
        ):
            breaks.append(row["received_at"])
    out["segment"] = np.searchsorted(sorted(breaks), [t.value for t in out.time], side="right")
    price = out.price_float
    same_stream = (
        (out.session_id == out.session_id.shift())
        & (out.connection_id == out.connection_id.shift())
        & (out.segment == out.segment.shift())
    )
    out["log_return_bps"] = (np.log(price) - np.log(price.shift())).where(same_stream) * 10_000
    return out


def observed_ofi_grid(tape: pd.DataFrame, grid: pd.DataFrame) -> pd.Series:
    """Past-second observed increments; resnapshot changes are excluded, not invented.

    Individual increments stay inside their seed epochs. A bin may contain fresh
    snapshots on one socket; its sum is an incomplete observed-flow measure.
    Bins containing unusable states or socket changes are suppressed.
    """
    if tape.empty:
        return pd.Series(np.nan, index=grid.index)
    events = tape.copy()
    events["time"] = events.received_at.dt.ceil("1s")
    groups = events.groupby(["time", "instrument_id"], sort=False)
    summary = groups.agg(
        valid=("valid", "all"),
        sessions=("session_id", "nunique"),
        connections=("connection_id", "nunique"),
        session_id=("session_id", "last"),
        connection_id=("connection_id", "last"),
    )
    summary["observed_ofi_shares"] = groups.ofi.sum(min_count=1)
    matched = grid[
        ["time", "instrument_id", "session_id", "connection_id", "analysis_eligible"]
    ].merge(
        summary.reset_index(), on=["time", "instrument_id"], how="left", suffixes=("", "_events")
    )
    eligible = (
        matched.analysis_eligible
        & matched.valid.fillna(False).astype(bool)
        & matched.sessions.eq(1)
        & matched.connections.eq(1)
        & matched.session_id.eq(matched.session_id_events)
        & matched.connection_id.eq(matched.connection_id_events)
    )
    return pd.Series(matched.observed_ofi_shares.where(eligible).to_numpy(), index=grid.index)


def strict_forward_move(
    grid: pd.DataFrame,
    horizon_s: int = 5,
    events: pd.DataFrame | None = None,
    *,
    require_same_seed: bool = True,
) -> pd.Series:
    """Forward quote diagnostic; all sampled points must be usable on one socket.

    Default also requires one seed epoch. With require_same_seed=False, fresh
    usable resnapshots can provide later prices on the same session/socket.
    This option does not attest provider completeness or treat snapshots as OFI.
    """
    if not isinstance(horizon_s, int) or horizon_s <= 0:
        raise ValueError("Use a positive integer forward horizon.")
    if not grid.time.diff().dropna().eq(pd.Timedelta(seconds=1)).all():
        raise ValueError("Forward movement requires a one-second, single-instrument grid.")
    eligible = grid.analysis_eligible & grid.mid.notna()
    columns = ("session_id", "connection_id") + (("epoch_id",) if require_same_seed else ())
    for offset in range(1, horizon_s + 1):
        eligible &= grid.analysis_eligible.shift(-offset, fill_value=False)
        for column in columns:
            eligible &= grid[column].eq(grid[column].shift(-offset))
    if events is not None and not events.empty:
        bad = ~events.valid
        if require_same_seed:
            bad |= events.event_kind.eq("book_snapshots")
        for column in columns:
            bad |= events[column].ne(events[column].shift())
        bad_times = np.sort(np.array([t.value for t in events.loc[bad, "received_at"]]))
        starts = np.array([t.value for t in grid.time])
        ends = starts + horizon_s * NS
        count = np.searchsorted(bad_times, ends, side="right") - np.searchsorted(
            bad_times, starts, side="right"
        )
        eligible &= count == 0
    return (grid.mid.shift(-horizon_s) - grid.mid).where(eligible)


def notebook_inventory(sample: Sample) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "dataset": f["dataset"],
                "rows": f["rows"],
                "size_KiB": (sample.path / f["path"]).stat().st_size / 1024,
            }
            for f in sample.manifest["files"]
        ]
    )
