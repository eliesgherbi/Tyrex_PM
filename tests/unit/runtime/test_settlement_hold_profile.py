"""Hold-to-resolution profile: config rules, entry gate, commitment, flatten/shutdown, startup."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.execution.session_state import ExecutionPhase
from tyrex_pm.execution.settlement.ledger import EpisodeState, SettlementLedger
from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config
from tyrex_pm.runtime.settlement_hooks import SettlementHooks

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "config" / "runs" / "settlement_value_trial.yaml"
LEGACY = ROOT / "config" / "runs" / "z_gap_tiny_live.yaml"
CURVE = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))
GAMMA_FEES = {"exponent": 1, "rate": "0.07", "rebateRate": "0.2", "takerOnly": True}


def write(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(TRIAL.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_trial_profile_and_legacy_profile_validate() -> None:
    trial = load_trading_run_config(TRIAL)
    assert trial.lifecycle_mode == "hold_to_resolution" and trial.settlement is not None
    assert trial.settlement.trial.max_debit_per_episode == Decimal("5")
    legacy = load_trading_run_config(LEGACY)
    assert legacy.lifecycle_mode == "flatten_before_close" and legacy.settlement is None


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r["lifecycle"].pop("mode"), "supports lifecycle.mode"),
        (lambda r: r.pop("settlement"), "requires a settlement block"),
        (lambda r: r["settlement"]["trial"].pop("trial_loss_budget"), "trial_loss_budget"),
        (lambda r: r["strategy"]["parameters"]["entry"].update(target_mode="require_target"),
         "cap_only"),
        (lambda r: r["settlement"]["trial"].update(max_debit_per_episode="6"), "tiny-live"),
        (lambda r: r["settlement"].update(redemption_mode="automatic"), "operator"),
    ],
)
def test_hold_profile_rejections(tmp_path: Path, mutate, match: str) -> None:
    with pytest.raises(RunConfigError, match=match):
        load_trading_run_config(write(tmp_path, mutate))


def test_legacy_strategy_cannot_use_hold_mode(tmp_path: Path) -> None:
    raw = yaml.safe_load(LEGACY.read_text(encoding="utf-8"))
    raw["lifecycle"]["mode"] = "hold_to_resolution"
    raw["settlement"] = yaml.safe_load(TRIAL.read_text(encoding="utf-8"))["settlement"]
    path = tmp_path / "z.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RunConfigError, match="supports lifecycle.mode"):
        load_trading_run_config(path)


def hooks(tmp_path: Path, fees=GAMMA_FEES, **trial) -> SettlementHooks:
    config = load_trading_run_config(TRIAL)
    if trial:
        from dataclasses import replace

        config = replace(config, settlement=replace(
            config.settlement, trial=replace(config.settlement.trial, **trial)))

    async def source(slug, condition):
        if isinstance(fees, Exception):
            raise fees
        return fees

    return SettlementHooks(config=config, ledger=SettlementLedger(tmp_path / "l.jsonl"),
                           fee_schedule_source=source)


def entry(h: SettlementHooks, window="w1", **kw):
    args = dict(window_id=window, window_slug=window, market_id="m", condition_id="0xc",
                token_id="tok", outcome_index=0, target_notional=Decimal("5"),
                tick_size=Decimal("0.01"), min_order_size=Decimal("5"), strategy_fee_curve=CURVE)
    args.update(kw)
    return asyncio.run(h.before_entry(**args))


def test_entry_gate_blocks_unverified_constraints_and_reserves_on_success(tmp_path: Path) -> None:
    assert entry(hooks(tmp_path / "a", fees=None)).reasons == ("FEE_SCHEDULE_MISSING",)
    failed = entry(hooks(tmp_path / "b", fees=TimeoutError()))
    assert "FEE_SCHEDULE_READ_FAILED:TimeoutError" in failed.reasons
    other = FeeCurveParams(fee_rate=Decimal("0.05"), exponent=Decimal("1"))
    assert entry(hooks(tmp_path / "c"), strategy_fee_curve=other).reasons == (
        "STRATEGY_FEE_CURVE_MISMATCH",
    )
    assert "MIN_ORDER_SIZE_UNKNOWN" in entry(hooks(tmp_path / "d"), min_order_size=None).reasons
    h = hooks(tmp_path / "e")
    ok = entry(h)
    assert ok.allowed
    assert h.ledger.episodes["settlement_value_trial:w1"].state is EpisodeState.RESERVED
    again = entry(h)
    assert "EPISODE_ALREADY_EXISTS_FOR_WINDOW" in again.reasons


def test_budget_counts_pending_reservations_before_dispatch(tmp_path: Path) -> None:
    h = hooks(tmp_path, trial_loss_budget=Decimal("8"), max_unresolved_count=5,
              max_unresolved_cost=Decimal("50"))
    assert entry(h, window="w1").allowed  # 5 reserved
    second = entry(h, window="w2")  # 5 + 5 = 10 > 8
    assert not second.allowed and "TRIAL_LOSS_BUDGET" in second.reasons


def state(phase=ExecutionPhase.POSITION_OPEN, shares="10", resting=(), unresolved=False):
    return SimpleNamespace(phase=phase, confirmed_position_shares=Decimal(shares),
                           resting_limit_venue_ids=tuple(resting), working_maker_entry=False,
                           has_unresolved_mutation_attempt=unresolved)


def test_outcome_mapping_and_hold_commitment(tmp_path: Path) -> None:
    h = hooks(tmp_path)
    entry(h)
    assert h.after_entry(window_id="w1", state=state(), cost_basis=Decimal("4")) == "FILL_CONFIRMED"
    totals = h.ledger.totals()
    assert totals.reserved_cash == 0 and totals.spent_unresolved == Decimal("4")

    async def cancel_fails():
        return state(resting=("o1",))

    ok, detail = asyncio.run(h.commit_hold(window_id="w1", state=state(resting=("o1",)),
                                           cancel_conflicting=cancel_fails))
    assert not ok and detail == "CONFLICTING_ORDER_NOT_PROVEN_CANCELLED"

    async def cancel_ok():
        return state()

    ok, detail = asyncio.run(h.commit_hold(window_id="w1", state=state(resting=("o1",)),
                                           cancel_conflicting=cancel_ok))
    assert ok and h.is_committed("w1")
    assert ("0xc", "tok") in h.committed_holds()


def test_no_fill_releases_and_unknown_keeps_reservation(tmp_path: Path) -> None:
    h = hooks(tmp_path, max_unresolved_count=5, max_unresolved_cost=Decimal("50"),
              trial_loss_budget=Decimal("50"))
    entry(h, window="w1")
    no_fill = state(phase=ExecutionPhase.COMPLETED_NO_FILL, shares="0")
    assert h.after_entry(window_id="w1", state=no_fill, cost_basis=Decimal(0)) == "DISPATCH_NO_FILL"
    assert h.ledger.totals().reserved_cash == 0
    assert entry(h, window="w2").allowed
    unknown = state(phase=ExecutionPhase.MANUAL_INTERVENTION, shares="0")
    assert h.after_entry(window_id="w2", state=unknown, cost_basis=Decimal(0)) == "DISPATCH_UNKNOWN"
    assert h.ledger.totals().reserved_cash == Decimal("5")
    assert h.startup_blockers() == ("SETTLEMENT_EPISODE_NEEDS_RECONCILIATION",)


def _tick_runtime(committed: bool, tau: float):
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    lifecycle = SimpleNamespace(state=SimpleNamespace(has_exposure=True, exit_requested_reason=None,
                                                      needs_venue_followup=False),
                                mark_manual=AsyncMock())
    runtime = SimpleNamespace(
        _bind_pending_market=AsyncMock(),
        active_market=SimpleNamespace(window_id="w1", entry_enabled=True,
                                      market=SimpleNamespace(condition_id="c", event_end=object(),
                                                             market_id=SimpleNamespace(value="m"))),
        account_state_authority=SimpleNamespace(current=lambda _m: object()),
        _apply_account_snapshot=lambda _s: None,
        _market_data_ready=lambda: True,
        _current_entry_tau_s=lambda: tau,
        config=SimpleNamespace(lifecycle=SimpleNamespace(manual_deadline_before_end_s=5,
                                                         mandatory_exit_before_end_s=20)),
        capabilities=SimpleNamespace(entry_window_open=False),
        lifecycle=lifecycle,
        settlement_hooks=SimpleNamespace(is_committed=lambda window: committed),
        _refresh_order_metadata_capability=lambda: None,
        _enqueue_intent=lambda intent: (_ for _ in ()).throw(AssertionError("flatten enqueued")),
        stop_requested=False,
    )
    return TradingRuntime, runtime


def test_committed_hold_is_exempt_from_manual_deadline_and_mandatory_flatten(monkeypatch) -> None:
    import tyrex_pm.runtime.trading_runtime as tr

    monkeypatch.setattr(tr, "entry_tau_bounds", lambda config: (30, 240))
    monkeypatch.setattr(tr, "strategy_entry_cutoff_s", lambda config: 30)
    cls, runtime = _tick_runtime(committed=True, tau=2.0)
    asyncio.run(cls.on_async_tick(runtime, SimpleNamespace(indicator_graph=None)))
    runtime.lifecycle.mark_manual.assert_not_called()
    assert runtime.stop_requested is False


def test_shutdown_preserves_a_committed_hold() -> None:
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    recorded = []
    runtime = SimpleNamespace(
        _cleanup_lock=asyncio.Lock(), _cleanup_done=False, stop_requested=False,
        capabilities=SimpleNamespace(entry_window_open=True, set_blocker=lambda *a: None),
        _stop_intent_dispatcher=AsyncMock(),
        active_market=SimpleNamespace(window_id="w1"),
        settlement_hooks=SimpleNamespace(is_committed=lambda window: True),
        _cancel_for_hold=AsyncMock(),
        lifecycle=SimpleNamespace(drain_open_session=AsyncMock()),
        run_recorder=SimpleNamespace(record=lambda kind, payload: recorded.append(kind)),
    )
    asyncio.run(TradingRuntime._shutdown_execution(runtime, "test"))
    runtime.lifecycle.drain_open_session.assert_not_called()
    runtime._cancel_for_hold.assert_awaited_once()
    assert recorded == ["SHUTDOWN_HOLD_PRESERVED"]


def test_startup_never_offers_to_close_a_committed_hold() -> None:
    from tyrex_pm.execution.startup_reconciliation import (
        AccountIdentity,
        Holding,
        InspectionFacts,
        ReadCoverage,
        assess,
    )

    session = SimpleNamespace(
        identity=SimpleNamespace(market_id="0xc", token_id="tok", window_id="w1"),
        session_id="s1", safe_to_open_entry=False, orders={}, resolved_attempt_ids=set(),
        confirmed_position_shares=Decimal("10"), external_activity_ids=set(), trades={},
        opened_at=None, resting_limit_venue_ids=(), has_journal_uncertainty=False,
        has_unresolved_mutation_attempt=False, entry_match_awaiting_confirmation=False,
        has_confirmed_buy=True, external_reduction_shares=Decimal(0), dust_residual=None,
        operational_disposition=None,
    )
    holding = Holding(condition_id="0xc", token_id="tok", shares=Decimal("10"))
    base = dict(
        identity=AccountIdentity(chain_id=137, environment="production", wallet="0x1"),
        coverage=ReadCoverage(complete=True, positions_complete=True, orders_complete=True,
                              activity_complete=True),
        sessions=(session,), holdings=(holding,),
    )
    legacy = assess(InspectionFacts(**base))
    assert any(p.action == "cancel_and_close" for p in legacy.proposals)
    held = assess(InspectionFacts(**base, committed_holds=frozenset({("0xc", "tok")})))
    assert not held.proposals
    assert any(i.code == "COMMITTED_HOLD_AWAITING_RESOLUTION" for i in held.issues)


def test_settlement_cli_offline_commands_and_chain_guard(tmp_path: Path, capsys) -> None:
    from tyrex_pm.application.cli import main

    cfg = write(tmp_path, lambda r: r["settlement"].update(ledger_path=str(tmp_path / "l.jsonl")))
    assert main(["settlement", "--config", str(cfg), "status"]) == 0
    assert '"episodes": []' in capsys.readouterr().out
    assert main(["settlement", "--config", str(cfg), "reconcile"]) == 2
    assert main(["settlement", "--config", str(cfg), "mark-manual", "--episode", "e1",
                 "--reason", "operator"]) == 2  # unknown episode
    SettlementLedger(tmp_path / "l.jsonl").record(
        "EPISODE_RESERVED", "e1", window_id="w", market_id="m", condition_id="0xc",
        token_id="t", outcome_index=0, max_debit="5", max_fees="0",
    )
    assert main(["settlement", "--config", str(cfg), "mark-manual", "--episode", "e1",
                 "--reason", "operator"]) == 0
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    assert ledger.episodes["e1"].state is EpisodeState.MANUAL_INTERVENTION
    assert main(["settlement", "--config", str(LEGACY), "status"]) == 2
