"""R7C.1 acceptance: CONFIRMED-only inventory, dust, ack fail-closed, address roles."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.execution.polymarket.address_roles import (
    AddressRoleError,
    AddressRoles,
)
from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from tyrex_pm.execution.polymarket.settlement import (
    FakeSettlementClock,
    FlatClassification,
    SettlementPhase,
    SettlementWaitConfig,
    TradeEvidence,
    TradeSettlementStatus,
    classify_flatness,
    inventory_from_trades,
    wait_for_entry_settlement,
)
from tyrex_pm.runtime.r7_position_ack import (
    build_acknowledgment,
    validate_acknowledgment_against_inventory,
)
from tyrex_pm.runtime.r7b_live_once import TerminalOutcome, run_r7b_live_once
from test_r7b_live_once import _base_args


def _t(status: TradeSettlementStatus, size: Decimal = Decimal("9.47")) -> TradeEvidence:
    return TradeEvidence(
        trade_id="t1",
        order_id="0xord",
        side="BUY",
        size=size,
        price=Decimal("0.51"),
        status=status,
    )


def test_matched_cannot_create_inventory_or_sell() -> None:
    qty, _, uncertain = inventory_from_trades([_t(TradeSettlementStatus.MATCHED)])
    assert qty == 0 and uncertain


def test_mined_cannot_create_inventory_or_sell(tmp_path: Path) -> None:
    qty, _, uncertain = inventory_from_trades([_t(TradeSettlementStatus.MINED)])
    assert qty == 0 and uncertain

    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [_t(TradeSettlementStatus.MINED)],
            settlement_balance_poller=lambda: (Decimal("9.47"), Decimal("9.47")),
            settlement_clock=FakeSettlementClock(),
            settlement_max_wait_s=2.0,
        )
    )
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION
    assert not any(r.side == "SELL" for r in spy.submitted)
    assert "MINED_NONTERMINAL" in (result.facts_path or Path()).read_text(encoding="utf-8")


def test_confirmed_balance_zero_waits_no_sell(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [_t(TradeSettlementStatus.CONFIRMED)],
            settlement_balance_poller=lambda: (Decimal("0"), None),
            settlement_clock=FakeSettlementClock(),
            settlement_max_wait_s=1.5,
        )
    )
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION
    assert not any(r.side == "SELL" for r in spy.submitted)


def test_confirmed_with_balance_can_sell(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [_t(TradeSettlementStatus.CONFIRMED)],
            settlement_balance_poller=lambda: (Decimal("9.47"), Decimal("9.47")),
            settlement_clock=FakeSettlementClock(),
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    assert any(r.side == "SELL" for r in spy.submitted)


def test_flat_dust_residual_unknown_classifications() -> None:
    assert (
        classify_flatness(conditional_balance=Decimal("0"), balance_known=True)[
            "classification"
        ]
        == FlatClassification.FLAT.value
    )
    dust = classify_flatness(
        conditional_balance=Decimal("0.000587"),
        balance_known=True,
        min_tradable=Decimal("0.01"),
        mark_price=Decimal("0.5"),
    )
    assert dust["classification"] == FlatClassification.FLAT_WITH_DUST.value
    assert dust["can_auto_sell"] is False
    assert dust["dust_cleanup_authorized"] is False
    assert "redeem" in dust["prohibited"]

    residual = classify_flatness(
        conditional_balance=Decimal("1.5"), balance_known=True
    )
    assert residual["classification"] == FlatClassification.RESIDUAL_EXPOSURE.value

    unk = classify_flatness(conditional_balance=None, balance_known=False)
    assert unk["classification"] == FlatClassification.UNKNOWN.value


def _four() -> list[dict]:
    return [
        {
            "conditionId": f"0xc{i}",
            "asset": f"t{i}",
            "outcome": "Up",
            "size": "5",
            "redeemable": True,
            "curPrice": 0,
            "slug": f"s{i}",
        }
        for i in range(4)
    ]


def test_ack_incomplete_row_fails_closed() -> None:
    ack = build_acknowledgment(raw_positions=_four(), commit_identity="c")
    incomplete = [
        {"conditionId": "0xc0", "asset": "t0", "size": "5"},  # missing redeemable
        *_four()[1:],
    ]
    v = validate_acknowledgment_against_inventory(ack, raw_positions=incomplete)
    assert not v.ok
    assert "ACK_INVENTORY_ROW_INCOMPLETE" in v.blockers


def test_ack_reordered_rows_ok() -> None:
    rows = _four()
    ack = build_acknowledgment(raw_positions=rows, commit_identity="c")
    reordered = list(reversed(rows))
    v = validate_acknowledgment_against_inventory(ack, raw_positions=reordered)
    assert v.ok and v.matched == 4


def test_ack_duplicate_identity_fails() -> None:
    ack = build_acknowledgment(raw_positions=_four(), commit_identity="c")
    rows = _four() + [_four()[0]]
    v = validate_acknowledgment_against_inventory(ack, raw_positions=rows)
    assert not v.ok
    assert "ACK_DUPLICATE_IDENTITY" in v.blockers


def test_ack_source_disagreement_fails_closed() -> None:
    ack = build_acknowledgment(raw_positions=_four(), commit_identity="c")
    primary = _four()
    secondary = _four()
    secondary[0]["size"] = "99"
    v = validate_acknowledgment_against_inventory(
        ack,
        raw_positions=primary,
        secondary_raw_positions=secondary,
    )
    assert not v.ok
    assert "ACK_SOURCE_DISAGREEMENT" in v.blockers


def test_signer_funder_proxy_discipline() -> None:
    roles = AddressRoles(
        signer_eoa="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        funder_proxy="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        signature_type=1,
        positions_wallet="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    assert roles.proxy_mode
    assert roles.conditional_owner.lower().startswith("0xbbbb")
    with pytest.raises(AddressRoleError, match="REFUSING_SIGNER"):
        roles.assert_conditional_query_target(
            queried_as="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )


def test_execute_live_requires_clean_worktree(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            allow_dirty_worktree=True,  # must be ignored for live
            git_identity_provider=lambda _r: ("rest_project", "deadbeef", False),
        )
    )
    assert result.outcome is TerminalOutcome.BLOCKED
    assert "DIRTY_WORKTREE" in result.report["blockers"]
    assert spy.submitted == []


def test_manual_external_flatten_with_dust_classification() -> None:
    dust = classify_flatness(
        conditional_balance=Decimal("0.000587"),
        balance_known=True,
        mark_price=Decimal("0.5"),
    )
    assert dust["classification"] == FlatClassification.FLAT_WITH_DUST.value
    assert dust["balance"] == "0.000587"
    assert dust["economic_value"] is not None


def test_mined_wait_never_confirms_with_balance() -> None:
    clock = FakeSettlementClock()
    r = wait_for_entry_settlement(
        poll_trades=lambda: [_t(TradeSettlementStatus.MINED)],
        poll_balance=lambda: (Decimal("9.47"), Decimal("9.47")),
        order_id="0x1",
        planned_qty=Decimal("9.47"),
        clock=clock,
        config=SettlementWaitConfig(max_wait_s=1.0, initial_backoff_s=0.5),
    )
    assert r.phase is SettlementPhase.MANUAL_INTERVENTION
    assert r.confirmed_acquired == Decimal("0")
    assert r.exhausted
