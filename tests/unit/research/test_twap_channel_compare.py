import asyncio
import json
from decimal import Decimal

import pytest

from scripts.compare_twap_channels import NS, capture_channels, summarize


def tick(channel, second, value="100", *, history=False, arrival_ms=1000):
    return {
        "channel": channel, "source_ns": second * NS,
        "received_ns": second * NS + arrival_ms * 1_000_000,
        "received_mono_ns": second * NS + arrival_ms * 1_000_000,
        "value": value, "delivery_kind": "subscription_history" if history else "live",
        "connection": 1,
    }


def record(tmp_path, rows):
    (tmp_path / "ticks.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_exact_values_and_clock_independent_relative_arrival(tmp_path):
    rows = []
    for second in range(100, 112):
        rows += [tick("polybolt", second, "100.123456789012345678"),
                 tick("rtds", second, "100.123456789012345678", arrival_ms=1250)]
    # Host wall-time skew cannot change the paired monotonic arrival difference.
    for row in rows:
        row["received_ns"] += 50 * NS
    record(tmp_path, rows)
    result = summarize(tmp_path)
    assert result["verdict"] == "MATCHED_ON_OBSERVED_INTERVAL"
    assert result["relative_arrival_rtds_minus_polybolt"]["median_ms"] == 250
    assert result["channels"]["rtds"]["source_to_receipt_host_clock"]["median_ms"] == 51250


def test_history_cannot_hide_missing_live_tick_or_improve_latency(tmp_path):
    rows = []
    for second in range(100, 112):
        rows.append(tick("polybolt", second))
        rows.append(tick("rtds", second, history=second == 105, arrival_ms=4000))
    record(tmp_path, rows)
    result = summarize(tmp_path)
    assert result["matched_live_timestamps"] == 11
    assert result["channels"]["rtds"]["history_rows"] == 1
    assert result["missing_at_assumed_1s_cadence"]["rtds"]["count"] == 1
    assert result["verdict"] == "DIFFERENCES_OBSERVED"


def test_no_nearest_matching_startup_tails_excluded_and_conflicts_visible(tmp_path):
    rows = [tick("polybolt", 99)]
    for second in range(100, 112):
        rows += [tick("polybolt", second), tick("rtds", second)]
    rows += [tick("rtds", 112), tick("rtds", 104, "101", arrival_ms=5000)]
    record(tmp_path, rows)
    result = summarize(tmp_path)
    assert result["matched_live_timestamps"] == 12
    assert result["exact_value_mismatches"] == 0  # First arrivals agree; duplicate conflicts.
    assert result["channels"]["rtds"]["conflicting_live_timestamps"] == 1
    assert all(v["count"] == 0 for v in result["missing_at_assumed_1s_cadence"].values())
    assert result["verdict"] == "DIFFERENCES_OBSERVED"


def test_precision_difference_not_rounded_away_and_missing_both_detected(tmp_path):
    rows = []
    for second in range(100, 113):
        if second == 105:
            continue
        rows += [tick("polybolt", second, "100.000000000001"),
                 tick("rtds", second, "100.000000000002")]
    record(tmp_path, rows)
    result = summarize(tmp_path)
    assert result["exact_value_mismatches"] == 12
    assert Decimal(result["max_absolute_value_difference"]) == Decimal("0.000000000001")
    assert all(v["count"] == 1 for v in result["missing_at_assumed_1s_cadence"].values())


def test_one_channel_never_connected_is_inconclusive(tmp_path):
    record(tmp_path, [tick("rtds", second) for second in range(100, 112)])
    result = summarize(tmp_path)
    assert result["verdict"] == "INSUFFICIENT_DATA"
    assert result["common_live_source_interval"] is None


def test_shared_gap_is_distinguished_from_channel_disagreement(tmp_path):
    rows = []
    for second in range(100, 113):
        if second != 105:
            rows += [tick("polybolt", second), tick("rtds", second)]
    record(tmp_path, rows)
    result = summarize(tmp_path)
    assert result["verdict"] == "MATCHED_VALUES_WITH_SHARED_GAPS"
    assert len(result["shared_cadence_gaps_utc"]) == 1
    assert all(not times for times in
               result["live_timestamps_seen_only_on_one_channel"].values())


@pytest.mark.asyncio
async def test_actual_adapters_and_normalizers_with_fake_sockets(tmp_path, monkeypatch):
    import websockets

    from tyrex_pm.data_warehouse.config import Credentials

    sent = []
    base_ms = 1792266900000
    poly = [json.dumps({"op": "authed"})]
    rtds = []
    for i in range(12):
        point = {"source": "chainlink", "symbol": "btcusd", "timestamp": base_ms+i*1000,
                 "full_accuracy_value": "81471.005883689865314304", "value": 81471.00588368987}
        poly.append(json.dumps({"channel": "price.crypto.twap", "seq": i+1, "payload": point}))
        rtds.append(json.dumps({"topic": "crypto_prices_twap_sixty", "type": "update",
                               "payload": {"symbol": "btc/usd", "timestamp": base_ms+i*1000,
                                           "value": 81471.00588368987,
                                           "full_accuracy_value": "81471005883689865314304"}}))

    class Socket:
        def __init__(self, messages):
            self.messages = list(messages)
            self.closed = asyncio.Event()

        async def send(self, body):
            sent.append(body)

        async def recv(self):
            await asyncio.sleep(0)
            if self.messages:
                return self.messages.pop(0)
            await self.closed.wait()
            raise OSError("closed")

        async def close(self):
            self.closed.set()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.close()

    monkeypatch.setattr(websockets, "connect", lambda url, **kw:
                        Socket(poly if "ws-live-v2" in url else rtds))
    monkeypatch.setattr("scripts.compare_twap_channels.load_credentials",
                        lambda *a, **kw: Credentials("test-key", "test-secret", "test-pass"))
    result = await capture_channels(tmp_path, .15, tmp_path / "unused.env")
    assert result["matched_live_timestamps"] == 12
    assert result["exact_value_mismatches"] == 0
    assert result["max_absolute_value_difference"] == "0E-18"
    for path in tmp_path.iterdir():
        assert "test-secret" not in path.read_text(encoding="utf-8")
        assert "test-pass" not in path.read_text(encoding="utf-8")
    assert any("test-secret" in body for body in sent)
