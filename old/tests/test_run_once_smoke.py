"""Regression smoke: one-shot run dispatch unchanged after execute_run extraction."""

from __future__ import annotations

import argparse
import asyncio
import inspect

import pytest


def test_cmd_run_delegates_to_execute_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from tyrex_pm.runtime import app

    called = []

    async def _fake_execute(args):
        called.append(args)
        return 0

    monkeypatch.setattr("tyrex_pm.runtime.run_once.execute_run", _fake_execute)

    args = argparse.Namespace(strategy="config/strategies/paired_binary.yaml")
    result = asyncio.run(app.cmd_run(args))
    assert result == 0
    assert len(called) == 1
    assert called[0] is args


def test_execute_run_is_async_in_run_once_module() -> None:
    from tyrex_pm.runtime.run_once import execute_run

    assert inspect.iscoroutinefunction(execute_run)
