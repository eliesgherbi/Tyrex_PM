"""A recording finishes its open window, then waits for the winners of its windows."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from tyrex_pm.data_warehouse import supervisor
from tyrex_pm.data_warehouse.capture.files import atomic_json, read_json
from tyrex_pm.data_warehouse.config import Profile, Resolution, Storage
from tyrex_pm.data_warehouse.supervisor import Collector

MS = 10**6


def _collector(tmp_path, monkeypatch, *, collection=None) -> Collector:
    profile = replace(
        Profile(),
        storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1),
        resolution=replace(Resolution(), poll_initial_s=1),
    )
    collector = Collector(tmp_path, profile, None, normalize_live=False, collection=collection)
    monkeypatch.setattr(supervisor, "CLOSE_MARGIN_S", 0)

    async def idle(*_args, **_kwargs):
        await collector.stop.wait()

    async def no_op():
        return None

    monkeypatch.setattr(supervisor, "record_stream", idle)
    for owner, attr in (
        (collector.discovery, "cycle"),
        (collector.discovery, "scan"),
        (collector, "metadata_cycle"),
        (collector, "book_audit"),
        (collector, "clocks"),
        (collector, "supervise_normalizer"),
        (collector, "supervise_quality"),
    ):
        monkeypatch.setattr(owner, attr, no_op)
    return collector


def _market(mid: str, start: int, end: int) -> dict:
    return {
        "market_id": mid,
        "window_start": start,
        "window_end": end,
        "instruments": [],
        "resolution_status": "PENDING",
    }


def _resolves(collector: Collector, calls: list) -> None:
    async def resolution_cycle():
        calls.append(collector.phase)
        if collector.phase == "awaiting_outcomes":
            for mid in collector.outcome_scope or ():
                collector.markets[mid] = {
                    **collector.markets[mid],
                    "resolution_status": "PROVIDER_TERMINAL",
                }

    collector.resolution_cycle = resolution_cycle


@pytest.mark.asyncio
async def test_open_window_is_finished_then_its_winner_awaited(tmp_path, monkeypatch) -> None:
    collector = _collector(tmp_path, monkeypatch)
    start = collector.started_at
    collector.markets = {
        "done": _market("done", start + 10 * MS, start + 40 * MS),
        "open": _market("open", start + 40 * MS, start + 400 * MS),
        "next": _market("next", start + 400 * MS, start + 800 * MS),
    }
    calls: list = []
    _resolves(collector, calls)
    began = time.monotonic()
    await collector.run(0.05, finish_windows=True, outcome_wait_s=10)
    session = read_json(collector.session_path)
    assert time.monotonic() - began >= 0.3  # waited for the open window to close
    assert session["windows_until"] == start + 400 * MS
    assert session["capture_ended_at"] >= start + 400 * MS
    assert session["outcome_wait"] == {"windows": 2, "known": 2, "missing": []}
    assert collector.outcome_scope == {"done", "open"}  # "next" is not this recording's
    assert "awaiting_outcomes" in calls


@pytest.mark.asyncio
async def test_outcome_wait_ends_at_its_limit(tmp_path, monkeypatch) -> None:
    collector = _collector(tmp_path, monkeypatch)
    start = collector.started_at
    collector.markets = {"open": _market("open", start + 10 * MS, start + 100 * MS)}

    async def never():
        return None

    collector.resolution_cycle = never
    await collector.run(0.05, finish_windows=True, outcome_wait_s=0.3)
    session = read_json(collector.session_path)
    assert session["outcome_wait"] == {"windows": 1, "known": 0, "missing": ["open"]}
    assert session["clean_stop"] is True


@pytest.mark.asyncio
async def test_stop_while_finishing_keeps_the_cut_window_out(tmp_path, monkeypatch) -> None:
    collector = _collector(tmp_path, monkeypatch)
    start = collector.started_at
    collector.markets = {"open": _market("open", start + 10 * MS, start + 5_000 * MS)}
    _resolves(collector, [])

    async def stop_soon():
        await asyncio.sleep(0.2)
        collector.stop.set()

    stopper = asyncio.create_task(stop_soon())
    await collector.run(0.05, finish_windows=True, outcome_wait_s=10)
    await stopper
    session = read_json(collector.session_path)
    assert session["windows_until"] < start + 5_000 * MS  # stopped inside the window
    assert session["outcome_wait"] is None  # a stop request ends every phase


@pytest.mark.asyncio
async def test_outcomes_only_session_polls_earlier_windows(tmp_path, monkeypatch) -> None:
    first = _collector(tmp_path, monkeypatch)
    await first.run(0.01)  # releases the storage root
    now = time.time_ns()
    atomic_json(
        first.collection_dir / "sessions" / "earlier.json",
        {
            "session_id": "earlier",
            "clean_stop": True,
            "started_at": now - 3_000 * MS,
            "stopped_at": now - 500 * MS,
            "windows_until": now - 1_000 * MS,
        },
    )
    collector = _collector(tmp_path, monkeypatch, collection=first.collection)
    collector.markets = {
        "kept": _market("kept", now - 2_000 * MS, now - 1_500 * MS),
        "cut": _market("cut", now - 1_200 * MS, now - 700 * MS),  # ends after windows_until
    }
    calls: list = []
    _resolves(collector, calls)
    await collector.run(outcomes_only=True, outcome_wait_s=10)
    session = read_json(collector.session_path)
    assert session["kind"] == "outcomes_only"
    assert session["windows_until"] == session["started_at"]  # captured no window
    assert collector.outcome_scope == {"kept"}
    assert session["outcome_wait"]["known"] == 1
