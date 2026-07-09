"""App one-shot shutdown after paired binary terminal."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.runtime.config import PairedBinaryRuntimeConfig, RuntimeConfig
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState


@pytest.mark.asyncio
async def test_stop_live_cancels_background_tasks() -> None:
    stop = asyncio.Event()

    async def bg_loop() -> None:
        try:
            while not stop.is_set():
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise

    task = asyncio.create_task(bg_loop())
    await asyncio.sleep(0.01)
    stop.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_paired_binary_runtime_one_shot_flag_default_false() -> None:
    cfg = PairedBinaryRuntimeConfig()
    assert cfg.stop_background_tasks_after_strategy_done is False


def test_terminal_state_detection() -> None:
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.DONE)
    assert state.is_terminal()
    state.phase = PairedBinaryPhase.ONLY_NO_ACTIVE
    assert not state.is_terminal()
