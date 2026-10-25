"""The CLOB socket carries only markets that open soon, are open, or are in their close tail."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tyrex_pm.data_warehouse.config import Coverage, Profile
from tyrex_pm.data_warehouse.supervisor import Collector

S = 10**9


def _host(markets: list[dict], ahead_s: int = 900) -> SimpleNamespace:
    profile = Profile(coverage=Coverage(book_subscribe_ahead_s=ahead_s))
    host = SimpleNamespace(
        profile=profile, markets={m["market_id"]: m for m in markets}, shard_of={}
    )
    host.active_markets = lambda: Collector.active_markets(host)
    host.book_markets = lambda: Collector.book_markets(host)
    return host


def _market(name: str, starts_in_s: float, closed_ago_s: float | None = None) -> dict:
    now = time.time_ns()
    return {
        "market_id": name,
        "window_start": now + int(starts_in_s * S),
        "window_end": now + int((starts_in_s + 300) * S),
        "closed_at": None if closed_ago_s is None else now - int(closed_ago_s * S),
        "instruments": [{"token_id": f"{name}-up"}, {"token_id": f"{name}-down"}],
    }


def test_markets_hours_ahead_are_not_subscribed() -> None:
    host = _host(
        [
            _market("open", -100),
            _market("soon", 600),
            _market("tonight", 6 * 3600),
            _market("tomorrow", 20 * 3600),
            _market("tail", -400, closed_ago_s=60),
            _market("done", -900, closed_ago_s=600),
        ]
    )
    tokens = Collector.tokens(host)
    assert tokens == sorted(
        f"{name}-{leg}" for name in ("open", "soon", "tail") for leg in ("up", "down")
    )


def test_a_day_of_discovered_markets_stays_a_handful_of_tokens() -> None:
    # Discovery finds every 5-minute market of the next 24 h: 288 markets, 576 tokens.
    host = _host([_market(f"m{i}", i * 300 - 150) for i in range(288)])
    assert len(Collector.tokens(host)) <= 2 * (900 // 300 + 2)


def test_ended_windows_leave_the_socket_even_without_a_closed_flag() -> None:
    # 24 Oct recording: Gamma's "closed" never arrived for ended windows, so the socket
    # grew by one market every 5 minutes (154 tokens after 6 h) and the venue dropped it.
    past = [_market(f"p{i}", -i * 300 - 450) for i in range(72)]  # ended 2.5 min to 6 h ago
    host = _host([*past, _market("open", -100), _market("soon", 600)])
    names = {t.rsplit("-", 1)[0] for t in Collector.tokens(host)}
    assert names == {"open", "soon"}
    # Within the 2-minute close tail a market is still recorded.
    host = _host([_market("tail", -360)])  # ended 60 s ago, closed flag unknown
    assert Collector.tokens(host) == ["tail-down", "tail-up"]


def test_subscription_must_cover_the_pre_window_capture() -> None:
    with pytest.raises(ValueError):
        Profile(coverage=Coverage(book_subscribe_ahead_s=60, pre_window_capture_s=300)).validate()
