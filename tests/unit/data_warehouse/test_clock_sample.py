"""Clock samples: a whole-second server time widens the uncertainty and centres the offset."""

from __future__ import annotations

from tyrex_pm.data_warehouse.supervisor import clock_sample

S = 1_000_000_000
MS = 1_000_000


def test_binance_millisecond_sample_is_unchanged() -> None:
    sample = clock_sample("binance_time", 1_000_123, request_at=999_900 * MS,
                          response_at=1_000_100 * MS, sample_id="b")
    assert sample["server_at"] == 1_000_123 * MS
    assert sample["estimated_offset_ns"] == 123 * MS  # server - request/response midpoint
    assert sample["uncertainty_ns"] == 100 * MS  # half the round trip
    assert sample["measurement_source"] == "binance_time" and sample["status"] == "OBSERVED"


def test_clob_whole_second_sample_is_centred_and_honestly_uncertain() -> None:
    # Local midpoint 1000.665 s; the CLOB reports 1000 s, i.e. a true time in [1000, 1001).
    sample = clock_sample("clob_time", 1000, request_at=1000 * S + 640 * MS,
                          response_at=1000 * S + 690 * MS, sample_id="c")
    assert sample["server_at"] == 1000 * S
    assert sample["estimated_offset_ns"] == -165 * MS  # centred: 1000.5 - 1000.665
    assert sample["uncertainty_ns"] == 25 * MS + S // 2  # round trip / 2 + resolution / 2


def test_negative_round_trip_is_a_wall_clock_discontinuity() -> None:
    sample = clock_sample("binance_time", 5, request_at=10 * MS, response_at=9 * MS,
                          sample_id="x")
    assert sample["status"] == "WALL_CLOCK_DISCONTINUITY" and sample["uncertainty_ns"] == 0
