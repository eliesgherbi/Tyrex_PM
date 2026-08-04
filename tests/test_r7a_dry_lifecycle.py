"""R7A dry mutation lifecycle — spy transport only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.execution.polymarket.approval import build_approval_artifact
from tyrex_pm.execution.polymarket.mutation_transport import (
    MutationArmToken,
    MutationGateError,
    SdkMutationTransport,
    SpyMutationTransport,
)
from tyrex_pm.runtime.r7_dry_lifecycle import run_dry_lifecycle
import pytest

def _artifact():
    now = datetime.now(timezone.utc)
    return build_approval_artifact(
        commit_identity="commit1",
        config_fingerprint="cfg1",
        r7a_report_hash="rep1",
        market_id="m1",
        condition_id="c1",
        instrument_token_id="tok1",
        outcome_side="YES",
        max_buy_notional=Decimal("5.00"),
        limit_price=Decimal("0.50"),
        quantity=Decimal("10"),
        order_type="FAK",
        tick_size="0.01",
        min_order_size="5",
        max_limit_price=Decimal("0.50"),
        entry_deadline=now + timedelta(minutes=10),
        flatten_deadline=now + timedelta(minutes=8),
        max_hold_s=180.0,
        flatten_before_close_s=30.0,
        risk_fingerprint="r1",
        user_stream_ready=True,
        reconciliation_clean=True,
    )


def test_full_fill_and_exit(tmp_path: Path) -> None:
    r = run_dry_lifecycle(
        artifact=_artifact(),
        budget_path=tmp_path / "b.json",
        scenario="full_fill_exit",
        commit_identity="commit1",
        config_fingerprint="cfg1",
    )
    assert r.ok
    assert r.submits == 2  # entry + exit
    assert r.phase == "MUTATIONS_DISABLED"
    assert Decimal(r.budget["filled_buy_notional"]) == Decimal("5.00")


def test_partial_fill_cancel(tmp_path: Path) -> None:
    r = run_dry_lifecycle(
        artifact=_artifact(),
        budget_path=tmp_path / "b.json",
        scenario="partial_fill_cancel",
        commit_identity="commit1",
        config_fingerprint="cfg1",
    )
    assert r.ok
    assert r.cancels == 1
    assert r.submits == 2


def test_rejected_and_unknown(tmp_path: Path) -> None:
    r1 = run_dry_lifecycle(
        artifact=_artifact(),
        budget_path=tmp_path / "b1.json",
        scenario="rejected_entry",
        commit_identity="commit1",
        config_fingerprint="cfg1",
    )
    assert r1.submits == 1
    r2 = run_dry_lifecycle(
        artifact=_artifact(),
        budget_path=tmp_path / "b2.json",
        scenario="unknown_submission",
        commit_identity="commit1",
        config_fingerprint="cfg1",
    )
    assert r2.phase == "UNKNOWN_SUBMISSION"
    assert Decimal(r2.budget["uncertain_buy_notional"]) == Decimal("5.00")


def test_spy_submit_once_and_no_cancel_all() -> None:
    spy = SpyMutationTransport()
    from tyrex_pm.execution.polymarket.transport import SubmitOrderRequest

    spy.submit_order(
        SubmitOrderRequest(token_id="t", side="BUY", price="0.5", size="10")
    )
    assert len(spy.submitted) == 1
    with pytest.raises(MutationGateError, match="EXTERNAL"):
        spy.cancel_order("0xexternal1")


def test_sdk_mutation_requires_arm_token() -> None:
    t = SdkMutationTransport(_client=object())
    from tyrex_pm.execution.polymarket.transport import SubmitOrderRequest

    with pytest.raises(MutationGateError, match="NOT_ARMED"):
        t.submit_order(
            SubmitOrderRequest(token_id="t", side="BUY", price="0.5", size="1")
        )
    token = MutationArmToken(artifact_id="a", allow_network=False)
    with pytest.raises(MutationGateError):
        t.enable_network(token)


def test_sdk_wired_fak_uses_market_order_when_armed() -> None:
    calls: list[str] = []

    class _FakeClient:
        def place_market_order(self, **kwargs):
            calls.append("market")
            return {"success": True, "orderID": "0x1", "status": "matched"}

        def place_limit_order(self, **kwargs):
            calls.append("limit")
            return {"success": True, "orderID": "0x2", "status": "live"}

        def cancel_order(self, *args, **kwargs):
            oid = kwargs.get("order_id") or (args[0] if args else None)
            calls.append(f"cancel:{oid}")
            return {"canceled": [oid]}

    t = SdkMutationTransport(_client=_FakeClient())
    t.enable_network(MutationArmToken(artifact_id="a", allow_network=True))
    from tyrex_pm.execution.polymarket.transport import SubmitOrderRequest

    r = t.submit_order(
        SubmitOrderRequest(
            token_id="tok",
            side="BUY",
            price="0.50",
            size="10",
            amount="5.00",
            order_type="FAK",
            tick_size="0.01",
        )
    )
    assert r.ok and r.venue_order_id == "0x1"
    assert calls == ["market"]
    t.cancel_order("0x1")
    assert "cancel:0x1" in calls
    with pytest.raises(MutationGateError, match="CANCEL_ALL"):
        t.cancel_all()
    t.disable_network()
    with pytest.raises(MutationGateError, match="NOT_ARMED"):
        t.submit_order(
            SubmitOrderRequest(token_id="t", side="BUY", price="0.5", size="1", amount="1")
        )


def test_preflight_cannot_import_mutation_sdk_wire() -> None:
    import tyrex_pm
    from pathlib import Path

    text = (
        Path(tyrex_pm.__file__).parent / "runtime" / "live_preflight.py"
    ).read_text(encoding="utf-8")
    assert "SdkMutationTransport" not in text
    assert "mutation_transport" not in text
    assert "enable-mutations" not in text
