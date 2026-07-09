"""Token-pair mismatch must ignore persisted lifecycle but keep wallet bootstrap."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_PAIRED_BINARY_STATE_RECOVERY_IGNORED
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.runtime.paired_binary_recovery import recover_on_startup
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import (
    NO,
    YES,
    app_cfg,
    both_legs_active_state,
    coord_with_books,
    facts_from_sink,
    seed_both_legs,
    write_persisted_state,
)

ALT_YES = YES[:-1] + "1"
ALT_NO = NO[:-1] + "2"


def test_persisted_done_different_tokens_ignored_and_idle(tmp_path: Path) -> None:
    write_persisted_state(
        tmp_path,
        PairedBinaryRuntimeState(
            phase=PairedBinaryPhase.DONE,
            owner_id="paired_binary",
            market_id="m1",
            yes_token_id=YES,
            no_token_id=NO,
        ),
    )
    app = app_cfg(yes_token_id=ALT_YES, no_token_id=ALT_NO)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("mismatch-done"),
            pb_rt=app.runtime.paired_binary,
        )
        facts = facts_from_sink(sink)
    assert state.phase == PairedBinaryPhase.IDLE
    ignored = [f for f in facts if f["fact_type"] == FACT_TYPE_PAIRED_BINARY_STATE_RECOVERY_IGNORED]
    assert ignored
    assert ignored[0]["payload"]["reason"] == "token_pair_mismatch"


def test_persisted_both_legs_active_different_tokens_wallet_bootstrap(tmp_path: Path) -> None:
    persisted = both_legs_active_state()
    ensure_pnl_budgets(persisted, app_cfg().paired_binary)
    write_persisted_state(tmp_path, persisted)

    app = app_cfg(yes_token_id=ALT_YES, no_token_id=ALT_NO)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    inject_fixture_book(coord, ALT_YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, ALT_NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    coord.wallet.positions[TokenId(ALT_YES)] = WalletPosition(
        token_id=TokenId(ALT_YES), qty=Decimal("5"), avg_price_usd=Decimal("0.50")
    )
    coord.wallet.positions[TokenId(ALT_NO)] = WalletPosition(
        token_id=TokenId(ALT_NO), qty=Decimal("5"), avg_price_usd=Decimal("0.51")
    )
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(ALT_YES), Decimal("5"), correlation_id="y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(ALT_NO), Decimal("5"), correlation_id="n")

    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("mismatch-active"),
            pb_rt=app.runtime.paired_binary,
        )
        facts = facts_from_sink(sink)
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_FILLED
    assert state.yes_token_id == ALT_YES
    assert state.no_token_id == ALT_NO
    ignored = [f for f in facts if f["fact_type"] == FACT_TYPE_PAIRED_BINARY_STATE_RECOVERY_IGNORED]
    assert ignored
    assert ignored[0]["payload"]["reason"] == "token_pair_mismatch"
    applied = [f for f in facts if f["fact_type"] == "paired_binary_state_recovery_applied"]
    assert not applied


def test_persisted_failed_different_tokens_resets_idle(tmp_path: Path) -> None:
    write_persisted_state(
        tmp_path,
        PairedBinaryRuntimeState(
            phase=PairedBinaryPhase.FAILED,
            owner_id="paired_binary",
            market_id="m1",
            yes_token_id=YES,
            no_token_id=NO,
        ),
    )
    app = app_cfg(yes_token_id=ALT_YES, no_token_id=ALT_NO)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("mismatch-failed"),
            pb_rt=app.runtime.paired_binary,
        )
    assert state.phase == PairedBinaryPhase.IDLE
