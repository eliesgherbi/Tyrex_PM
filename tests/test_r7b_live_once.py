"""Deterministic R7B operator CLI safety tests (no network mutations)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tyrex_pm.execution.polymarket.fees_fd import FeeDescriptor
from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from tyrex_pm.execution.polymarket.order_sizing import size_buy_under_cap
from tyrex_pm.execution.polymarket.settlement import (
    FakeSettlementClock,
    TradeEvidence,
    TradeSettlementStatus,
)
from tyrex_pm.runtime.r7_position_ack import build_acknowledgment
from tyrex_pm.runtime.r7b_live_once import (
    LiveOnceError,
    R7BLiveOnceArgs,
    TerminalOutcome,
    run_r7b_live_once,
)


def _immediate_settlement(qty: Decimal = Decimal("9.09")) -> dict[str, Any]:
    """Injected pollers: CONFIRMED trade + sellable balance on first poll."""
    trades = [
        TradeEvidence(
            trade_id="t1",
            order_id="0xspy0001",
            side="BUY",
            size=qty,
            price=Decimal("0.55"),
            status=TradeSettlementStatus.CONFIRMED,
        )
    ]
    return {
        "settlement_trade_poller": lambda: list(trades),
        "settlement_balance_poller": lambda: (qty, qty),
        "settlement_clock": FakeSettlementClock(),
        "settlement_max_wait_s": 5.0,
    }


class RaisingTransport:
    """Any call means dry/default path incorrectly mutated."""

    def submit_order(self, request: Any) -> Any:
        raise AssertionError("MUTATION_TRANSPORT_CALLED")

    def cancel_order(self, venue_order_id: str) -> Any:
        raise AssertionError("MUTATION_TRANSPORT_CALLED")

    def cancel_all(self) -> None:
        raise AssertionError("MUTATION_TRANSPORT_CALLED")


def _fee() -> FeeDescriptor:
    return FeeDescriptor(
        fee_rate=Decimal("0.25"),
        exponent=Decimal("2"),
        taker_only=True,
        source="test",
        condition_id="0xcond",
    )


def _eligible_window(
    *,
    slug: str = "btc-updown-5m-2000000000",
    yes: str = "tok_yes_live",
    no: str = "tok_no_live",
    condition_id: str = "0xcond_live",
) -> dict[str, Any]:
    now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    fee = _fee()
    sized = size_buy_under_cap(
        best_ask=Decimal("0.55"),
        ask_size=Decimal("100"),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        max_buy_notional=Decimal("5.00"),
        max_limit_price=Decimal("0.55"),
        fee=fee,
    )
    return {
        "slug": slug,
        "eligible": True,
        "yes_token_id": yes,
        "no_token_id": no,
        "condition_id": condition_id,
        "fee": fee,
        "tick_size": "0.01",
        "min_order_size": "5",
        "best_ask": Decimal("0.55"),
        "ask_size": Decimal("100"),
        "sized": sized,
        "title": "BTC Up/Down test",
        "market_accepting": True,
        "deadlines": SimpleNamespace(
            market_start=now,
            market_end=now + timedelta(seconds=300),
            entry_deadline=now + timedelta(seconds=200),
            flatten_deadline=now + timedelta(seconds=270),
        ),
    }


def _git_ok(_repo: Path) -> tuple[str, str, bool]:
    return "rest_project", "abc123deadbeef", True


def _ack_rows() -> list[dict[str, Any]]:
    return [
        {
            "conditionId": f"0xack{i}",
            "asset": f"ack_tok_{i}",
            "outcome": "Up",
            "size": "5",
            "redeemable": True,
            "curPrice": 0,
            "slug": f"old-{i}",
        }
        for i in range(4)
    ]


def _write_ack(tmp_path: Path) -> tuple[Path, list[dict[str, Any]]]:
    from tyrex_pm.runtime.r7_position_ack import build_acknowledgment, write_acknowledgment

    rows = _ack_rows()
    ack = build_acknowledgment(raw_positions=rows, commit_identity="abc123deadbeef")
    path = tmp_path / "state" / "r7" / "position_acknowledgment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_acknowledgment(path, ack)
    return path, rows


def _base_args(tmp_path: Path, **kwargs: Any) -> R7BLiveOnceArgs:
    window = _eligible_window()
    ack_path, ack_rows = _write_ack(tmp_path)
    defaults: dict[str, Any] = {
        "output_dir": tmp_path / "out",
        "acknowledgment_path": ack_path,
        "repo_root": tmp_path,
        "allow_dirty_worktree": True,
        "git_identity_provider": _git_ok,
        "window_provider": lambda: [window],
        "positions_provider": lambda: list(ack_rows),
        "skip_network": True,
        "forced_outcome": "YES",
        "require_acknowledgment": True,
    }
    # Happy-path live tests need settlement injectors (no network)
    if kwargs.get("execute_live") and "settlement_trade_poller" not in kwargs:
        defaults.update(_immediate_settlement(qty=window["sized"].quantity))
    defaults.update(kwargs)
    return R7BLiveOnceArgs(**defaults)


def test_dry_mode_cannot_call_mutation_transport(tmp_path: Path) -> None:
    transport = RaisingTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            dry_run=True,
            execute_live=False,
            mutation_transport=transport,
        )
    )
    assert result.outcome is TerminalOutcome.DRY_OK
    assert result.exit_code == 0
    assert result.report["mutations_attempted"] == []
    assert result.report["mutations_enabled"] is False


def test_missing_execute_live_cannot_mutate(tmp_path: Path) -> None:
    transport = RaisingTransport()
    result = run_r7b_live_once(
        _base_args(tmp_path, dry_run=False, execute_live=False, mutation_transport=transport)
    )
    assert result.report["dry_run"] is True
    assert result.outcome is TerminalOutcome.DRY_OK
    assert result.report["mutations_attempted"] == []


def test_exactly_one_entry_per_process(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(tmp_path, execute_live=True, mutation_transport=spy)
    )
    assert result.outcome is TerminalOutcome.FLAT
    buys = [r for r in spy.submitted if r.side.upper() == "BUY"]
    sells = [r for r in spy.submitted if r.side.upper() == "SELL"]
    assert len(buys) == 1
    assert len(sells) == 1
    assert result.report["second_entry_denied"] is True


def test_cumulative_fee_inclusive_budget_never_exceeds_5(tmp_path: Path) -> None:
    import json

    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            max_buy_collateral=Decimal("5.00"),
        )
    )
    assert result.ok
    pre = result.report["pre_submit"]
    assert Decimal(pre["max_collateral"]) <= Decimal("5.00")
    assert Decimal(pre["buy_amount"]) + Decimal(pre["estimated_max_entry_fee"]) <= Decimal(
        "5.00"
    )
    budget_files = list((tmp_path / "out").glob("budget_*.json"))
    assert budget_files
    budget = json.loads(budget_files[0].read_text(encoding="utf-8"))
    filled = Decimal(budget["filled_buy_notional"])
    working = Decimal(budget["working_buy_notional"])
    uncertain = Decimal(budget["uncertain_buy_notional"])
    assert filled + working + uncertain <= Decimal("5.00")


def test_no_market_switching(tmp_path: Path) -> None:
    windows = [
        _eligible_window(slug="btc-updown-5m-2000000000"),
        _eligible_window(slug="btc-updown-5m-2000000300", condition_id="0xother"),
    ]
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            dry_run=True,
            window_provider=lambda: windows,
        )
    )
    assert result.outcome is TerminalOutcome.DRY_OK
    assert result.report["market_switch_blocked"] is True
    assert result.report["pre_submit"]["market_slug"] == "btc-updown-5m-2000000000"


def test_no_acknowledged_position_action(tmp_path: Path) -> None:
    import json

    ack_rows = [
        {
            "conditionId": f"0xack{i}",
            "asset": f"ack_tok_{i}",
            "outcome": "Up",
            "size": "5",
            "redeemable": True,
            "curPrice": 0,
            "slug": f"old-{i}",
        }
        for i in range(4)
    ]
    ack = build_acknowledgment(raw_positions=ack_rows, commit_identity="abc123deadbeef")
    ack_path = tmp_path / "ack.json"
    ack_path.write_text(json.dumps(ack.to_dict(), indent=2), encoding="utf-8")
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            acknowledgment_path=ack_path,
            positions_provider=lambda: ack_rows,
        )
    )
    assert result.outcome is TerminalOutcome.FLAT
    for req in spy.submitted:
        assert not req.token_id.startswith("ack_tok_")
    assert result.report.get("acknowledgment", {}).get("untouched") is True


def test_unknown_submission_cannot_resubmit(tmp_path: Path) -> None:
    spy = SpyMutationTransport(submit_behavior="timeout")
    result = run_r7b_live_once(
        _base_args(tmp_path, execute_live=True, mutation_transport=spy)
    )
    assert result.outcome is TerminalOutcome.MANUAL_INTERVENTION
    assert result.exit_code == 3
    assert len(spy.submitted) == 1
    assert result.report["second_entry_denied"] is True
    residual = result.report["residual"]
    assert Decimal(residual["uncertain_buy_notional"]) > 0


def test_entry_before_expiry_still_reconciles_and_flattens(tmp_path: Path) -> None:
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
    text = (result.facts_path or Path()).read_text(encoding="utf-8")
    assert "entry_deadline_passed" in text


def test_presubmit_failure_each_check(tmp_path: Path) -> None:
    import json

    checks = [
        ("branch_ok", False, "BRANCH_MISMATCH"),
        ("worktree_ok", False, "DIRTY_WORKTREE"),
        ("ack_ok", False, "ACKNOWLEDGMENT_INVALID"),
        ("reconciliation_ok", False, "RECONCILIATION_NOT_READY"),
        ("user_stream_ready", False, "USER_STREAM_NOT_READY"),
        ("selected_market_flat", False, "SELECTED_MARKET_POSITION_NONZERO"),
        ("open_orders_selected", 1, "SELECTED_MARKET_OPEN_ORDER"),
        ("market_accepting", False, "MARKET_NOT_ACCEPTING_ORDERS"),
        ("deadlines_ok", False, "INSUFFICIENT_TIME_REMAINING"),
        ("book_ok", False, "BOOK_NOT_READY"),
        ("tick_min_ok", False, "TICK_OR_MIN_SIZE_INVALID"),
        ("fee_ok", False, "FEE_PARAMETERS_UNKNOWN"),
        ("balance_ok", False, "BALANCE_UNKNOWN"),
        ("kill_switch_active", True, "KILL_SWITCH_ACTIVE"),
        ("budget_ok", False, "BUDGET_INVALID"),
    ]
    for key, value, code in checks:
        ack_path = None
        positions: list = []
        if key == "ack_ok":
            rows = [
                {
                    "conditionId": f"0xa{i}",
                    "asset": f"t{i}",
                    "outcome": "Up",
                    "size": "1",
                    "redeemable": True,
                    "curPrice": 0,
                    "slug": f"s{i}",
                }
                for i in range(4)
            ]
            ack = build_acknowledgment(raw_positions=rows, commit_identity="abc123deadbeef")
            ack_path = tmp_path / f"ack_{key}.json"
            ack_path.write_text(json.dumps(ack.to_dict()), encoding="utf-8")
            positions = rows

        spy = RaisingTransport()
        kwargs: dict = {
            "execute_live": True,
            "mutation_transport": spy,
            "readiness_overrides": {key: value},
            "allow_dirty_worktree": True,
        }
        if key == "ack_ok":
            kwargs["acknowledgment_path"] = ack_path
            kwargs["positions_provider"] = lambda p=positions: p
        result = run_r7b_live_once(_base_args(tmp_path, **kwargs))
        assert result.outcome is TerminalOutcome.BLOCKED, key
        assert result.exit_code == 2, key
        assert code in result.report["blockers"], (key, result.report["blockers"])
        assert result.report["mutations_attempted"] == []


def test_terminal_flat_and_manual_intervention(tmp_path: Path) -> None:
    spy_ok = SpyMutationTransport()
    flat = run_r7b_live_once(
        _base_args(tmp_path, execute_live=True, mutation_transport=spy_ok)
    )
    assert flat.outcome is TerminalOutcome.FLAT
    assert flat.exit_code == 0

    class PartialSpy(SpyMutationTransport):
        def submit_order(self, request):  # type: ignore[no-untyped-def]
            if request.side.upper() == "SELL":
                self.submit_behavior = "reject"
            return super().submit_order(request)

    mi = run_r7b_live_once(
        _base_args(tmp_path, execute_live=True, mutation_transport=PartialSpy())
    )
    assert mi.outcome is TerminalOutcome.MANUAL_INTERVENTION
    assert mi.exit_code == 3
    assert "residual" in mi.report


def test_max_buy_collateral_over_5_rejected(tmp_path: Path) -> None:
    with pytest.raises(LiveOnceError, match="MAX_BUY_COLLATERAL"):
        run_r7b_live_once(
            _base_args(
                tmp_path,
                max_buy_collateral=Decimal("5.01"),
                dry_run=True,
            )
        )


def test_wrong_strategy_rejected(tmp_path: Path) -> None:
    with pytest.raises(LiveOnceError, match="STRATEGY_NOT_ALLOWED"):
        run_r7b_live_once(_base_args(tmp_path, strategy="other", dry_run=True))


def test_selected_token_is_acknowledged_blocks(tmp_path: Path) -> None:
    import json

    rows = [
        {
            "conditionId": "0xcond_live",
            "asset": "tok_yes_live",
            "outcome": "Up",
            "size": "5",
            "redeemable": True,
            "curPrice": 0,
            "slug": "btc-updown-5m-2000000000",
        }
    ] + [
        {
            "conditionId": f"0xpad{i}",
            "asset": f"pad{i}",
            "outcome": "Up",
            "size": "1",
            "redeemable": True,
            "curPrice": 0,
            "slug": f"pad-{i}",
        }
        for i in range(3)
    ]
    ack = build_acknowledgment(raw_positions=rows, commit_identity="abc123deadbeef")
    ack_path = tmp_path / "ack_sel.json"
    ack_path.write_text(json.dumps(ack.to_dict()), encoding="utf-8")
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            dry_run=True,
            acknowledgment_path=ack_path,
            positions_provider=lambda: rows,
        )
    )
    assert result.outcome is TerminalOutcome.BLOCKED
    assert (
        "SELECTED_TOKEN_IS_ACKNOWLEDGED" in result.report["blockers"]
        or "SELECTED_MARKET_POSITION_NONZERO" in result.report["blockers"]
    )
