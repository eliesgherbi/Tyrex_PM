"""Settlement lifecycle: durable ledger, trial limits, constraints, resolution, redemption."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.execution.settlement.chain import (
    decode_payout_redemption,
    payout_redemption_topic,
    receipt_redemption_verifier,
)
from tyrex_pm.execution.settlement.constraints import parse_fee_schedule, verify_constraints
from tyrex_pm.execution.settlement.governor import TrialLimits, check_entry
from tyrex_pm.execution.settlement.ledger import EpisodeState, LedgerError, SettlementLedger
from tyrex_pm.execution.settlement.resolution import (
    PayoutObservation,
    RedemptionEvidence,
    ResolutionManager,
)

S = EpisodeState
COND = "0x" + "ab" * 32
WALLET = "0x" + "11" * 20
LIMITS = TrialLimits(
    max_debit_per_episode=Decimal("5"),
    max_episodes=3,
    max_unresolved_count=2,
    max_unresolved_cost=Decimal("10"),
    trial_loss_budget=Decimal("12"),
)


def reserve(ledger: SettlementLedger, n: int, debit="5", fees="0.1") -> str:
    episode = f"ep{n}"
    ledger.record(
        "EPISODE_RESERVED", episode, window_id=f"w{n}", market_id="m", condition_id=COND,
        token_id=f"t{n}", outcome_index=0, max_debit=debit, max_fees=fees,
    )
    return episode


def hold(ledger: SettlementLedger, episode: str, shares="10", cost="4") -> None:
    ledger.record("FILL_CONFIRMED", episode, net_shares=shares, cost_basis=cost)
    ledger.record("HOLD_REQUESTED", episode, quantity=shares)
    ledger.record("HOLD_COMMITTED", episode, quantity=shares)


def test_reservation_becomes_spent_capital_exactly_once(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    ep = reserve(ledger, 1)
    totals = ledger.totals()
    assert totals.reserved_cash == Decimal("5.1") and totals.spent_unresolved == 0
    hold(ledger, ep)
    totals = ledger.totals()
    assert totals.reserved_cash == 0 and totals.spent_unresolved == Decimal("4")
    assert totals.open_worst_case_loss == Decimal("4")  # not 4 + 5.1


def test_illegal_transitions_duplicates_and_conflicts(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    ep = reserve(ledger, 1)
    with pytest.raises(LedgerError):
        ledger.record("HOLD_COMMITTED", ep, quantity="1")  # not filled / requested
    with pytest.raises(LedgerError, match="already has an episode"):
        ledger.record("EPISODE_RESERVED", "other", window_id="w1", market_id="m",
                      condition_id=COND, token_id="t", outcome_index=0)
    ledger.record("FILL_CONFIRMED", ep, net_shares="10", cost_basis="4")
    ledger.record("FILL_CONFIRMED", ep, net_shares="10", cost_basis="4")  # idempotent
    with pytest.raises(LedgerError, match="conflicting"):
        ledger.record("FILL_CONFIRMED", ep, net_shares="11", cost_basis="4")


def test_recovery_replays_state_and_skips_only_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    ledger = SettlementLedger(path)
    hold(ledger, reserve(ledger, 1))
    with path.open("ab") as handle:
        handle.write(b'{"event": "PAYOUT_PRO')  # crash mid-append
    recovered = SettlementLedger(path)
    assert recovered.torn_tail_skipped
    assert recovered.episodes["ep1"].state is S.HOLD_COMMITTED
    lines = path.read_bytes().split(b"\n")
    lines.insert(1, b"garbage")
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(LedgerError, match="unreadable"):
        SettlementLedger(path)


def test_budget_includes_pending_unresolved_realized_and_proposed(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    hold(ledger, reserve(ledger, 1), cost="4")           # unresolved: 4
    reserve(ledger, 2, debit="5", fees="0.1")              # pending: 5.1
    check = check_entry(ledger, LIMITS, window_id="w3", proposed_debit=Decimal("3"),
                        proposed_fees=Decimal("0.2"))
    # 4 + 5.1 + 3.2 = 12.3 > 12 budget; unresolved cost 12.3 > 10; count 2+1 > 2
    assert not check.allowed
    assert {"TRIAL_LOSS_BUDGET", "MAX_UNRESOLVED_COST", "MAX_UNRESOLVED_COUNT"} <= set(
        check.reasons
    )
    assert check.loss_budget_used_after == Decimal("12.3")
    duplicate = check_entry(ledger, LIMITS, window_id="w1", proposed_debit=Decimal("1"),
                            proposed_fees=Decimal("0"))
    assert "EPISODE_ALREADY_EXISTS_FOR_WINDOW" in duplicate.reasons


def test_proven_payout_reduces_worst_case_and_manual_blocks(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    ep = reserve(ledger, 1)
    hold(ledger, ep, shares="10", cost="4")
    ledger.record("PAYOUT_PROVEN", ep, payout_per_share="1")
    assert ledger.totals().open_worst_case_loss == 0 and ledger.totals().receivable == 10
    ep2 = reserve(ledger, 2)
    ledger.record("MANUAL_INTERVENTION", ep2, reason="test")
    check = check_entry(ledger, LIMITS, window_id="w9", proposed_debit=Decimal("1"),
                        proposed_fees=Decimal("0"))
    assert "SETTLEMENT_MANUAL_INTERVENTION_OPEN" in check.reasons
    assert ledger.totals().open_worst_case_loss == Decimal("5.1")


def test_trial_limits_keep_tiny_live_cap() -> None:
    with pytest.raises(ValueError, match="tiny-live"):
        TrialLimits(max_debit_per_episode=Decimal("6"), max_episodes=1, max_unresolved_count=1,
                    max_unresolved_cost=Decimal("1"), trial_loss_budget=Decimal("1"))


def test_constraints_require_verified_fees_tick_and_minimum() -> None:
    curve = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))
    gamma = {"exponent": 1, "rate": "0.07", "rebateRate": "0.2", "takerOnly": True}
    ok = verify_constraints(fee_schedule=gamma, strategy_fee_curve=curve,
                            tick_size=Decimal("0.01"), min_order_size=Decimal("5"), fee_source="t")
    assert ok.ok
    assert parse_fee_schedule({"e": 1, "r": "0.07", "to": True})[0] == curve
    bad = verify_constraints(fee_schedule=None, strategy_fee_curve=curve, tick_size=None,
                             min_order_size=None, fee_source="t")
    assert set(bad.reasons) == {
        "FEE_SCHEDULE_MISSING", "TICK_SIZE_UNKNOWN", "MIN_ORDER_SIZE_UNKNOWN"
    }
    other = verify_constraints(fee_schedule={"r": "0.10", "e": 1, "to": True},
                               strategy_fee_curve=curve, tick_size=Decimal("0.01"),
                               min_order_size=Decimal("5"), fee_source="t")
    assert other.reasons == ("STRATEGY_FEE_CURVE_MISMATCH",)
    unsupported = parse_fee_schedule({"r": "0.07", "e": "1.5", "to": True})
    assert unsupported[1] == "FEE_SCHEDULE_UNSUPPORTED"
    assert parse_fee_schedule({"r": "0.07", "e": 1})[1] == "FEE_SCHEDULE_NOT_TAKER_ONLY"


def _manager(ledger, payout, evidence=None):
    async def reader(condition_id, outcome_index):
        return payout

    async def verifier(tx, episode, wallet):
        return evidence

    return ResolutionManager(ledger, payout_reader=reader, redemption_verifier=verifier,
                             wallet=WALLET)


def test_resolution_requires_finalized_payout_and_completes_zero_payout(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    ep = reserve(ledger, 1)
    hold(ledger, ep)
    report = asyncio.run(_manager(ledger, PayoutObservation(False, None, "ctf", "unresolved"))
                         .reconcile())
    assert ep in report.uncertain and ledger.episodes[ep].state is S.RESOLUTION_UNCERTAIN
    asyncio.run(_manager(ledger, PayoutObservation(False, None, "ctf", "unresolved")).reconcile())
    assert ledger.episodes[ep].history.count("RESOLUTION_UNCERTAIN") == 1  # deduplicated
    asyncio.run(_manager(ledger, PayoutObservation(True, Decimal(0), "ctf")).reconcile())
    assert ledger.episodes[ep].state is S.COMPLETED
    assert ledger.totals().realized_pnl == Decimal("-4")


def test_redemption_needs_attributable_finalized_matching_credit(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    ep = reserve(ledger, 1)
    hold(ledger, ep, shares="10", cost="4")
    manager = _manager(ledger, PayoutObservation(True, Decimal(1), "ctf"))
    asyncio.run(manager.reconcile())
    assert ledger.episodes[ep].state is S.REDEMPTION_PENDING_OPERATOR
    assert manager.worklist()[0]["expected_credit"] == "10"
    unrelated = RedemptionEvidence(
        False, True, Decimal("10"), ("NO_MATCHING_REDEMPTION_LOG",), "0x"
    )
    ok, reasons = asyncio.run(_manager(ledger, None, unrelated).confirm_redemption(ep, "0x"))
    assert not ok and "REDEMPTION_NOT_ATTRIBUTABLE" in reasons
    short = RedemptionEvidence(True, True, Decimal("9"), (), "0x")
    ok, reasons = asyncio.run(_manager(ledger, None, short).confirm_redemption(ep, "0x"))
    assert not ok and reasons == ("CREDIT_AMOUNT_MISMATCH",)
    assert ledger.totals().receivable == Decimal("10")  # still not cash
    good = RedemptionEvidence(True, True, Decimal("10"), (), "0xabc", block_number=7)
    ok, _ = asyncio.run(_manager(ledger, None, good).confirm_redemption(ep, "0xabc"))
    assert ok and ledger.episodes[ep].state is S.COMPLETED
    assert ledger.totals().realized_pnl == Decimal("6") and ledger.totals().receivable == 0


def _redemption_log(wallet: str, condition: str, index_sets, payout: int, collateral: str):
    def pad(hex_no_prefix: str) -> str:
        return hex_no_prefix.rjust(64, "0")

    data = pad(condition[2:]) + pad(hex(96)[2:]) + pad(hex(payout)[2:])
    data += pad(hex(len(index_sets))[2:])
    data += "".join(pad(hex(i)[2:]) for i in index_sets)
    return {
        "address": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "topics": [payout_redemption_topic(), "0x" + pad(wallet[2:]), "0x" + pad(collateral[2:]),
                   "0x" + "00" * 32],
        "data": "0x" + data,
    }


def test_receipt_verifier_attributes_only_our_redemption() -> None:
    pytest.importorskip("eth_utils")
    from tyrex_pm.execution.settlement.chain import DEFAULT_COLLATERAL
    from tyrex_pm.execution.settlement.ledger import Episode

    episode = Episode(episode_id="e", window_id="w", market_id="m", condition_id=COND,
                      token_id="t", outcome_index=0)
    log = _redemption_log(WALLET, COND, [1, 2], 10_000_000, DEFAULT_COLLATERAL)
    assert decode_payout_redemption(log)["index_sets"] == [1, 2]

    class Reader:
        def __init__(self, logs, status="0x1"):
            self.logs, self.status = logs, status

        async def receipt(self, tx):
            return {"status": self.status, "blockNumber": "0x10", "logs": self.logs}

        async def finalized_block_number(self):
            return 0x20

    ok = asyncio.run(receipt_redemption_verifier(Reader([log]))("0x1", episode, WALLET))
    assert ok.attributable and ok.finalized and ok.credited_amount == Decimal("10")
    stranger = _redemption_log("0x" + "22" * 20, COND, [1, 2], 10_000_000, DEFAULT_COLLATERAL)
    no = asyncio.run(receipt_redemption_verifier(Reader([stranger]))("0x1", episode, WALLET))
    assert not no.attributable and "NO_MATCHING_REDEMPTION_LOG" in no.reasons
    failed = asyncio.run(receipt_redemption_verifier(Reader([log], "0x0"))("0x1", episode, WALLET))
    assert not failed.attributable and "TRANSACTION_FAILED" in failed.reasons
