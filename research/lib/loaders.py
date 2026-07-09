"""Parquet data-lake loaders for M2B.4 offline lab."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

DAY_TABLES = (
    "markets",
    "ws_quality",
    "price_to_beat",
    "reference_prices",
    "trade_prices",
    "best_bid_ask",
    "tick_size_changes",
    "market_resolutions",
    "lifecycle_events",
    "btc_ticks",
    "clock_sync",
)


@dataclass
class DayPartition:
    """One normalized parquet day partition."""

    date: str
    root: Path
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    book_snapshots: dict[str, pd.DataFrame] = field(default_factory=dict)
    book_deltas: dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def partition_dir(self) -> Path:
        return self.root / f"date={self.date}"


def _partition_path(parquet_root: Path, date: str) -> Path:
    return parquet_root / f"date={date}"


def _read_table(partition: Path, name: str) -> pd.DataFrame:
    path = partition / f"{name}.parquet"
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_parquet(path)


def _load_book_tables(partition: Path) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    snaps: dict[str, pd.DataFrame] = {}
    deltas: dict[str, pd.DataFrame] = {}
    if not partition.is_dir():
        return snaps, deltas
    for child in partition.iterdir():
        if not child.is_dir() or not child.name.startswith("market_id="):
            continue
        market_id = child.name.split("=", 1)[1]
        sp = child / "book_snapshots.parquet"
        dp = child / "book_deltas.parquet"
        if sp.is_file():
            snaps[market_id] = pd.read_parquet(sp)
        if dp.is_file():
            deltas[market_id] = pd.read_parquet(dp)
    return snaps, deltas


def load_day(
    path_or_date: str | Path,
    *,
    parquet_root: Path | str | None = None,
    load_books: bool = True,
) -> DayPartition:
    """Load one hive partition by date string or partition directory path."""
    path = Path(path_or_date)
    if path.is_dir() and path.name.startswith("date="):
        partition = path
        date = path.name.split("=", 1)[1]
    elif path.is_dir() and (path / "markets.parquet").is_file():
        partition = path
        date = path.name.removeprefix("date=") if path.name.startswith("date=") else path.name
    else:
        if parquet_root is None:
            raise ValueError("parquet_root required when path_or_date is a date string")
        date = str(path_or_date)
        partition = _partition_path(Path(parquet_root), date)
    if not partition.is_dir():
        raise FileNotFoundError(f"partition not found: {partition}")

    tables = {name: _read_table(partition, name) for name in DAY_TABLES}
    snaps: dict[str, pd.DataFrame] = {}
    deltas: dict[str, pd.DataFrame] = {}
    if load_books:
        snaps, deltas = _load_book_tables(partition)
    return DayPartition(date=date, root=partition.parent, tables=tables, book_snapshots=snaps, book_deltas=deltas)


def load_days(
    dates: list[str],
    *,
    parquet_root: Path | str,
    load_books: bool = False,
) -> list[DayPartition]:
    return [load_day(d, parquet_root=parquet_root, load_books=load_books) for d in dates]


def load_partitions(
    paths: list[str | Path],
    *,
    load_books: bool = False,
) -> DayPartition:
    """Concatenate multiple day partitions into one logical partition."""
    if not paths:
        raise ValueError("paths must not be empty")
    parts = [load_day(p, load_books=load_books) for p in paths]
    merged = DayPartition(
        date="+".join(p.date for p in parts),
        root=parts[0].root,
        tables={},
        book_snapshots={},
        book_deltas={},
    )
    for name in DAY_TABLES:
        frames = [p.tables.get(name, pd.DataFrame()) for p in parts]
        frames = [f for f in frames if not f.empty]
        merged.tables[name] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if load_books:
        for p in parts:
            merged.book_snapshots.update(p.book_snapshots)
            merged.book_deltas.update(p.book_deltas)
    return merged


def table(day: DayPartition, name: str) -> pd.DataFrame:
    return day.tables.get(name, pd.DataFrame())
