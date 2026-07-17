"""R7A.1 BTC 5m market-window and deadline invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tyrex_pm.adapters.polymarket.btc_5m_window import (
    BTC_5M_WINDOW_S,
    MarketWindowError,
    compute_lifecycle_deadlines,
    parse_slug_epoch,
    parse_title_window_utc,
    resolve_btc_5m_window,
    window_from_slug_epoch,
)


def test_slug_epoch_produces_correct_utc_start() -> None:
    slug = "btc-updown-5m-1784268900"
    epoch = parse_slug_epoch(slug)
    start, end = window_from_slug_epoch(slug, epoch)
    assert start == datetime(2026, 7, 17, 6, 15, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == BTC_5M_WINDOW_S


def test_five_minute_duration_and_title_agree() -> None:
    slug = "btc-updown-5m-1784268900"
    title = "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET"
    event = {
        "startDate": "2026-07-16T06:22:37.881504Z",  # listing — must not be market_start
        "startTime": "2026-07-17T06:15:00Z",
        "endDate": "2026-07-17T06:20:00Z",
        "createdAt": "2026-07-16T06:21:42.908646Z",
        "title": title,
        "closed": False,
        "active": True,
    }
    market = {
        "eventStartTime": "2026-07-17T06:15:00Z",
        "startDate": "2026-07-16T06:22:37.881504Z",
        "endDate": "2026-07-17T06:20:00Z",
        "createdAt": "2026-07-16T06:21:43.095233Z",
        "acceptingOrders": True,
        "closed": False,
        "active": True,
        "question": title,
    }
    w = resolve_btc_5m_window(slug=slug, event=event, market=market)
    assert w.market_start.isoformat() == "2026-07-17T06:15:00+00:00"
    assert w.market_end.isoformat() == "2026-07-17T06:20:00+00:00"
    assert w.listed_at is not None
    assert w.listed_at.date().isoformat() == "2026-07-16"
    assert w.listed_at != w.market_start


def test_creation_time_cannot_become_market_start() -> None:
    slug = "btc-updown-5m-1784268900"
    # If we incorrectly trusted startDate, duration would be ~1 day — we must not.
    w = resolve_btc_5m_window(
        slug=slug,
        event={
            "startDate": "2026-07-16T06:22:37Z",
            "startTime": "2026-07-17T06:15:00Z",
            "endDate": "2026-07-17T06:20:00Z",
            "title": "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET",
        },
        market={
            "eventStartTime": "2026-07-17T06:15:00Z",
            "startDate": "2026-07-16T06:22:37Z",
            "endDate": "2026-07-17T06:20:00Z",
            "acceptingOrders": True,
            "question": "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET",
        },
    )
    assert (w.market_end - w.market_start).total_seconds() == 300


def test_entry_deadline_precedes_flatten_and_approval() -> None:
    slug = "btc-updown-5m-1784268900"
    w = resolve_btc_5m_window(
        slug=slug,
        event={
            "startTime": "2026-07-17T06:15:00Z",
            "endDate": "2026-07-17T06:20:00Z",
            "title": "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET",
        },
        market={
            "eventStartTime": "2026-07-17T06:15:00Z",
            "endDate": "2026-07-17T06:20:00Z",
            "acceptingOrders": True,
            "question": "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET",
        },
        now=datetime(2026, 7, 17, 6, 15, 30, tzinfo=timezone.utc),
    )
    d = compute_lifecycle_deadlines(
        w, now=datetime(2026, 7, 17, 6, 15, 30, tzinfo=timezone.utc)
    )
    assert d.approval_expiration <= d.entry_deadline < d.flatten_deadline < d.market_end
    # Never a +10m entry deadline past market_end
    assert d.entry_deadline < w.market_end
    assert (d.entry_deadline - datetime(2026, 7, 17, 6, 15, 30, tzinfo=timezone.utc)) < timedelta(
        minutes=10
    )


def test_insufficient_time_blocks() -> None:
    slug = "btc-updown-5m-1784268900"
    w = resolve_btc_5m_window(
        slug=slug,
        event={
            "startTime": "2026-07-17T06:15:00Z",
            "endDate": "2026-07-17T06:20:00Z",
            "title": "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET",
        },
        market={
            "eventStartTime": "2026-07-17T06:15:00Z",
            "endDate": "2026-07-17T06:20:00Z",
            "acceptingOrders": True,
            "question": "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET",
        },
        require_accepting_orders=False,
    )
    with pytest.raises(MarketWindowError, match="INSUFFICIENT_TIME_REMAINING"):
        compute_lifecycle_deadlines(
            w, now=datetime(2026, 7, 17, 6, 19, 0, tzinfo=timezone.utc)
        )


def test_closed_market_blocks() -> None:
    with pytest.raises(MarketWindowError, match="MARKET_NOT_ACCEPTING_ORDERS"):
        resolve_btc_5m_window(
            slug="btc-updown-5m-1784268900",
            event={
                "startTime": "2026-07-17T06:15:00Z",
                "endDate": "2026-07-17T06:20:00Z",
                "title": "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET",
                "closed": True,
            },
            market={
                "eventStartTime": "2026-07-17T06:15:00Z",
                "endDate": "2026-07-17T06:20:00Z",
                "acceptingOrders": False,
                "closed": True,
                "question": "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET",
            },
        )


def test_title_parser() -> None:
    parsed = parse_title_window_utc(
        "Bitcoin Up or Down - July 17, 2:15AM-2:20AM ET", year=2026
    )
    assert parsed is not None
    assert parsed[0] == datetime(2026, 7, 17, 6, 15, tzinfo=timezone.utc)
    assert parsed[1] == datetime(2026, 7, 17, 6, 20, tzinfo=timezone.utc)
