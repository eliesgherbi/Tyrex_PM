"""Parquet table column definitions (M2B.3)."""

from __future__ import annotations

from typing import Any

MARKETS_COLUMNS = [
    "date",
    "market_id",
    "event_slug",
    "yes_token_id",
    "no_token_id",
    "recording_started_ts",
    "recording_ended_ts",
    "event_start_ts",
    "event_end_ts",
    "event_count",
    "segment_count",
    "compressed",
    "dropped_events",
    "coverage_status",
    "coverage_pct",
    "first_recv_ts",
    "last_recv_ts",
    "book_snapshot_count",
    "book_delta_count",
    "ws_seq_gap_count",
    "market_discovered_count",
    "max_staleness_s",
    "duplicate_event_count",
    "corrupt_row_count",
    "source_manifest_path",
]

BOOK_SNAPSHOT_COLUMNS = [
    "date",
    "market_id",
    "event_id",
    "recv_ts",
    "source_ts",
    "token_id",
    "side",
    "bid_price",
    "bid_size",
    "ask_price",
    "ask_size",
    "best_bid",
    "best_ask",
    "mid",
    "spread",
    "book_hash",
    "venue_cursor",
    "raw_json",
]

BOOK_DELTA_COLUMNS = [
    "date",
    "market_id",
    "event_id",
    "recv_ts",
    "source_ts",
    "token_id",
    "side",
    "price",
    "size",
    "best_bid",
    "best_ask",
    "mid",
    "spread",
    "book_hash",
    "venue_cursor",
    "change_type",
    "raw_json",
]

WS_QUALITY_COLUMNS = [
    "date",
    "market_id",
    "event_count",
    "ws_seq_gap_count",
    "gap_rate",
    "duplicate_event_count",
    "dropped_events",
    "corrupt_row_count",
    "first_recv_ts",
    "last_recv_ts",
    "max_inter_event_gap_s",
    "max_staleness_s",
    "recording_duration_s",
    "status",
]

BTC_TICKS_COLUMNS = [
    "date",
    "source",
    "symbol",
    "stream",
    "event_id",
    "recv_ts",
    "source_ts",
    "bid",
    "ask",
    "mid",
    "price",
    "quantity",
    "trade_id",
    "raw_json",
]

CLOCK_SYNC_COLUMNS = [
    "date",
    "source",
    "symbol",
    "event_id",
    "recv_ts",
    "source_ts",
    "latency_ms",
    "raw_json",
]

LIFECYCLE_COLUMNS = [
    "date",
    "market_id",
    "source",
    "event_type",
    "event_id",
    "recv_ts",
    "source_ts",
    "payload_json",
]


def _pa():
    import pyarrow as pa

    return pa


def _str_field(name: str):
    return _pa().field(name, _pa().string())


def _int_field(name: str):
    return _pa().field(name, _pa().int64())


def _float_field(name: str):
    return _pa().field(name, _pa().float64())


def _bool_field(name: str):
    return _pa().field(name, _pa().bool_())


def markets_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _str_field("event_slug"),
            _str_field("yes_token_id"),
            _str_field("no_token_id"),
            _str_field("recording_started_ts"),
            _str_field("recording_ended_ts"),
            _str_field("event_start_ts"),
            _str_field("event_end_ts"),
            _int_field("event_count"),
            _int_field("segment_count"),
            _bool_field("compressed"),
            _int_field("dropped_events"),
            _str_field("coverage_status"),
            _float_field("coverage_pct"),
            _str_field("first_recv_ts"),
            _str_field("last_recv_ts"),
            _int_field("book_snapshot_count"),
            _int_field("book_delta_count"),
            _int_field("ws_seq_gap_count"),
            _int_field("market_discovered_count"),
            _float_field("max_staleness_s"),
            _int_field("duplicate_event_count"),
            _int_field("corrupt_row_count"),
            _str_field("source_manifest_path"),
            _str_field("price_to_beat"),
            _str_field("price_to_beat_ts"),
            _str_field("price_to_beat_source"),
            _float_field("price_to_beat_lag_ms"),
            _str_field("final_reference_price"),
            _str_field("final_reference_price_ts"),
            _float_field("final_reference_lag_ms"),
            _str_field("direction_vs_price_to_beat"),
            _str_field("winning_asset_id"),
            _str_field("winning_outcome"),
            _str_field("resolved_ts"),
            _int_field("trade_price_count"),
            _int_field("tick_size_change_count"),
            _int_field("best_bid_ask_count"),
            _int_field("reference_tick_count"),
        ]
    )


def book_snapshot_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("token_id"),
            _str_field("side"),
            _str_field("bid_price"),
            _str_field("bid_size"),
            _str_field("ask_price"),
            _str_field("ask_size"),
            _str_field("best_bid"),
            _str_field("best_ask"),
            _str_field("mid"),
            _str_field("spread"),
            _str_field("book_hash"),
            _str_field("venue_cursor"),
            _str_field("raw_json"),
        ]
    )


def book_delta_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("token_id"),
            _str_field("side"),
            _str_field("price"),
            _str_field("size"),
            _str_field("best_bid"),
            _str_field("best_ask"),
            _str_field("mid"),
            _str_field("spread"),
            _str_field("book_hash"),
            _str_field("venue_cursor"),
            _str_field("change_type"),
            _str_field("raw_json"),
        ]
    )


def ws_quality_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _int_field("event_count"),
            _int_field("ws_seq_gap_count"),
            _float_field("gap_rate"),
            _int_field("duplicate_event_count"),
            _int_field("dropped_events"),
            _int_field("corrupt_row_count"),
            _str_field("first_recv_ts"),
            _str_field("last_recv_ts"),
            _float_field("max_inter_event_gap_s"),
            _float_field("max_staleness_s"),
            _float_field("recording_duration_s"),
            _str_field("status"),
        ]
    )


def btc_ticks_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("source"),
            _str_field("symbol"),
            _str_field("stream"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("bid"),
            _str_field("ask"),
            _str_field("mid"),
            _str_field("price"),
            _str_field("quantity"),
            _str_field("trade_id"),
            _str_field("raw_json"),
        ]
    )


def clock_sync_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("source"),
            _str_field("symbol"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _float_field("latency_ms"),
            _str_field("raw_json"),
        ]
    )


def lifecycle_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _str_field("source"),
            _str_field("event_type"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("payload_json"),
        ]
    )


def trade_prices_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _str_field("token_id"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("price"),
            _str_field("size"),
            _str_field("side"),
            _str_field("fee_rate_bps"),
            _str_field("transaction_hash"),
            _str_field("raw_json"),
        ]
    )


def tick_size_changes_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _str_field("token_id"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("old_tick_size"),
            _str_field("new_tick_size"),
            _str_field("raw_json"),
        ]
    )


def best_bid_ask_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _str_field("token_id"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("best_bid"),
            _str_field("best_ask"),
            _str_field("spread"),
            _str_field("raw_json"),
        ]
    )


def market_resolutions_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("winning_asset_id"),
            _str_field("winning_outcome"),
            _str_field("resolved_ts"),
            _str_field("raw_json"),
        ]
    )


def reference_prices_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("source"),
            _str_field("feed"),
            _str_field("symbol"),
            _str_field("event_id"),
            _str_field("recv_ts"),
            _str_field("source_ts"),
            _str_field("value"),
            _float_field("latency_ms"),
            _str_field("raw_json"),
        ]
    )


def price_to_beat_schema():
    pa = _pa()
    return pa.schema(
        [
            _str_field("date"),
            _str_field("market_id"),
            _float_field("event_start_ts"),
            _float_field("event_end_ts"),
            _str_field("price_to_beat"),
            _str_field("price_to_beat_ts"),
            _str_field("price_to_beat_source"),
            _float_field("price_to_beat_lag_ms"),
            _str_field("raw_reference_event_id"),
            _str_field("status"),
            _str_field("final_reference_price"),
            _str_field("final_reference_price_ts"),
            _float_field("final_reference_lag_ms"),
            _str_field("direction_vs_price_to_beat"),
            _str_field("raw_json"),
        ]
    )


_SCHEMA_BY_BASENAME: dict[str, Any] = {}


def schema_for_path(path) -> Any | None:
    from pathlib import Path

    name = Path(path).name
    if not _SCHEMA_BY_BASENAME:
        _SCHEMA_BY_BASENAME.update(
            {
                "markets.parquet": markets_schema(),
                "book_snapshots.parquet": book_snapshot_schema(),
                "book_deltas.parquet": book_delta_schema(),
                "ws_quality.parquet": ws_quality_schema(),
                "btc_ticks.parquet": btc_ticks_schema(),
                "clock_sync.parquet": clock_sync_schema(),
                "lifecycle_events.parquet": lifecycle_schema(),
                "trade_prices.parquet": trade_prices_schema(),
                "tick_size_changes.parquet": tick_size_changes_schema(),
                "best_bid_ask.parquet": best_bid_ask_schema(),
                "market_resolutions.parquet": market_resolutions_schema(),
                "reference_prices.parquet": reference_prices_schema(),
                "price_to_beat.parquet": price_to_beat_schema(),
            }
        )
    return _SCHEMA_BY_BASENAME.get(name)
