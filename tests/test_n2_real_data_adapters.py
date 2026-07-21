"""N2 read-only adapters — offline deterministic tests."""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from tyrex_pm.adapters.binance.normalize import normalize_trade_message
from tyrex_pm.adapters.clock_sync import FakeClockSyncProvider, OsMonitorClockSyncProvider
from tyrex_pm.adapters.polymarket.discovery import (
    bind_btc_5m_gamma_event,
    market_from_gamma_event,
)
from tyrex_pm.adapters.polymarket.rtds_adapter import (
    RtdsBinanceComparisonAdapter,
    RtdsChainlinkAdapter,
)
from tyrex_pm.adapters.polymarket.rtds_normalize import (
    binance_subscribe_message,
    chainlink_subscribe_message,
    normalize_chainlink_tick,
    normalize_rtds_binance_tick,
)
from tyrex_pm.adapters.polymarket.ws_adapter import PolymarketMarketWsAdapter
from tyrex_pm.core.clock import FakeClock, SystemClock
from tyrex_pm.core.events import EventSource, ReferencePriceUpdated, SettlementReferenceUpdated
from tyrex_pm.core.ids import MarketId, new_correlation_id
from tyrex_pm.core.ingress import AppendOnlyIngressLog, FeedRole, IngressMeta, IngressSequencer
from tyrex_pm.core.time_authority import (
    ClockSourceObservation,
    ClockSyncSnapshot,
    SnapshotTimeAuthority,
    TimeSyncStatus,
)
from tyrex_pm.domain.polymarket.discovery_binding import DiscoverySessionRole
from tyrex_pm.domain.polymarket.outcome_map import (
    NormalizedLeg,
    OutcomeMapError,
    map_up_down_outcomes,
)
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.market_data.reference_store import ReferenceDataStore
from tyrex_pm.market_data.settlement_store import SettlementReferenceStore
from tyrex_pm.runtime.feed_supervisor import FeedHandle, FeedSupervisor

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "n2"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- normalize ---


def test_rtds_chainlink_normalize_golden() -> None:
    msg = _load("rtds_chainlink_tick.json")
    wall = datetime(2026, 7, 20, 21, 15, 5, tzinfo=timezone.utc)
    evt = normalize_chainlink_tick(
        msg,
        ts_received=wall,
        receive_monotonic_ns=42,
        ingress_sequence=1,
        connection_generation=1,
        clock_uncertainty_ms=10,
    )
    assert evt is not None
    assert isinstance(evt, SettlementReferenceUpdated)
    assert evt.source is EventSource.POLYMARKET_RTDS_CHAINLINK
    assert evt.settlement.symbol == "btc/usd"
    assert evt.settlement.price == Decimal("65276.78644629988")
    assert evt.ts_event == datetime(2026, 7, 20, 21, 15, 0, tzinfo=timezone.utc)
    assert evt.ingress is not None
    assert evt.ingress.role is FeedRole.SETTLEMENT_REFERENCE
    assert evt.ingress.raw_fingerprint


def test_rtds_binance_normalize_comparison_role() -> None:
    msg = _load("rtds_binance_tick.json")
    wall = datetime(2026, 7, 20, 21, 15, 0, tzinfo=timezone.utc)
    evt = normalize_rtds_binance_tick(
        msg,
        ts_received=wall,
        receive_monotonic_ns=1,
        ingress_sequence=2,
        connection_generation=1,
        clock_uncertainty_ms=None,
        subscription_mode="unfiltered",
    )
    assert evt is not None
    assert evt.source is EventSource.POLYMARKET_RTDS_BINANCE
    assert evt.reference.venue == "polymarket_rtds_binance"
    assert evt.ingress is not None
    assert evt.ingress.role is FeedRole.COMPARISON_REFERENCE
    assert evt.ingress.subscription_mode == "unfiltered"


def test_rtds_binance_subscribe_modes() -> None:
    filtered = binance_subscribe_message(mode="filtered")
    assert filtered["subscriptions"][0]["filters"] == "btcusdt"
    unfiltered = binance_subscribe_message(mode="unfiltered")
    assert "filters" not in unfiltered["subscriptions"][0]
    cl = chainlink_subscribe_message()
    assert cl["subscriptions"][0]["topic"] == "crypto_prices_chainlink"


def test_binance_normalize_ingress_and_trade_id() -> None:
    payload = {
        "e": "trade",
        "s": "BTCUSDT",
        "t": 6521212440,
        "p": "65326.01",
        "T": 1784582100284,
    }
    wall = datetime(2026, 7, 20, 21, 15, 0, tzinfo=timezone.utc)
    evt = normalize_trade_message(
        payload,
        ts_received=wall,
        receive_monotonic_ns=99,
        ingress_sequence=3,
        connection_generation=2,
        clock_uncertainty_ms=5,
    )
    assert evt.source is EventSource.BINANCE
    assert evt.ingress is not None
    assert evt.ingress.provider_sequence_id == "6521212440"
    assert evt.ingress.role is FeedRole.TRADING_REFERENCE


# --- discovery / UpDown ---


def test_up_down_label_mapping_and_reversed() -> None:
    om = map_up_down_outcomes(["Down", "Up"], ["downTok", "upTok"])
    assert om.token_for(NormalizedLeg.UP).value == "upTok"
    assert om.token_for(NormalizedLeg.DOWN).value == "downTok"


@pytest.mark.parametrize(
    "outcomes,tokens,code",
    [
        (["Down"], ["1"], "missing_outcome"),
        (["Up", "Up"], ["1", "2"], "duplicate_outcome"),
        (["Up", "Maybe"], ["1", "2"], "unknown_label"),
        (["Down", "Sideways"], ["1", "2"], "unknown_label"),
        (["Up", "Down"], ["1"], "token_count_mismatch"),
        (["Up", "Down"], ["1", "1"], "duplicate_token_ids"),
    ],
)
def test_up_down_rejection_cases(outcomes, tokens, code) -> None:
    with pytest.raises(OutcomeMapError) as ei:
        map_up_down_outcomes(outcomes, tokens)
    assert ei.value.code == code


def test_bind_btc_5m_gamma_happy_path() -> None:
    event = _load("gamma_btc_5m_event.json")
    binding = bind_btc_5m_gamma_event(
        event,
        expected_slug="btc-updown-5m-1784582100",
        session_role=DiscoverySessionRole.ACTIVE,
    )
    assert binding.up_token_id.startswith("43399")
    assert binding.down_token_id.startswith("14649")
    assert binding.market.event_start == datetime(2026, 7, 20, 21, 15, tzinfo=timezone.utc)
    assert binding.resolution_source == "https://data.chain.link/streams/btc-usd"
    assert binding.market_rule_ok
    assert binding.outcomes.mapping_method == "label_index_only"


def test_bind_rejects_wrong_window_and_source() -> None:
    event = _load("gamma_btc_5m_event.json")
    with pytest.raises(ValueError, match="wrong_window"):
        bind_btc_5m_gamma_event(event, expected_slug="btc-updown-5m-1784580000")
    bad = json.loads(json.dumps(event))
    bad["markets"][0]["resolutionSource"] = "https://example.com"
    bad["markets"][0]["description"] = "no chainlink here"
    with pytest.raises(ValueError, match="market_rule_source_mismatch"):
        bind_btc_5m_gamma_event(bad, expected_slug="btc-updown-5m-1784582100")


def test_positional_mapping_forbidden_without_labels() -> None:
    event = _load("gamma_btc_5m_event.json")
    event["markets"][0]["outcomes"] = '["A", "B"]'
    with pytest.raises(ValueError):
        market_from_gamma_event(event)


# --- stores / late OOO ---


def test_settlement_store_records_then_rejects_stale() -> None:
    store = SettlementReferenceStore()
    disp = EventDispatcher()
    store.attach(disp)
    msg = _load("rtds_chainlink_tick.json")
    wall = datetime(2026, 7, 20, 21, 15, 5, tzinfo=timezone.utc)
    first = normalize_chainlink_tick(
        msg,
        ts_received=wall,
        receive_monotonic_ns=1,
        ingress_sequence=1,
        connection_generation=1,
        clock_uncertainty_ms=None,
    )
    assert first is not None
    disp.publish(first)
    assert store.get().initialized
    # older tick
    older = dict(msg)
    older["payload"] = dict(msg["payload"])
    older["payload"]["timestamp"] = 1784582099000
    older["payload"]["value"] = 1.0
    second = normalize_chainlink_tick(
        older,
        ts_received=wall + timedelta(seconds=1),
        receive_monotonic_ns=2,
        ingress_sequence=2,
        connection_generation=1,
        clock_uncertainty_ms=None,
    )
    assert second is not None
    disp.publish(second)
    assert len(store.ingress) == 2
    assert store.ingress.rows[-1].accepted_by_current_view is False
    assert store.get().snapshot is not None
    assert store.get().snapshot.price == Decimal("65276.78644629988")


def test_reference_store_keeps_series_separate() -> None:
    store = ReferenceDataStore()
    disp = EventDispatcher()
    store.attach(disp)
    wall = datetime(2026, 7, 20, 21, 15, tzinfo=timezone.utc)
    spot = normalize_trade_message(
        {"s": "BTCUSDT", "p": "65326.01", "T": 1784582100284, "t": 1},
        ts_received=wall,
        ingress_sequence=1,
        connection_generation=1,
        receive_monotonic_ns=1,
    )
    rtds = normalize_rtds_binance_tick(
        _load("rtds_binance_tick.json"),
        ts_received=wall,
        receive_monotonic_ns=2,
        ingress_sequence=2,
        connection_generation=1,
        clock_uncertainty_ms=None,
        subscription_mode="unfiltered",
    )
    assert rtds is not None
    disp.publish(spot)
    disp.publish(rtds)
    assert store.get("BTCUSDT").snapshot is not None
    assert store.get("BTCUSDT").venue == "binance"
    assert store.get_series(venue="polymarket_rtds_binance", symbol="BTCUSDT").initialized


def test_ooo_marker_on_chainlink() -> None:
    msg = _load("rtds_chainlink_tick.json")
    wall = datetime(2026, 7, 20, 21, 15, 5, tzinfo=timezone.utc)
    evt = normalize_chainlink_tick(
        msg,
        ts_received=wall,
        receive_monotonic_ns=1,
        ingress_sequence=1,
        connection_generation=1,
        clock_uncertainty_ms=None,
        last_source_ts_ms=1784582101000,
    )
    assert evt is not None
    assert evt.ingress is not None
    assert evt.ingress.late_or_out_of_order == "out_of_order"


# --- fake WS lifecycle ---


class _FakeWs:
    def __init__(self, messages: list[Any], *, fail_recv_after: int | None = None) -> None:
        self._messages = list(messages)
        self._i = 0
        self.sent: list[str] = []
        self.closed = False
        self._fail_recv_after = fail_recv_after

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if self._fail_recv_after is not None and self._i >= self._fail_recv_after:
            raise asyncio.TimeoutError()
        if self._i >= len(self._messages):
            await asyncio.sleep(0.05)
            raise asyncio.TimeoutError()
        item = self._messages[self._i]
        self._i += 1
        if isinstance(item, dict):
            return json.dumps(item)
        return str(item)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._messages):
            raise StopAsyncIteration
        item = self._messages[self._i]
        self._i += 1
        if isinstance(item, dict):
            return json.dumps(item)
        return str(item)


class _FakeConnect:
    def __init__(self, sessions: list[_FakeWs]) -> None:
        self._sessions = sessions
        self._idx = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        cm = MagicMock()

        async def _aenter(*_a: Any, **_k: Any) -> _FakeWs:
            ws = self._sessions[min(self._idx, len(self._sessions) - 1)]
            self._idx += 1
            return ws

        async def _aexit(*_a: Any, **_k: Any) -> None:
            return None

        cm.__aenter__ = _aenter
        cm.__aexit__ = _aexit
        return cm


@pytest.mark.asyncio
async def test_rtds_chainlink_connect_subscribe_publish_shutdown() -> None:
    msg = _load("rtds_chainlink_tick.json")
    ws = _FakeWs([msg, "PONG"])
    connect = _FakeConnect([ws])
    health: list[str] = []
    adapter = RtdsChainlinkAdapter(
        connect=connect,
        heartbeat_timeout_s=0.2,
        on_health=lambda s, d: health.append(s),
    )
    disp = EventDispatcher()
    seen: list[SettlementReferenceUpdated] = []
    disp.subscribe(SettlementReferenceUpdated, seen.append)
    task = asyncio.create_task(adapter.run(disp))
    await asyncio.sleep(0.15)
    await adapter.stop()
    await asyncio.wait_for(task, timeout=2)
    assert any(s == "subscribed" for s in health)
    assert seen
    assert adapter.connection_generation >= 1
    assert any("PING" in x for x in ws.sent) or ws.sent  # subscribe at least
    assert json.loads(ws.sent[0])["action"] == "subscribe"


@pytest.mark.asyncio
async def test_rtds_binance_auto_falls_back_unfiltered() -> None:
    # First session: filtered, only non-btc noise / idle → failover
    # Second: unfiltered with btcusdt
    noise = {"topic": "crypto_prices", "type": "update", "payload": {"symbol": "ethusdt", "timestamp": 1, "value": 1}}
    btc = _load("rtds_binance_tick.json")
    ws1 = _FakeWs([noise], fail_recv_after=1)
    ws2 = _FakeWs([btc])
    connect = _FakeConnect([ws1, ws2])
    modes: list[str] = []
    adapter = RtdsBinanceComparisonAdapter(
        connect=connect,
        mode="auto",
        filtered_idle_s=0.05,
        heartbeat_timeout_s=0.2,
        on_health=lambda s, d: modes.append(f"{s}:{d.get('subscription_mode')or d.get('mode')}"),
    )
    disp = EventDispatcher()
    seen: list[ReferencePriceUpdated] = []
    disp.subscribe(ReferencePriceUpdated, seen.append)
    task = asyncio.create_task(adapter.run(disp))
    await asyncio.sleep(0.4)
    await adapter.stop()
    await asyncio.wait_for(task, timeout=2)
    assert seen
    assert seen[0].source is EventSource.POLYMARKET_RTDS_BINANCE
    assert adapter.active_mode == "unfiltered"


@pytest.mark.asyncio
async def test_rtds_heartbeat_timeout_increments_generation() -> None:
    ws1 = _FakeWs([], fail_recv_after=0)
    ws2 = _FakeWs([_load("rtds_chainlink_tick.json")])
    connect = _FakeConnect([ws1, ws2])
    adapter = RtdsChainlinkAdapter(
        connect=connect,
        heartbeat_timeout_s=0.05,
        reconnect_backoff_s=0.01,
    )
    disp = EventDispatcher()
    task = asyncio.create_task(adapter.run(disp))
    await asyncio.sleep(0.35)
    await adapter.stop()
    await asyncio.wait_for(task, timeout=2)
    assert adapter.connection_generation >= 2


@pytest.mark.asyncio
async def test_feed_supervisor_graceful_shutdown() -> None:
    msg = _load("rtds_chainlink_tick.json")
    ws = _FakeWs([msg])
    adapter = RtdsChainlinkAdapter(connect=_FakeConnect([ws]), heartbeat_timeout_s=0.2)
    supervisor = FeedSupervisor([FeedHandle("rtds_chainlink", adapter, required_for_ready=True)])
    disp = EventDispatcher()
    task = asyncio.create_task(supervisor.run(disp))
    await asyncio.sleep(0.1)
    await supervisor.stop()
    await asyncio.wait_for(task, timeout=2)
    assert supervisor.readiness().feeds["rtds_chainlink"] is not None


def test_clob_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    binding_event = _load("gamma_btc_5m_event.json")
    binding = bind_btc_5m_gamma_event(
        binding_event, expected_slug="btc-updown-5m-1784582100"
    )
    adapter = PolymarketMarketWsAdapter.from_binding(binding)
    assert set(adapter._allowed) == set(binding.clob_asset_ids)
    # simulate handle
    bad = {"event_type": "book", "asset_id": "999", "bids": [], "asks": [], "timestamp": 1}
    disp = EventDispatcher()

    async def _run() -> None:
        await adapter._handle_raw(json.dumps(bad), disp, generation=1)

    asyncio.run(_run())
    assert adapter.rejected_wrong_token == 1


def test_prepared_next_does_not_publish() -> None:
    binding_event = _load("gamma_btc_5m_event.json")
    binding = bind_btc_5m_gamma_event(
        binding_event,
        expected_slug="btc-updown-5m-1784582100",
        session_role=DiscoverySessionRole.PREPARED_NEXT,
    )
    adapter = PolymarketMarketWsAdapter.from_binding(binding)
    assert adapter._publish_events is False


# --- clock ---


@pytest.mark.asyncio
async def test_clock_sync_snapshot_ready_degraded_unsync() -> None:
    clock = FakeClock(_wall=datetime(2026, 7, 20, 21, 0, tzinfo=timezone.utc))
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=100, max_snapshot_age_ms=1000)
    view = auth.view()
    assert view.sync_status is TimeSyncStatus.UNSYNCHRONIZED
    assert view.ready is False

    ready_provider = FakeClockSyncProvider(offset_ms=2.0, uncertainty_ms=5)
    snap = await ready_provider.measure()
    auth.apply_snapshot(snap)
    assert auth.view().ready is True
    assert auth.view().sync_status is TimeSyncStatus.READY

    deg = FakeClockSyncProvider(
        offset_ms=10.0,
        uncertainty_ms=5,
        sync_status=TimeSyncStatus.DEGRADED,
        disagreement_ms=600.0,
        sources=(
            ClockSourceObservation(source="os_clock", offset_ms=0.0, ok=True),
            ClockSourceObservation(source="binance_api_time", offset_ms=600.0, ok=True),
        ),
    )
    auth.apply_snapshot(await deg.measure())
    assert auth.view().ready is False
    assert auth.view().sync_status is TimeSyncStatus.DEGRADED


@pytest.mark.asyncio
async def test_os_monitor_disagreement_degraded() -> None:
    def fetcher() -> tuple[float, float]:
        # remote far ahead → large offset / disagreement
        local = datetime.now(timezone.utc).timestamp() * 1000.0
        return local + 2000.0, 10.0

    provider = OsMonitorClockSyncProvider(
        enable_binance_cross_check=True,
        disagreement_degraded_ms=100.0,
        time_fetcher=fetcher,
    )
    snap = await provider.measure()
    assert snap.primary_source == "os_clock"
    assert snap.max_source_disagreement_ms is not None
    assert snap.sync_status is TimeSyncStatus.DEGRADED


def test_core_time_authority_has_no_network_imports() -> None:
    path = REPO / "src" / "tyrex_pm" / "core" / "time_authority.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    forbidden = {"socket", "urllib", "http", "websockets", "requests", "aiohttp", "ntplib"}
    assert not (mods & forbidden)


def test_adapters_do_not_import_strategies() -> None:
    adapters = REPO / "src" / "tyrex_pm" / "adapters"
    for path in adapters.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "strategies" not in node.module.split("."), path
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("tyrex_pm.strategies"), path


def test_strategies_still_cannot_import_adapters() -> None:
    strategies = REPO / "src" / "tyrex_pm" / "strategies"
    for path in strategies.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "adapters" in node.module:
                pytest.fail(f"{path} imports adapters")


def test_connection_generation_and_sequencer() -> None:
    seq = IngressSequencer()
    assert seq.next() == 1
    assert seq.next() == 2
    from tyrex_pm.core.ingress import ConnectionGeneration

    g = ConnectionGeneration()
    assert g.bump() == 1
    assert g.bump() == 2
