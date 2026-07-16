"""Unit tests for run_continue orchestrator."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tyrex_pm.ingestion.btc_5m_window_scheduler import select_next_btc_5m_trading_window
from tyrex_pm.ingestion.market_discovery import MarketDiscoveryResult
from tyrex_pm.runtime.run_continue import (
    RunContinueConfig,
    build_window_run_name,
    resolve_window_metadata,
    run_continue_session,
    session_dir,
    wait_until,
)


def _config(*, max_windows: int | None = None, tmp_path: Path) -> RunContinueConfig:
    return RunContinueConfig(
        strategy="config/strategies/paired_binary.yaml",
        scenario="live_paired_binary_phase1_trailing_enforce",
        session_run_name="test_session",
        reference_event_url="https://polymarket.com/fr/event/btc-updown-5m-1783369200",
        repo_root=tmp_path,
        state_dir="var/state",
        prestart_seconds=30.0,
        entry_grace_seconds=15.0,
        sleep_granularity=0.05,
        max_windows=max_windows,
    )


def _discovery(market_id: str, *, start_ts: float) -> MarketDiscoveryResult:
    return MarketDiscoveryResult(
        market_id=market_id,
        condition_id="0xcond",
        yes_token_id="yes",
        no_token_id="no",
        event_start_ts=start_ts,
        event_end_ts=start_ts + 300.0,
        event_slug=f"btc-updown-5m-{int(start_ts)}",
    )


@contextmanager
def _patch_session_loop(*, plan, resolve_fn):
    import tyrex_pm.runtime.run_continue as rc_mod

    async def _immediate_wait(_wake, _stop, _gran):
        return

    saved = (
        rc_mod.resolve_window_metadata,
        rc_mod.wait_until,
        rc_mod.select_next_btc_5m_trading_window,
    )
    rc_mod.resolve_window_metadata = resolve_fn
    rc_mod.wait_until = _immediate_wait
    rc_mod.select_next_btc_5m_trading_window = lambda **kwargs: plan
    try:
        yield
    finally:
        rc_mod.resolve_window_metadata = saved[0]
        rc_mod.wait_until = saved[1]
        rc_mod.select_next_btc_5m_trading_window = saved[2]


@pytest.mark.asyncio
async def test_wait_until_exits_early_on_stop() -> None:
    stop = asyncio.Event()
    stop.set()
    await wait_until(time.time() + 60.0, stop, granularity=0.05)


@pytest.mark.asyncio
async def test_run_name_generation_is_safe() -> None:
    name = build_window_run_name("phase1_trailing_continue", "btc_5m_20260705_1705")
    assert name == "phase1_trailing_continue__btc_5m_20260705_1705"
    assert "/" not in name


@pytest.mark.asyncio
async def test_max_windows_one_runs_single_execute(tmp_path: Path) -> None:
    execute = AsyncMock(return_value=0)
    stop = asyncio.Event()
    plan = select_next_btc_5m_trading_window(now_ts=1_783_369_800.0 + 220.0)
    meta = _discovery("btc_5m_test_window", start_ts=float(plan.window_start_ts))

    async def _fake_resolve(_plan, *, entry_grace_seconds, stop):
        return meta

    with _patch_session_loop(plan=plan, resolve_fn=_fake_resolve):
        code = await run_continue_session(
            _config(max_windows=1, tmp_path=tmp_path),
            stop=stop,
            execute_run=execute,
        )

    assert code == 0
    assert execute.await_count == 1
    sess = session_dir(tmp_path, "test_session")
    lines = (sess / "session_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert "session_started" in events
    assert "window_run_complete" in events
    assert events.count("window_run_start") == 1


@pytest.mark.asyncio
async def test_non_zero_execute_run_stops_session(tmp_path: Path) -> None:
    execute = AsyncMock(return_value=1)
    stop = asyncio.Event()
    plan = select_next_btc_5m_trading_window(now_ts=1_783_369_800.0 + 220.0)
    meta = _discovery("btc_5m_fail", start_ts=float(plan.window_start_ts))

    async def _fake_resolve(_plan, *, entry_grace_seconds, stop):
        return meta

    with _patch_session_loop(plan=plan, resolve_fn=_fake_resolve):
        code = await run_continue_session(
            _config(tmp_path=tmp_path),
            stop=stop,
            execute_run=execute,
        )

    assert code == 1
    assert execute.await_count == 1
    sess = session_dir(tmp_path, "test_session")
    log = (sess / "session_log.jsonl").read_text(encoding="utf-8")
    assert "window_run_failed" in log


@pytest.mark.asyncio
async def test_window_skipped_when_gamma_unavailable(tmp_path: Path) -> None:
    execute = AsyncMock(return_value=0)
    stop = asyncio.Event()
    plan = select_next_btc_5m_trading_window(now_ts=1_783_369_800.0 + 220.0)

    async def _fake_resolve(_plan, *, entry_grace_seconds, stop):
        stop.set()
        return None

    with _patch_session_loop(plan=plan, resolve_fn=_fake_resolve):
        code = await run_continue_session(
            _config(max_windows=1, tmp_path=tmp_path),
            stop=stop,
            execute_run=execute,
        )

    assert code == 0
    assert execute.await_count == 0
    sess = session_dir(tmp_path, "test_session")
    log = (sess / "session_log.jsonl").read_text(encoding="utf-8")
    assert "window_skipped" in log


@pytest.mark.asyncio
async def test_resolve_window_metadata_rejects_start_ts_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = select_next_btc_5m_trading_window(now_ts=1_783_369_800.0 + 220.0)
    stop = asyncio.Event()
    bad = _discovery("btc_5m_bad", start_ts=float(plan.window_start_ts + 60))

    monkeypatch.setattr(
        "tyrex_pm.runtime.run_continue.discover_btc_5m_by_slug",
        lambda slug: bad,
    )
    stop.set()
    result = await resolve_window_metadata(
        plan,
        entry_grace_seconds=15.0,
        stop=stop,
    )
    assert result is None
