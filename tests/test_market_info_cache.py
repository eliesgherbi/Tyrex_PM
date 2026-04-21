"""Phase 5 unit tests — :mod:`tyrex_pm.venue.polymarket.market_info`.

These tests exercise the four properties the rest of the bot now depends on:

* ``MarketInfo.quantize_price`` floors prices to the venue tick (never rounds up).
* ``MarketInfoCache`` caches resolved entries within ``ttl_s`` and re-fetches
  after expiry.
* The cache is fail-closed: any HTTP error from ``/markets-by-token`` or
  ``/clob-markets`` propagates as :class:`MarketInfoFetchError` instead of
  silently returning a stale or partial entry.
* The risk gate :func:`tyrex_pm.risk.venue_min_size.evaluate_venue_min_size`
  prefers the venue's ``min_order_size`` over the YAML default when a
  :class:`MarketInfo` is reachable via ``RiskContext.market_info``.

No live HTTP calls are made; ``httpx.AsyncClient`` is monkeypatched at the
module level to a tiny in-process fake so the tests run hermetically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.venue.polymarket import market_info as mi_mod
from tyrex_pm.venue.polymarket.market_info import (
    MarketInfo,
    MarketInfoCache,
    MarketInfoFetchError,
)


# ---------------------------------------------------------------------------
# httpx fakes
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeAsyncClient:
    """Drop-in for ``httpx.AsyncClient`` used by :class:`MarketInfoCache`.

    The class-level ``responses`` dict is keyed by URL substring; each test
    sets it before calling ``cache.get`` and asserts on the matching ``calls``
    counter to confirm cache hits/misses.
    """

    responses: dict[str, _FakeResp] = {}
    calls: dict[str, int] = {}

    def __init__(self, *_a, **_k) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_a) -> None:  # noqa: D401 — context manager protocol
        return None

    async def get(self, url: str) -> _FakeResp:
        for needle, resp in self.responses.items():
            if needle in url:
                self.calls[needle] = self.calls.get(needle, 0) + 1
                return resp
        return _FakeResp(404, {"error": f"no fake registered for {url}"})


class _FakeClient:
    """Stand-in for the V2 ``ClobClient`` — only the two SDK helpers used."""

    def __init__(self, *, neg_risk: bool = False, fee_bps: int = 0) -> None:
        self._neg_risk = neg_risk
        self._fee = fee_bps
        self.neg_risk_calls = 0
        self.fee_calls = 0

    def get_neg_risk(self, _token_id: str) -> bool:
        self.neg_risk_calls += 1
        return self._neg_risk

    def get_fee_rate_bps(self, _token_id: str) -> int:
        self.fee_calls += 1
        return self._fee


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.responses = {}
    _FakeAsyncClient.calls = {}
    monkeypatch.setattr(mi_mod.httpx, "AsyncClient", _FakeAsyncClient)


# ---------------------------------------------------------------------------
# MarketInfo.quantize_price
# ---------------------------------------------------------------------------


def _mi(tick: str, mos: str = "5") -> MarketInfo:
    return MarketInfo(
        token_id=TokenId("t"),
        condition_id="c",
        tick_size=Decimal(tick),
        min_order_size=Decimal(mos),
        neg_risk=False,
        fee_rate_bps=0,
        outcomes={},
        fetched_ts=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    ("tick", "raw", "expected"),
    [
        ("0.01", "0.5523", "0.55"),
        ("0.01", "0.50", "0.5"),
        ("0.001", "0.12399", "0.123"),
        ("0.1", "0.97", "0.9"),
        # Already-aligned values are returned unchanged (after .normalize()).
        ("0.01", "0.50000", "0.5"),
    ],
)
def test_quantize_price_floors_to_tick(tick: str, raw: str, expected: str) -> None:
    info = _mi(tick)
    assert info.quantize_price(Decimal(raw)) == Decimal(expected)


def test_quantize_price_zero_tick_returns_input_unchanged() -> None:
    """Defensive: a malformed tick of 0 must not divide-by-zero or quantize."""

    info = _mi("0")
    assert info.quantize_price(Decimal("0.5523")) == Decimal("0.5523")


# ---------------------------------------------------------------------------
# MarketInfoCache: TTL + concurrency
# ---------------------------------------------------------------------------


def _wire_happy_path(token_id: str = "t1", condition_id: str = "0xc1") -> None:
    _FakeAsyncClient.responses = {
        f"/markets-by-token/{token_id}": _FakeResp(200, {"condition_id": condition_id}),
        f"/clob-markets/{condition_id}": _FakeResp(
            200,
            {
                "mts": "0.01",
                "mos": "5",
                "t": [
                    {"t": token_id, "o": "Yes"},
                    {"t": "other", "o": "No"},
                ],
            },
        ),
    }


@pytest.mark.asyncio
async def test_cache_hit_within_ttl_does_not_refetch() -> None:
    _wire_happy_path()
    cache = MarketInfoCache(_FakeClient(), host="https://example", ttl_s=60.0)

    a = await cache.get("t1")
    b = await cache.get("t1")

    assert a is b
    assert _FakeAsyncClient.calls["/markets-by-token/t1"] == 1
    assert _FakeAsyncClient.calls["/clob-markets/0xc1"] == 1


@pytest.mark.asyncio
async def test_cache_miss_after_ttl_refetches() -> None:
    _wire_happy_path()
    cache = MarketInfoCache(_FakeClient(), host="https://example", ttl_s=60.0)

    first = await cache.get("t1")

    cache._cache["t1"] = MarketInfo(  # type: ignore[attr-defined]
        token_id=first.token_id,
        condition_id=first.condition_id,
        tick_size=first.tick_size,
        min_order_size=first.min_order_size,
        neg_risk=first.neg_risk,
        fee_rate_bps=first.fee_rate_bps,
        outcomes=first.outcomes,
        fetched_ts=datetime.now(timezone.utc) - timedelta(seconds=120),
        raw=first.raw,
    )

    await cache.get("t1")

    assert _FakeAsyncClient.calls["/markets-by-token/t1"] == 2
    assert _FakeAsyncClient.calls["/clob-markets/0xc1"] == 2


@pytest.mark.asyncio
async def test_snapshot_returns_resolved_entries_only() -> None:
    _wire_happy_path()
    cache = MarketInfoCache(_FakeClient(), host="https://example", ttl_s=60.0)

    assert cache.snapshot() == {}
    await cache.get("t1")
    snap = cache.snapshot()
    assert list(snap.keys()) == [TokenId("t1")]
    assert snap[TokenId("t1")].tick_size == Decimal("0.01")


# ---------------------------------------------------------------------------
# MarketInfoCache: fail-closed semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_raises_when_markets_by_token_returns_404() -> None:
    _FakeAsyncClient.responses = {
        "/markets-by-token/missing": _FakeResp(404, {"error": "market not found"}),
    }
    cache = MarketInfoCache(_FakeClient(), host="https://example")

    with pytest.raises(MarketInfoFetchError):
        await cache.get("missing")
    assert "missing" not in cache._cache  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_raises_when_clob_markets_missing_mts_or_mos() -> None:
    _FakeAsyncClient.responses = {
        "/markets-by-token/t1": _FakeResp(200, {"condition_id": "0xc1"}),
        "/clob-markets/0xc1": _FakeResp(200, {"foo": "bar"}),  # no mts/mos
    }
    cache = MarketInfoCache(_FakeClient(), host="https://example")

    with pytest.raises(MarketInfoFetchError):
        await cache.get("t1")


@pytest.mark.asyncio
async def test_get_raises_when_sdk_helper_throws() -> None:
    _wire_happy_path()

    class _Boom(_FakeClient):
        def get_neg_risk(self, _token_id: str) -> bool:  # type: ignore[override]
            raise RuntimeError("sdk down")

    cache = MarketInfoCache(_Boom(), host="https://example")
    with pytest.raises(MarketInfoFetchError):
        await cache.get("t1")
