"""Analytical safeguards: causal sampling, observed gaps, exact depth and integrity."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from tyrex_pm.data_warehouse.contracts import file_digest  # noqa: E402
from tyrex_pm.research.tuto_btc5m.analysis import (  # noqa: E402
    BookStudy,
    btc_grid,
    observed_ofi_grid,
    ofi_increment,
    strict_forward_move,
    walk_book,
)
from tyrex_pm.research.tuto_btc5m.data import NS, Sample  # noqa: E402

T = 1790869500 * NS
IID = "polymarket:137:outcome:1"


def event(at, kind, **fields):
    return {
        "received_at": T + at,
        "session_id": "session",
        "connection_id": "socket",
        "source_id": "clob",
        "ingest_sequence": at,
        "payload_path": "$",
        "record_id": str(at) + kind,
        **fields,
    }


def fake_sample(datasets, length=4):
    sample = SimpleNamespace(
        start=T,
        end=T + length * NS,
        labels={IID: "Up"},
        manifest={"instrument_ids": [IID]},
        records=lambda name: datasets.get(name, []),
    )
    sample.frame = lambda name: pd.DataFrame(datasets.get(name, []))
    return sample


def seed(at=-NS):
    return event(
        at,
        "snapshot",
        instrument_id=IID,
        snapshot_origin="websocket",
        provider_update_id=None,
        bids=[{"price": Decimal("0.49"), "quantity": Decimal("10")}],
        asks=[{"price": Decimal("0.51"), "quantity": Decimal("20")}],
    )


def test_quote_grid_is_causal_and_never_fills_over_disconnect():
    disconnect = event(
        NS // 2,
        "operation",
        details=json.dumps({"type": "disconnected", "source": "clob", "connection_id": "socket"}),
    )
    study = BookStudy(
        fake_sample({"book_snapshots": [seed(), seed(2 * NS)], "operational_events": [disconnect]})
    )
    grid = study.grid(max_age_s=10)
    assert grid.analysis_eligible.tolist() == [True, False, True, True]
    assert pd.isna(grid.loc[1, "mid"])
    assert (grid.observation_at <= grid.time).all()
    assert grid.loc[0, "book_updated_at"].value == T - NS
    assert grid.loc[0, "epoch_id"] != grid.loc[2, "epoch_id"]


def test_an_unused_socket_update_does_not_create_displayed_flow():
    change = event(
        NS,
        "update",
        connection_id="unseeded_socket",
        changes=[
            {
                "instrument_id": IID,
                "side": "BUY",
                "price": Decimal("0.49"),
                "new_quantity": Decimal("1000"),
            }
        ],
    )
    study = BookStudy(fake_sample({"book_snapshots": [seed()], "book_updates": [change]}))
    assert study.level_changes.empty
    assert study.grid(max_age_s=10).loc[1, "bid_quantity"] == 10


def test_quiet_book_keeps_its_causal_seed_then_fails_freshness_filter():
    study = BookStudy(fake_sample({"book_snapshots": [seed()]}))
    grid = study.grid(max_age_s=2)
    assert grid.analysis_eligible.tolist() == [True, True, False, False]
    assert grid.loc[0, "mid"] == 0.5


def test_walk_depth_exactness_partial_fill_and_invalid_book():
    book = {
        "valid": True,
        "bids": [],
        "asks": [
            {"price": Decimal("0.700000000000000000000000000001"), "quantity": Decimal("10")},
            {"price": Decimal("0.71"), "quantity": Decimal("20")},
        ],
    }
    result = walk_book(book, 50)
    assert result["displayed_fill_shares"] == 30
    assert result["unfilled_shares"] == 20
    assert result["cash_before_fees"] == Decimal("21.200000000000000000000000000010")
    assert not result["complete"]
    with pytest.raises(ValueError, match="invalid"):
        walk_book({**book, "valid": False}, 10)


def test_ofi_changes_at_best_price_and_resets_after_seed_change():
    before = {
        "valid": True,
        "epoch_id": "a",
        "session_id": "s",
        "connection_id": "c",
        "bid": 0.49,
        "ask": 0.51,
        "bid_quantity": 10,
        "ask_quantity": 20,
    }
    assert ofi_increment(before, {**before, "bid_quantity": 15, "ask_quantity": 17}) == 8
    assert ofi_increment(before, {**before, "bid": 0.50, "bid_quantity": 7}) == 7
    assert pd.isna(ofi_increment(before, {**before, "epoch_id": "b"}))


def test_forward_label_rejects_subsecond_gap_even_if_grid_endpoints_are_valid():
    times = pd.date_range(pd.Timestamp(T, tz="UTC"), periods=4, freq="1s")
    grid = pd.DataFrame(
        {
            "time": times,
            "mid": [0.5, 0.51, 0.52, 0.53],
            "analysis_eligible": True,
            "epoch_id": "a",
            "session_id": "s",
            "connection_id": "c",
        }
    )
    events = pd.DataFrame(
        {
            "received_at": [times[0], times[0] + pd.Timedelta(milliseconds=500), times[1]],
            "valid": [True, False, True],
            "event_kind": ["book_updates"] * 3,
            "epoch_id": ["a"] * 3,
            "session_id": ["s"] * 3,
            "connection_id": ["c"] * 3,
        }
    )
    assert pd.isna(strict_forward_move(grid, 1, events).iloc[0])
    assert strict_forward_move(grid, 1, events).iloc[1] == pytest.approx(0.01)
    assert pd.isna(strict_forward_move(grid, 1, events).iloc[-1])


def test_past_second_ofi_does_not_mix_sockets_or_fill_absent_bins():
    times = pd.date_range(pd.Timestamp(T, tz="UTC"), periods=3, freq="1s")
    grid = pd.DataFrame(
        {
            "time": times,
            "instrument_id": IID,
            "session_id": "s",
            "connection_id": "c2",
            "analysis_eligible": True,
        }
    )
    events = pd.DataFrame(
        {
            "received_at": [
                times[0] + pd.Timedelta(milliseconds=200),
                times[0] + pd.Timedelta(milliseconds=700),
            ],
            "instrument_id": IID,
            "epoch_id": ["a", "b"],
            "session_id": "s",
            "connection_id": ["c1", "c2"],
            "valid": True,
            "event_kind": "book_updates",
            "ofi": [20, -3],
        }
    )
    assert observed_ofi_grid(events, grid).isna().all()


def test_btc_sampling_rejects_future_price_and_observed_short_disconnect():
    trade_rows = []
    for offset in [NS // 2, NS, 2 * NS]:
        at = pd.Timestamp(T + offset, tz="UTC")
        trade_rows.append(
            {
                "instrument_id": "binance:spot:BTCUSDT",
                "received_at": at,
                "price": Decimal(100 + offset // NS),
                "quantity": Decimal(1),
                "session_id": "s",
                "connection_id": "c",
            }
        )
    disconnect = event(
        NS + NS // 2, "operation", details=json.dumps({"type": "disconnected", "source": "binance"})
    )
    grid = btc_grid(
        fake_sample({"trade_prints": trade_rows, "operational_events": [disconnect]}), max_age_s=3
    )
    assert pd.isna(grid.loc[0, "price_float"])
    assert pd.isna(grid.loc[2, "log_return_bps"])
    assert grid.loc[1, "trade_at"] <= grid.loc[1, "time"]


def test_sample_rejects_manifest_or_part_tampering(tmp_path):
    part = tmp_path / "example.parquet"
    part.write_bytes(b"original bytes")
    quality = tmp_path / "quality.json"
    quality.write_text("{}")
    manifest = {
        "tutorial_sample_version": 1,
        "files": [{"path": part.name, "sha256": file_digest(part)}],
        "quality_sha256": file_digest(quality),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    pinned = file_digest(path)
    Sample(tmp_path, pinned)
    with pytest.raises(ValueError, match="manifest checksum"):
        Sample(tmp_path, "0" * 64)
    part.write_bytes(b"tampered bytes")
    with pytest.raises(ValueError, match="file checksum"):
        Sample(tmp_path, pinned)


def test_nullable_source_timestamps_do_not_round_through_float():
    sample = object.__new__(Sample)
    sample._records = {
        "trade_prints": [
            {"source_time": T + 1, "received_at": T + 2},
            {"source_time": None, "received_at": T + 3},
        ]
    }
    frame = sample.frame("trade_prints")
    assert frame.loc[0, "source_time"].value == T + 1
    assert pd.isna(frame.loc[1, "source_time"])


def test_fresh_snapshots_can_supply_prices_without_becoming_flow_or_bridging_a_socket():
    times = pd.date_range(pd.Timestamp(T, tz="UTC"), periods=3, freq="1s")
    grid = pd.DataFrame(
        {
            "time": times,
            "mid": [0.5, 0.51, 0.52],
            "analysis_eligible": True,
            "epoch_id": ["a", "b", "c"],
            "session_id": "s",
            "connection_id": "c",
        }
    )
    events = grid.rename(columns={"time": "received_at"}).assign(
        valid=True, event_kind="book_snapshots"
    )
    assert strict_forward_move(grid, 1, events).isna().all()
    moves = strict_forward_move(grid, 1, events, require_same_seed=False)
    assert moves.iloc[0] == pytest.approx(0.01)
    grid.loc[1, "connection_id"] = "new_socket"
    events.loc[1, "connection_id"] = "new_socket"
    assert strict_forward_move(grid, 1, events, require_same_seed=False).isna().all()


def test_ofi_bin_sums_observed_increments_and_never_counts_a_snapshot_as_addition():
    times = pd.date_range(pd.Timestamp(T, tz="UTC"), periods=2, freq="1s")
    grid = pd.DataFrame(
        {
            "time": times,
            "instrument_id": IID,
            "session_id": "s",
            "connection_id": "c",
            "analysis_eligible": True,
        }
    )
    events = pd.DataFrame(
        {
            "received_at": [times[0] + pd.Timedelta(milliseconds=x) for x in [200, 500, 700]],
            "instrument_id": IID,
            "session_id": "s",
            "connection_id": "c",
            "valid": True,
            "event_kind": ["book_updates", "book_snapshots", "book_updates"],
            "ofi": [20, float("nan"), -3],
        }
    )
    measured = observed_ofi_grid(events, grid)
    assert pd.isna(measured.iloc[0])
    assert measured.iloc[1] == 17
