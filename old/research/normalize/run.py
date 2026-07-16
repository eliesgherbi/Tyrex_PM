"""CLI entry: normalize recorded JSONL → Parquet (M2B.3 / M2B.3-A)."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tyrex_pm.core.events import EventType

from research.normalize.io import (
    EventReadStats,
    external_btc_dir,
    iter_events_from_manifest,
    list_market_dirs,
    load_coverage_report,
    load_json,
    reference_prices_dir,
    resolve_day_dir,
)
from research.normalize.quality import compute_stream_metrics, coverage_status_for_market
from research.normalize.schema import schema_for_path
from research.normalize.tables import (
    best_bid_ask,
    book_deltas,
    book_snapshots,
    btc_ticks,
    lifecycle,
    market_resolutions,
    markets,
    reference_prices,
    tick_size_changes,
    trade_prices,
    ws_quality,
)


def _require_parquet():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit(
            "pandas/pyarrow required for normalization; install: pip install 'tyrex-pm[research]'"
        ) from exc
    return pa, pq


def write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    pa, pq = _require_parquet()
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = schema_for_path(path)
    if not rows:
        if schema is not None:
            pq.write_table(schema.empty_table(), path)
        else:
            pq.write_table(pa.table({}), path)
        return
    table = pa.Table.from_pylist(rows, schema=schema) if schema is not None else pa.Table.from_pylist(rows)
    pq.write_table(table, path, use_dictionary=False)


@dataclass
class NormalizeResult:
    markets: list[dict[str, Any]] = field(default_factory=list)
    ws_quality: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_events: list[dict[str, Any]] = field(default_factory=list)
    btc_ticks: list[dict[str, Any]] = field(default_factory=list)
    clock_sync: list[dict[str, Any]] = field(default_factory=list)
    trade_prices: list[dict[str, Any]] = field(default_factory=list)
    tick_size_changes: list[dict[str, Any]] = field(default_factory=list)
    best_bid_ask: list[dict[str, Any]] = field(default_factory=list)
    market_resolutions: list[dict[str, Any]] = field(default_factory=list)
    reference_prices: list[dict[str, Any]] = field(default_factory=list)
    price_to_beat: list[dict[str, Any]] = field(default_factory=list)
    book_snapshot_count: int = 0
    book_delta_count: int = 0


def normalize_market(
    *,
    date: str,
    market_dir: Path,
    coverage: dict[str, Any] | None,
    strict: bool,
) -> tuple[NormalizeResult, list[dict[str, Any]], list[dict[str, Any]]]:
    manifest_path = market_dir / "manifest.json"
    manifest = load_json(manifest_path)
    market_id = str(manifest.get("market_id") or market_dir.name)
    stats = EventReadStats()
    events = list(iter_events_from_manifest(market_dir, manifest, strict=strict, stats=stats))
    stream_metrics = compute_stream_metrics(events)

    snap_rows: list[dict[str, Any]] = []
    delta_rows: list[dict[str, Any]] = []
    life_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    tick_rows: list[dict[str, Any]] = []
    bba_rows: list[dict[str, Any]] = []
    resolution_rows: list[dict[str, Any]] = []
    ptb_rows: list[dict[str, Any]] = []
    discovered_payload: dict[str, Any] | None = None

    for event in events:
        snap_rows.extend(book_snapshots.rows_from_event(date, market_id, event))
        delta_rows.extend(book_deltas.rows_from_event(date, market_id, event))
        life_rows.extend(lifecycle.rows_from_event(date, market_id, event))
        trade_rows.extend(trade_prices.rows_from_event(date, market_id, event))
        tick_rows.extend(tick_size_changes.rows_from_event(date, market_id, event))
        bba_rows.extend(best_bid_ask.rows_from_event(date, market_id, event))
        resolution_rows.extend(market_resolutions.rows_from_event(date, market_id, event))
        ptb_rows.extend(reference_prices.price_to_beat_rows_from_event(date, event))
        if event.event_type == EventType.MARKET_DISCOVERED and discovered_payload is None:
            discovered_payload = dict(event.payload)

    coverage_status, _ = coverage_status_for_market(market_id, coverage, manifest_event_count=len(events))
    market_row = markets.build_market_row(
        date=date,
        market_id=market_id,
        manifest=manifest,
        manifest_path=manifest_path,
        stream_metrics=stream_metrics,
        coverage=coverage,
        corrupt_row_count=stats.corrupt_row_count,
        duplicate_event_count=stats.duplicate_event_count,
    )
    market_row = markets.enrich_market_row_from_discovered(market_row, discovered_payload)
    market_row = markets.enrich_market_row_from_events(market_row, events)
    ws_row = ws_quality.build_ws_quality_row(
        date=date,
        market_id=market_id,
        manifest=manifest,
        stream_metrics=stream_metrics,
        corrupt_row_count=stats.corrupt_row_count,
        duplicate_event_count=stats.duplicate_event_count,
        coverage_status=coverage_status,
    )

    result = NormalizeResult(
        markets=[market_row],
        ws_quality=[ws_row],
        lifecycle_events=life_rows,
        trade_prices=trade_rows,
        tick_size_changes=tick_rows,
        best_bid_ask=bba_rows,
        market_resolutions=resolution_rows,
        price_to_beat=ptb_rows,
        book_snapshot_count=len(snap_rows),
        book_delta_count=len(delta_rows),
    )
    return result, snap_rows, delta_rows


def normalize_external_btc(*, date: str, ext_dir: Path, strict: bool) -> NormalizeResult:
    manifest = load_json(ext_dir / "manifest.json")
    stats = EventReadStats()
    events = list(iter_events_from_manifest(ext_dir, manifest, strict=strict, stats=stats))
    tick_rows: list[dict[str, Any]] = []
    clock_rows: list[dict[str, Any]] = []
    for event in events:
        tick_rows.extend(btc_ticks.btc_tick_rows_from_event(date, event))
        clock_rows.extend(btc_ticks.clock_sync_rows_from_event(date, event))
    return NormalizeResult(btc_ticks=tick_rows, clock_sync=clock_rows)


def normalize_reference_prices(*, date: str, ref_dir: Path, strict: bool) -> NormalizeResult:
    manifest = load_json(ref_dir / "manifest.json")
    stats = EventReadStats()
    events = list(iter_events_from_manifest(ref_dir, manifest, strict=strict, stats=stats))
    ref_rows: list[dict[str, Any]] = []
    for event in events:
        ref_rows.extend(reference_prices.reference_price_rows_from_event(date, event))
    return NormalizeResult(reference_prices=ref_rows)


def normalize_day(
    *,
    day_dir: Path,
    date: str,
    out_root: Path,
    market_id: str | None,
    include_external_btc: bool,
    strict: bool,
    overwrite: bool,
) -> dict[str, Any]:
    out_day = out_root / f"date={date}"
    if out_day.exists() and not overwrite and any(out_day.iterdir()):
        raise FileExistsError(f"output exists (use --overwrite): {out_day}")

    coverage = load_coverage_report(day_dir)
    aggregate = NormalizeResult()
    per_market_snaps: dict[str, list[dict[str, Any]]] = {}
    per_market_deltas: dict[str, list[dict[str, Any]]] = {}

    for market_dir in list_market_dirs(day_dir, market_id=market_id):
        result, snaps, deltas = normalize_market(
            date=date,
            market_dir=market_dir,
            coverage=coverage,
            strict=strict,
        )
        aggregate.markets.extend(result.markets)
        aggregate.ws_quality.extend(result.ws_quality)
        aggregate.lifecycle_events.extend(result.lifecycle_events)
        aggregate.trade_prices.extend(result.trade_prices)
        aggregate.tick_size_changes.extend(result.tick_size_changes)
        aggregate.best_bid_ask.extend(result.best_bid_ask)
        aggregate.market_resolutions.extend(result.market_resolutions)
        aggregate.price_to_beat.extend(result.price_to_beat)
        aggregate.book_snapshot_count += result.book_snapshot_count
        aggregate.book_delta_count += result.book_delta_count
        mid = str(result.markets[0]["market_id"])
        per_market_snaps[mid] = snaps
        per_market_deltas[mid] = deltas

    if include_external_btc:
        ext = external_btc_dir(day_dir)
        if ext is not None:
            ext_result = normalize_external_btc(date=date, ext_dir=ext, strict=strict)
            aggregate.btc_ticks.extend(ext_result.btc_ticks)
            aggregate.clock_sync.extend(ext_result.clock_sync)

    ref = reference_prices_dir(day_dir)
    if ref is not None:
        ref_result = normalize_reference_prices(date=date, ref_dir=ref, strict=strict)
        aggregate.reference_prices.extend(ref_result.reference_prices)
        ref_tick_count = len(ref_result.reference_prices)
        for row in aggregate.markets:
            row["reference_tick_count"] = ref_tick_count

    write_parquet(out_day / "markets.parquet", aggregate.markets)
    write_parquet(out_day / "ws_quality.parquet", aggregate.ws_quality)
    write_parquet(out_day / "lifecycle_events.parquet", aggregate.lifecycle_events)
    write_parquet(out_day / "btc_ticks.parquet", aggregate.btc_ticks)
    write_parquet(out_day / "clock_sync.parquet", aggregate.clock_sync)
    write_parquet(out_day / "trade_prices.parquet", aggregate.trade_prices)
    write_parquet(out_day / "tick_size_changes.parquet", aggregate.tick_size_changes)
    write_parquet(out_day / "best_bid_ask.parquet", aggregate.best_bid_ask)
    write_parquet(out_day / "market_resolutions.parquet", aggregate.market_resolutions)
    write_parquet(out_day / "reference_prices.parquet", aggregate.reference_prices)
    write_parquet(out_day / "price_to_beat.parquet", aggregate.price_to_beat)

    for mid, rows in per_market_snaps.items():
        mdir = out_day / f"market_id={mid}"
        write_parquet(mdir / "book_snapshots.parquet", rows)
    for mid, rows in per_market_deltas.items():
        mdir = out_day / f"market_id={mid}"
        write_parquet(mdir / "book_deltas.parquet", rows)

    summary = {
        "date": date,
        "market_count": len(aggregate.markets),
        "book_snapshot_rows": aggregate.book_snapshot_count,
        "book_delta_rows": aggregate.book_delta_count,
        "btc_tick_rows": len(aggregate.btc_ticks),
        "clock_sync_rows": len(aggregate.clock_sync),
        "trade_price_rows": len(aggregate.trade_prices),
        "tick_size_change_rows": len(aggregate.tick_size_changes),
        "best_bid_ask_rows": len(aggregate.best_bid_ask),
        "market_resolution_rows": len(aggregate.market_resolutions),
        "reference_price_rows": len(aggregate.reference_prices),
        "price_to_beat_rows": len(aggregate.price_to_beat),
        "lifecycle_rows": len(aggregate.lifecycle_events),
        "avg_gap_rate": (
            sum(r.get("gap_rate", 0.0) for r in aggregate.ws_quality) / len(aggregate.ws_quality)
            if aggregate.ws_quality
            else 0.0
        ),
        "max_dropped_events": max((r.get("dropped_events", 0) for r in aggregate.markets), default=0),
        "total_corrupt_rows": sum(r.get("corrupt_row_count", 0) for r in aggregate.markets),
        "output_dir": str(out_day),
    }
    (out_day / "normalize_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Normalize Tyrex_PM recordings to Parquet (M2B.3)")
    p.add_argument("--recordings", required=True, help="Day folder or recordings root")
    p.add_argument("--out", required=True, help="Parquet output root (e.g. var/parquet)")
    p.add_argument("--date", default=None, help="YYYY-MM-DD when --recordings is a root")
    p.add_argument("--market-id", default=None, help="Normalize one market only")
    p.add_argument("--include-external-btc", action="store_true", help="Include external/btc_binance")
    p.add_argument("--strict", action="store_true", help="Fail on corrupt event rows")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing parquet output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    day_dir, date = resolve_day_dir(Path(args.recordings), date=args.date)
    summary = normalize_day(
        day_dir=day_dir,
        date=date,
        out_root=Path(args.out),
        market_id=args.market_id,
        include_external_btc=args.include_external_btc,
        strict=args.strict,
        overwrite=args.overwrite,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
