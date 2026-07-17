"""R7C settlement / reconciliation hardening — deterministic, no network."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.mutation_lifecycle import MutationLifecycle, MutationPhase
from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from tyrex_pm.execution.polymarket.settlement import (
    FakeSettlementClock,
    SettlementPhase,
    TradeEvidence,
    TradeSettlementStatus,
    detect_manual_flat,
    evaluate_sell_readiness,
    wait_for_entry_settlement,
)
from tyrex_pm.runtime.r7b_live_once import TerminalOutcome, run_r7b_live_once

from test_r7b_live_once import _base_args, _eligible_window


FIXTURE = Path(__file__).parent / "fixtures" / "r7b_incident_d632b631_facts.jsonl"


def _trade(
    status: TradeSettlementStatus,
    size: Decimal = Decimal("9.47"),
    tid: str = "tr1",
    side: str = "BUY",
) -> TradeEvidence:
    return TradeEvidence(
        trade_id=tid,
        order_id="0x68efa63a23abb0ab55042204683f48f4303ed2db3e9d955317bc41add43e71db",
        side=side,
        size=size,
        price=Decimal("0.51"),
        status=status,
    )


def test_matched_delayed_mined_confirmed_then_sell(tmp_path: Path) -> None:
    clock = FakeSettlementClock()
    state = {"n": 0, "bal": Decimal("0")}

    def trades() -> list[TradeEvidence]:
        state["n"] += 1
        if state["n"] == 1:
            return [_trade(TradeSettlementStatus.MATCHED)]
        if state["n"] == 2:
            return [_trade(TradeSettlementStatus.MINED)]
        return [_trade(TradeSettlementStatus.CONFIRMED)]

    def bal() -> tuple[Decimal, Decimal | None]:
        # Balance may appear at MINED — still must not sell until CONFIRMED
        if state["n"] >= 2:
            state["bal"] = Decimal("9.47")
            return state["bal"], Decimal("9.47")
        return Decimal("0"), None

    spy = SpyMutationTransport()
    w = _eligible_window()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            window_provider=lambda: [w],
            settlement_trade_poller=trades,
            settlement_balance_poller=bal,
            settlement_clock=clock,
            settlement_max_wait_s=30.0,
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    assert len([r for r in spy.submitted if r.side == "BUY"]) == 1
    assert len([r for r in spy.submitted if r.side == "SELL"]) == 1
    facts = (result.facts_path or Path()).read_text(encoding="utf-8")
    assert "entry_matched_not_settled" in facts
    assert "MINED_NONTERMINAL_AWAITING_CONFIRMED" in facts
    assert "entry_confirmed" in facts


def test_balance_zero_then_available(tmp_path: Path) -> None:
    clock = FakeSettlementClock()
    n = {"i": 0}

    def trades() -> list[TradeEvidence]:
        return [_trade(TradeSettlementStatus.CONFIRMED)]

    def bal() -> tuple[Decimal, Decimal | None]:
        n["i"] += 1
        if n["i"] < 3:
            return Decimal("0"), None
        return Decimal("9.47"), Decimal("9.47")

    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=trades,
            settlement_balance_poller=bal,
            settlement_clock=clock,
            settlement_max_wait_s=20.0,
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    assert len(spy.submitted) == 2


def test_matched_retrying_confirmed() -> None:
    clock = FakeSettlementClock()
    n = {"i": 0}

    def trades() -> list[TradeEvidence]:
        n["i"] += 1
        if n["i"] == 1:
            return [_trade(TradeSettlementStatus.MATCHED)]
        if n["i"] == 2:
            return [_trade(TradeSettlementStatus.RETRYING)]
        return [_trade(TradeSettlementStatus.CONFIRMED)]

    def bal() -> tuple[Decimal, Decimal | None]:
        if n["i"] >= 3:
            return Decimal("9.47"), Decimal("9.47")
        return Decimal("0"), None

    r = wait_for_entry_settlement(
        poll_trades=trades,
        poll_balance=bal,
        order_id="0xabc",
        planned_qty=Decimal("9.47"),
        clock=clock,
        config=__import__(
            "tyrex_pm.execution.polymarket.settlement", fromlist=["SettlementWaitConfig"]
        ).SettlementWaitConfig(max_wait_s=20),
    )
    assert r.phase is SettlementPhase.ENTRY_CONFIRMED
    assert r.confirmed_acquired == Decimal("9.47")


def test_matched_failed_no_sell(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [_trade(TradeSettlementStatus.FAILED)],
            settlement_balance_poller=lambda: (Decimal("0"), None),
            settlement_clock=FakeSettlementClock(),
            settlement_max_wait_s=5.0,
        )
    )
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION
    assert len([r for r in spy.submitted if r.side == "SELL"]) == 0


def test_partial_fak_and_smaller_than_planned(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    w = _eligible_window()
    planned = w["sized"].quantity
    partial = (planned / 2).quantize(Decimal("0.01"))
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            window_provider=lambda: [w],
            settlement_trade_poller=lambda: [
                _trade(TradeSettlementStatus.CONFIRMED, size=partial)
            ],
            settlement_balance_poller=lambda: (partial, partial),
            settlement_clock=FakeSettlementClock(),
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    sell = [r for r in spy.submitted if r.side == "SELL"][0]
    assert Decimal(sell.size) == partial
    assert Decimal(sell.size) < planned


def test_conditional_balance_smaller_than_confirmed(tmp_path: Path) -> None:
    """SELL qty is capped by sellable balance; leftover confirmed qty is residual.

    R7E fail-closed: selling the entire sellable 3.00 while confirmed acquired is
    9.47 leaves accounting residual → MANUAL_INTERVENTION / RESIDUAL_EXPOSURE,
    not a false FLAT.
    """
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [
                _trade(TradeSettlementStatus.CONFIRMED, size=Decimal("9.47"))
            ],
            settlement_balance_poller=lambda: (Decimal("3.00"), Decimal("9.47")),
            settlement_clock=FakeSettlementClock(),
        )
    )
    sell = [r for r in spy.submitted if r.side == "SELL"][0]
    assert Decimal(sell.size) == Decimal("3.00")
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION
    assert Decimal(result.report["residual"]["confirmed_sold"]) == Decimal("3.00")
    assert Decimal(result.report["residual"]["confirmed_acquired"]) == Decimal("9.47")


def test_funder_mismatch_and_allowance(tmp_path: Path) -> None:
    r1 = evaluate_sell_readiness(
        confirmed_acquired=Decimal("5"),
        sellable_balance=Decimal("5"),
        allowance=Decimal("5"),
        token_id="t",
        expected_token_id="t",
        funder_ok=False,
        stream_or_rest_healthy=True,
        bid_depth_ok=True,
        tick_min_ok=True,
    )
    assert not r1.ok and "FUNDER_PROXY_MISMATCH" in r1.blockers

    r2 = evaluate_sell_readiness(
        confirmed_acquired=Decimal("5"),
        sellable_balance=Decimal("5"),
        allowance=Decimal("1"),
        token_id="t",
        expected_token_id="t",
        funder_ok=True,
        stream_or_rest_healthy=True,
        bid_depth_ok=True,
        tick_min_ok=True,
    )
    assert not r2.ok and "CONDITIONAL_ALLOWANCE_INSUFFICIENT" in r2.blockers


def test_stream_disconnect_rest_fallback() -> None:
    """Missing stream events: REST poller alone can confirm."""
    clock = FakeSettlementClock()
    r = wait_for_entry_settlement(
        poll_trades=lambda: [_trade(TradeSettlementStatus.CONFIRMED)],
        poll_balance=lambda: (Decimal("9.47"), Decimal("9.47")),
        order_id="0x1",
        planned_qty=Decimal("9.47"),
        clock=clock,
        stream_events=lambda: [],  # disconnected / empty
    )
    assert r.phase is SettlementPhase.ENTRY_CONFIRMED


def test_rest_stream_disagreement_prefers_union() -> None:
    clock = FakeSettlementClock()
    r = wait_for_entry_settlement(
        poll_trades=lambda: [_trade(TradeSettlementStatus.MATCHED, tid="a")],
        poll_balance=lambda: (Decimal("9.47"), Decimal("9.47")),
        order_id="0x1",
        planned_qty=Decimal("9.47"),
        clock=clock,
        stream_events=lambda: [_trade(TradeSettlementStatus.CONFIRMED, tid="a")],
    )
    assert r.confirmed_acquired == Decimal("9.47")


def test_restart_and_manual_flat() -> None:
    assert detect_manual_flat(
        confirmed_acquired=Decimal("9.47"),
        current_balance=Decimal("0"),
        current_position=Decimal("0"),
        sell_trades=[_trade(TradeSettlementStatus.CONFIRMED, side="SELL")],
    )


def test_entry_near_expiry_then_exit(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            simulate_entry_deadline_passed=True,
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    assert len(spy.submitted) == 2


def test_no_sell_storm_and_no_second_buy(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    # Balance never appears → MI, only one BUY, zero SELL
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [_trade(TradeSettlementStatus.MATCHED)],
            settlement_balance_poller=lambda: (Decimal("0"), None),
            settlement_clock=FakeSettlementClock(),
            settlement_max_wait_s=2.0,
        )
    )
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION
    assert len([r for r in spy.submitted if r.side == "BUY"]) == 1
    assert len([r for r in spy.submitted if r.side == "SELL"]) == 0
    residual = result.report["residual"]
    assert "9.47" not in str(residual.get("confirmed_acquired"))
    assert residual["confirmed_acquired"] == "0"
    assert "exposure_high" in residual


def test_matched_alone_does_not_emit_entry_filled_9_47(tmp_path: Path) -> None:
    """Regression: incident path must not treat insert matched as fill."""
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [_trade(TradeSettlementStatus.MATCHED)],
            settlement_balance_poller=lambda: (Decimal("0"), None),
            settlement_clock=FakeSettlementClock(),
            settlement_max_wait_s=1.5,
        )
    )
    facts = (result.facts_path or Path()).read_text(encoding="utf-8")
    assert "entry_matched_not_settled" in facts
    assert "entry_filled" not in facts
    assert "entry_confirmed" not in facts
    # Must not SELL while balance is zero
    assert not any(r.side == "SELL" for r in spy.submitted)
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION


def test_incident_fixture_documents_bug() -> None:
    """Fixture preserves the buggy trail; corrected runtime must diverge."""
    lines = FIXTURE.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert "entry_filled" in events
    assert events.index("entry_filled") < events.index("exit_submit")
    # Gap between entry_submit and entry_filled in fixture is ~5ms — forbidden now


def test_lifecycle_matched_not_active() -> None:
    life = MutationLifecycle()
    life.prepare_approval("a")
    life.accept_approval()
    life.arm()
    life.note_entry_submitting()
    life.note_entry_matched()
    assert life.phase is MutationPhase.ENTRY_MATCHED
    assert not life.can_submit_exit()
    life.note_entry_settling()
    life.note_entry_confirmed(partial=False)
    assert life.can_submit_exit()


def test_residual_uses_confirmed_not_planned() -> None:
    clock = FakeSettlementClock()
    r = wait_for_entry_settlement(
        poll_trades=lambda: [_trade(TradeSettlementStatus.MATCHED)],
        poll_balance=lambda: (Decimal("0"), None),
        order_id="0x1",
        planned_qty=Decimal("9.47"),
        clock=clock,
        config=__import__(
            "tyrex_pm.execution.polymarket.settlement", fromlist=["SettlementWaitConfig"]
        ).SettlementWaitConfig(max_wait_s=1.0, initial_backoff_s=0.5),
    )
    assert r.exhausted
    assert r.confirmed_acquired == Decimal("0")
    assert r.exposure_high == Decimal("9.47")  # range upper, not confirmed residual
    assert r.exposure_low == Decimal("0")
