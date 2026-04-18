from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tyrex_pm.runtime.live_attest import cmd_live_attest


@pytest.fixture(autouse=True)
def _redirect_runs_dir_to_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stop ``cmd_live_attest`` from writing into the real ``var/reporting/runs/``.

    Without this guard each test in this module drops a real-looking run folder into the
    repo's ``var/reporting/runs/`` directory because ``cmd_live_attest`` resolves
    ``runs_dir`` from the loaded ``AppConfig`` (which defaults to ``var/reporting/runs``).
    We monkeypatch ``load_app_config`` to override the resolved ``runs_dir`` to an absolute
    path under ``tmp_path``; the absolute path absorbs the ``root /`` join inside
    ``cmd_live_attest``, so artifacts land under ``tmp_path`` instead of the repo.
    """
    import tyrex_pm.runtime.live_attest as _la

    original = _la.load_app_config

    def _patched(*args, **kwargs):
        cfg = original(*args, **kwargs)
        new_reporting = dataclasses.replace(cfg.runtime.reporting, runs_dir=str(tmp_path))
        new_runtime = dataclasses.replace(cfg.runtime, reporting=new_reporting)
        return dataclasses.replace(cfg, runtime=new_runtime)

    monkeypatch.setattr("tyrex_pm.runtime.live_attest.load_app_config", _patched)


@pytest.mark.asyncio
async def test_live_attest_exits_2_on_placeholder_token_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    args = argparse.Namespace(
        repo_root=root,
        strategy="config/strategies/guru_follow.yaml",
        scenario="live_attest",
        token_id="<TOKEN>",
        size="1",
        price="0.01",
        side="BUY",
        readiness_timeout_s=5.0,
    )
    code = await cmd_live_attest(args)
    assert code == 2


@pytest.mark.asyncio
async def test_live_attest_exits_2_when_no_clob_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        "tyrex_pm.runtime.live_attest.try_create_clob_client",
        lambda: None,
    )
    args = argparse.Namespace(
        repo_root=root,
        strategy="config/strategies/guru_follow.yaml",
        scenario="live_attest",
        token_id="123",
        size="1",
        price="0.01",
        side="BUY",
        readiness_timeout_s=5.0,
    )
    code = await cmd_live_attest(args)
    assert code == 2


@pytest.mark.asyncio
async def test_live_attest_success_path_mocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]

    from decimal import Decimal

    from tyrex_pm.core.time import utc_now

    fake_clob = MagicMock()
    fake_clob.creds = MagicMock(api_key="k", api_secret="s", api_passphrase="p")

    async def _refresh(wallet, _clob):
        wallet.usdc_balance = Decimal("1000000")
        wallet.usdc_allowance = Decimal("1000000")
        wallet.last_sync_ts = utc_now()

    async def _noop(*_a, **_k):
        return None

    async def _heartbeat_sets_ok(health, _bridge, _interval_s, _sink, *, run_id, stop):
        health.mark_heartbeat(ok=True)
        await stop.wait()

    monkeypatch.setattr("tyrex_pm.runtime.live_attest.try_create_clob_client", lambda: fake_clob)
    monkeypatch.setattr("tyrex_pm.runtime.live_attest.refresh_wallet_from_clob", _refresh)
    monkeypatch.setattr("tyrex_pm.runtime.live_attest.supervised_heartbeat_loop", _heartbeat_sets_ok)
    monkeypatch.setattr("tyrex_pm.runtime.live_attest.venue_refresh_loop", _noop)
    monkeypatch.setattr("tyrex_pm.runtime.live_attest.run_user_ws_ingest", _noop)
    monkeypatch.setattr("tyrex_pm.runtime.live_attest.user_ws_staleness_loop", _noop)

    bridge = MagicMock()
    bridge.post_heartbeat = AsyncMock(return_value={"ok": True})
    bridge.create_and_post_limit = AsyncMock(return_value={"orderID": "0xvenue1", "status": "live"})
    bridge.cancel_order = AsyncMock(return_value={"canceled": "0xvenue1"})

    monkeypatch.setattr("tyrex_pm.runtime.live_attest.PyClobBridge", lambda _c: bridge)

    args = argparse.Namespace(
        repo_root=root,
        strategy="config/strategies/guru_follow.yaml",
        scenario="live_attest",
        token_id="1234567890",
        size="1",
        price="0.01",
        side="BUY",
        readiness_timeout_s=5.0,
    )
    code = await cmd_live_attest(args)
    assert code == 0
    assert bridge.create_and_post_limit.await_count >= 1
    assert bridge.cancel_order.await_count >= 1
