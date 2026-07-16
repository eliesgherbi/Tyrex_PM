"""BOTH_ENTRY_PENDING shutdown flatten tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import (
    YES,
    app_cfg,
    coord_with_books,
    facts_from_sink,
    seed_no_leg,
)


@pytest.mark.asyncio
async def test_entry_pending_one_filled_leg_force_sells(tmp_path: Path) -> None:
    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_no_leg(coord, qty=Decimal("5"))
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_ENTRY_PENDING,
        pair_correlation_id="entry-pending",
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id=YES,
        no_token_id=cfg.no_token_id,
    )
    state.no.filled_qty = Decimal("5")
    state.no.target_qty = Decimal("5")
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("entry-pending-shutdown"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert "paired_binary_open_exposure_at_shutdown" in types
    assert "paired_binary_shutdown_force_flatten_started" in types
    sells = [f for f in facts if f["fact_type"] == "intent_created" and f["payload"].get("side") == "SELL"]
    assert sells
    assert state.phase != PairedBinaryPhase.BOTH_ENTRY_PENDING
