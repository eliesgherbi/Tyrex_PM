"""R7A.2 session envelope, binding, and dry lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.runtime.r7_position_ack import build_acknowledgment
from tyrex_pm.runtime.r7b_session import (
    SessionError,
    SessionPhase,
    SessionRuntimeState,
    build_execution_artifact,
    build_session_authorization,
    future_authorization_wording,
    validate_session_authorization,
)
from tyrex_pm.runtime.r7b_session_dry import run_dry_session_lifecycle


def _ack():
    rows = [
        {
            "conditionId": f"0xc{i}",
            "asset": f"t{i}",
            "outcome": "Up",
            "size": "5",
            "redeemable": True,
            "curPrice": 0,
            "slug": f"btc-updown-5m-{i}",
        }
        for i in range(4)
    ]
    return build_acknowledgment(raw_positions=rows, commit_identity="commit1")


def test_session_envelope_accepted() -> None:
    ack = _ack()
    s = build_session_authorization(
        commit_identity="commit1",
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
    )
    validate_session_authorization(s, commit_identity="commit1")
    assert s.limits["max_buy_collateral_including_fee"] == "5.00"
    assert s.user_authorization_present is False
    assert "ReferenceMomentumStrategy" in future_authorization_wording(s)


def test_session_rejects_over_5_and_pyramid() -> None:
    ack = _ack()
    with pytest.raises(SessionError, match="MAX_BUY"):
        build_session_authorization(
            commit_identity="c",
            acknowledged_position_set_id=ack.acknowledgment_id,
            acknowledged_position_set_fingerprint=ack.set_fingerprint(),
            limits={"max_buy_collateral_including_fee": "5.01"},
        )
    with pytest.raises(SessionError, match="PYRAMIDING"):
        build_session_authorization(
            commit_identity="c",
            acknowledged_position_set_id=ack.acknowledgment_id,
            acknowledged_position_set_fingerprint=ack.set_fingerprint(),
            limits={"pyramiding": True},
        )


def test_expired_and_wrong_commit_rejected() -> None:
    ack = _ack()
    s = build_session_authorization(
        commit_identity="commit1",
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
        lifetime_s=1,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(SessionError, match="EXPIRED"):
        validate_session_authorization(
            s,
            commit_identity="commit1",
            now=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        )
    s2 = build_session_authorization(
        commit_identity="commit1",
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
    )
    with pytest.raises(SessionError, match="COMMIT"):
        validate_session_authorization(s2, commit_identity="other")


def test_bind_one_market_no_switch() -> None:
    ack = _ack()
    s = build_session_authorization(
        commit_identity="c",
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
    )
    rt = SessionRuntimeState()
    rt.attach(s)
    rt.observe_window("btc-updown-5m-1", eligible=False)
    rt.observe_window("btc-updown-5m-2", eligible=True)
    rt.bind_market("btc-updown-5m-2")
    with pytest.raises(SessionError):
        rt.bind_market("btc-updown-5m-3")


def test_max_windows_enforced() -> None:
    ack = _ack()
    s = build_session_authorization(
        commit_identity="c",
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
    )
    rt = SessionRuntimeState()
    rt.attach(s)
    with pytest.raises(SessionError, match="MAX_WINDOWS"):
        for i in range(4):
            rt.observe_window(f"btc-updown-5m-{i}", eligible=False)


def test_consume_on_submit_blocks_second_market(tmp_path: Path) -> None:
    ack = _ack()
    r = run_dry_session_lifecycle(
        ack=ack,
        budget_path=tmp_path / "b.json",
        commit_identity="commit1",
        scenario="full_bind_fill_exit",
    )
    assert r.ok
    assert r.phase == SessionPhase.COMPLETED_FLAT.value
    assert r.state["session_consumed"] is True
    assert r.submits >= 2


def test_dry_uncertain_and_max_windows(tmp_path: Path) -> None:
    ack = _ack()
    r1 = run_dry_session_lifecycle(
        ack=ack,
        budget_path=tmp_path / "u.json",
        commit_identity="commit1",
        scenario="uncertain_submit",
    )
    assert r1.ok
    r2 = run_dry_session_lifecycle(
        ack=ack,
        budget_path=tmp_path / "m.json",
        commit_identity="commit1",
        scenario="max_windows",
    )
    assert r2.ok


def test_artifact_collateral_cap() -> None:
    ack = _ack()
    s = build_session_authorization(
        commit_identity="c",
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
    )
    with pytest.raises(SessionError, match="COLLATERAL"):
        build_execution_artifact(
            session=s,
            market_slug="btc-updown-5m-1",
            condition_id="c",
            yes_token_id="y",
            no_token_id="n",
            selected_outcome="YES",
            selected_token_id="y",
            signal_id="s",
            decision_id="d",
            market_start="t0",
            market_end="t1",
            entry_deadline="t2",
            flatten_deadline="t3",
            best_ask=Decimal("0.5"),
            worst_entry_price=Decimal("0.5"),
            buy_amount=Decimal("5"),
            max_entry_fee=Decimal("0.2"),
            max_entry_collateral=Decimal("5.20"),
            max_estimated_shares=Decimal("10"),
            visible_depth=Decimal("10"),
            tick_size="0.01",
            min_order_size="5",
            fee_rate="0.07",
            fee_exponent="1",
            user_stream_ready=True,
            reconciliation_fingerprint="r",
            balance_allowance_ready=True,
            kill_switch_active=False,
            live_budget_unused=True,
        )


def test_replay_consumed_session() -> None:
    ack = _ack()
    s = build_session_authorization(
        commit_identity="c",
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
    )
    s = type(s)(**{**s.__dict__, "consumed": True})
    with pytest.raises(SessionError, match="CONSUMED"):
        validate_session_authorization(s, commit_identity="c")
