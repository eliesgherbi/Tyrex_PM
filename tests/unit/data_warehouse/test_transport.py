from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from tyrex_pm.data_warehouse.config import Operations, Profile, Storage
from tyrex_pm.data_warehouse.connectors.http import PublicHttp
from tyrex_pm.data_warehouse.contracts import RawEnvelope
from tyrex_pm.data_warehouse.normalize.worker import normalize_once
from tyrex_pm.data_warehouse.query.reader import Reader
from tyrex_pm.data_warehouse.supervisor import Collector


@pytest.mark.asyncio
async def test_reference_socket_auth_is_not_archived_and_sequence_gap_is_visible(
    tmp_path, monkeypatch
):
    import asyncio

    from tyrex_pm.data_warehouse.capture.writer import raw_manifests, segment_records
    from tyrex_pm.data_warehouse.config import Credentials
    from tyrex_pm.data_warehouse.connectors.streams import record_stream
    from tyrex_pm.data_warehouse.contracts import canonical

    profile = replace(Profile(), storage=replace(Storage(), min_free_disk_gib=0,
                                                raw_fsync_interval_ms=1))
    creds = Credentials("test-key", "test-secret", "test-passphrase")
    collector = Collector(tmp_path, profile, creds, normalize_live=False)
    frames = [canonical({"op": "authed"})] + [canonical({
        "channel": "price.crypto.twap", "seq": n, "payload": {
            "symbol": "btcusd", "source": "chainlink", "timestamp": 1000 + n,
            "full_accuracy_value": "60000.123456789"}}) for n in (1, 3)]
    sent = []

    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def send(self, value):
            sent.append(value)
        async def recv(self):
            if frames:
                return frames.pop(0)
            await asyncio.Future()

    monkeypatch.setattr("websockets.connect", lambda *args, **kwargs: Socket())
    task = asyncio.create_task(record_stream(collector, "polybolt", "reference", credentials=creds))
    for _ in range(100):
        if collector.feed_health.get("reference", {}).get("last_observation_at") == 1003000000:
            break
        await asyncio.sleep(.01)
    collector.stop.set()
    await asyncio.wait_for(task, 5)
    assert collector.projection_bytes == 0
    collector.writer.close()
    collector.catalog.close()
    await collector.http.close()
    archived = [r for m in raw_manifests(tmp_path) for r in segment_records(tmp_path, m)]
    assert len([r for r in archived if r.source_id == "polybolt"]) == 2
    assert all(b"test-secret" not in r.bytes() for r in archived)
    assert any(b"sequence_gap" in r.bytes() for r in archived)
    assert "test-secret" in sent[0]  # Only the outbound auth frame contains credentials.


@pytest.mark.asyncio
async def test_http_allowlist_preserves_body_and_never_mutates():
    seen = []
    captured = []

    async def capture(body, **kwargs):
        raw = RawEnvelope.create(
            body=body, collection_id="c", session_id="s", sequence=len(captured) + 1, **kwargs
        )
        captured.append(raw)
        return raw

    def serve(request):
        seen.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b' [ {"asset_id":"1","bids":[],"asks":[]} ] ')

    http = PublicHttp(capture, Operations(), transport=httpx.MockTransport(serve))
    document, raw = await http.read("clob_books", body=[{"token_id": "1"}])
    assert document[0]["asset_id"] == "1"
    assert raw.bytes().startswith(b" [ ")
    assert raw.method == "POST" and seen[0].url.path == "/books"
    with pytest.raises(ValueError, match="allowlist"):
        await http.read("submit_order")
    with pytest.raises(ValueError, match="exceeds 20"):
        await http.read("resolutions", params={"condition": ",".join(map(str, range(21)))})
    assert len(seen) == 1
    await http.close()


@pytest.mark.asyncio
async def test_supervisor_shutdown_and_pending_projection_without_network(tmp_path, monkeypatch):
    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    collector = Collector(tmp_path, profile, None, normalize_live=False)

    async def idle(*args, **kwargs):
        await collector.stop.wait()

    async def no_op():
        return None

    monkeypatch.setattr("tyrex_pm.data_warehouse.supervisor.record_stream", idle)
    for owner, attr in (
        (collector.discovery, "cycle"),
        (collector.discovery, "scan"),
        (collector, "metadata_cycle"),
        (collector, "book_audit"),
        (collector, "resolution_cycle"),
        (collector, "clocks"),
    ):
        monkeypatch.setattr(owner, attr, no_op)
    await collector.persist("tasks", "unresolved", {"status": "PENDING"})
    result = await collector.run(0.1)
    assert result["stopped"]
    assert result["fsynced"] == result["received"]
    normalize_once(tmp_path)
    events = list(Reader(tmp_path).iter_rows("operational_events"))
    assert any(row["event_type"] == "session_stopped" for row in events)
    resumed = Collector(
        tmp_path, profile, None, collection=collector.collection, normalize_live=False
    )
    assert (
        resumed.catalog.values(collector.collection, "tasks")["unresolved"]["status"] == "PENDING"
    )
    resumed.writer.close()
    resumed.catalog.close()
    await resumed.http.close()
