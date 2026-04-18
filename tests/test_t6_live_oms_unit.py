"""T6: OMS single-writer submit+cancel (mock bridge); live CLOB env-gated smoke."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId, VenueOrderId
from tyrex_pm.core.models import ApprovedCancel, ApprovedIntent, CancelIntent, EnterIntent, RiskContext
from tyrex_pm.execution.live_oms import LiveOMS
from tyrex_pm.execution.oms import SingleWriterOMS
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.runtime.config import load_app_config, parse_app_config


class _FakeBridge:
    def __init__(self) -> None:
        self.posted: list = []
        self.cancelled: list[VenueOrderId] = []

    async def create_and_post_limit(self, req) -> dict:
        self.posted.append(req)
        return {"orderID": "venue-fake-1", "status": "mock"}

    async def cancel_order(self, venue_order_id: VenueOrderId) -> dict:
        self.cancelled.append(venue_order_id)
        return {"canceled": str(venue_order_id)}

    async def post_heartbeat(self, heartbeat_id: str) -> dict:
        return {"ok": True, "heartbeat_id": heartbeat_id}


@pytest.mark.asyncio
async def test_live_oms_submits_via_bridge() -> None:
    fb = _FakeBridge()
    oms = LiveOMS(fb)  # type: ignore[arg-type]
    ap = ApprovedIntent(
        intent=EnterIntent(
            token_id=TokenId("123"),
            side=Side.BUY,
            size=Decimal("1"),
            limit_price=Decimal("0.5"),
            order_style=OrderStyle.GTC,
        ),
        client_order_id=ClientOrderId(str(uuid4())),
        run_id=RunId("r"),
    )
    out = await oms.submit(ap)
    assert "venue-fake-1" in out
    assert len(fb.posted) == 1
    assert fb.posted[0].token_id == TokenId("123")


@pytest.mark.asyncio
async def test_single_writer_oms_place_then_cancel_serializes() -> None:
    fb = _FakeBridge()
    inner = LiveOMS(fb)  # type: ignore[arg-type]
    sw = SingleWriterOMS(inner)
    sw.start()
    try:
        ap = ApprovedIntent(
            intent=EnterIntent(
                token_id=TokenId("123"),
                side=Side.BUY,
                size=Decimal("1"),
                limit_price=Decimal("0.5"),
                order_style=OrderStyle.GTC,
            ),
            client_order_id=ClientOrderId(str(uuid4())),
            run_id=RunId("r"),
        )
        r1 = await sw.submit(ap)
        assert "venue-fake-1" in r1
        vid = VenueOrderId("venue-fake-1")
        ac = ApprovedCancel(
            venue_order_id=vid,
            client_order_id=ap.client_order_id,
            run_id=RunId("r"),
            intent_id=ap.intent.intent_id,
        )
        r2 = await sw.cancel(ac)
        assert vid in fb.cancelled
        assert "venue-fake-1" in r2
    finally:
        await sw.stop()


@pytest.mark.asyncio
async def test_t6_supervised_round_trip_mocked() -> None:
    """Proven path: submit → parse venue id → cancel on same SingleWriterOMS queue."""
    fb = _FakeBridge()
    inner = LiveOMS(fb)  # type: ignore[arg-type]
    sw = SingleWriterOMS(inner)
    sw.start()
    try:
        ap = ApprovedIntent(
            intent=EnterIntent(
                token_id=TokenId("123"),
                side=Side.BUY,
                size=Decimal("1"),
                limit_price=Decimal("0.5"),
                order_style=OrderStyle.GTC,
            ),
            client_order_id=ClientOrderId(str(uuid4())),
            run_id=RunId("r"),
        )
        res_place = await sw.submit(ap)
        oid = json.loads(res_place).get("orderID")
        assert oid == "venue-fake-1"
        ac = ApprovedCancel(
            venue_order_id=VenueOrderId(str(oid)),
            client_order_id=ap.client_order_id,
            run_id=RunId("r"),
            intent_id=ap.intent.intent_id,
        )
        await sw.cancel(ac)
        assert fb.cancelled == [VenueOrderId("venue-fake-1")]
    finally:
        await sw.stop()


def test_cancel_bypasses_concurrency_and_ws_stale_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    base = load_app_config(repo_root=root, scenario_file="shadow_guru")
    app = parse_app_config(
        risk=base.raw["risk"],
        strategy=base.raw["strategy"],
        runtime={**base.raw["runtime"], "execution_mode": "live"},
    )
    ci = CancelIntent(venue_order_id=VenueOrderId("0xabc"), client_order_id=None)
    ctx = RiskContext(
        execution_mode=ExecutionMode.LIVE,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=None,
        usdc_allowance=None,
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=False,
        clob_session_ok=False,
        in_flight_order_count=99,
        orders_in_flight_by_token={},
        venue_truth_stale=True,
    )
    d = evaluate_intent(ci, ctx, app=app, run_id=RunId("r"))
    assert d.approved and d.approved_cancel is not None


def test_aggressive_denies_when_venue_truth_stale() -> None:
    root = Path(__file__).resolve().parents[1]
    base = load_app_config(repo_root=root, scenario_file="shadow_guru")
    app = parse_app_config(
        risk={
            **base.raw["risk"],
            "readiness": {
                **base.raw["risk"].get("readiness", {}),
                "require_user_ws_live": True,
            },
        },
        strategy=base.raw["strategy"],
        runtime={**base.raw["runtime"], "execution_mode": "live"},
    )
    intent = EnterIntent(
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ctx = RiskContext(
        execution_mode=ExecutionMode.LIVE,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={TokenId("1234567890"): Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        venue_truth_stale=True,
    )
    d = evaluate_intent(intent, ctx, app=app, run_id=RunId("r"))
    assert not d.approved
    assert rc.VENUE_TRUTH_STALE in d.reason_codes


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("TYREX_LIVE_SMOKE") != "1",
    reason="Set TYREX_LIVE_SMOKE=1 and TYREX_PRIVATE_KEY for real CLOB smoke (designated wallet only)",
)
async def test_t6_real_clob_heartbeat_smoke() -> None:
    from tyrex_pm.runtime.health_runtime import HealthRuntime
    from tyrex_pm.venue.polymarket.clob_bridge import PyClobBridge
    from tyrex_pm.venue.polymarket.clob_env import try_create_clob_client
    from tyrex_pm.venue.polymarket.clob_heartbeat import post_heartbeat_with_recovery

    c = try_create_clob_client()
    assert c is not None
    b = PyClobBridge(c)
    h = HealthRuntime()
    assert await post_heartbeat_with_recovery(h, b)
