"""Phase 5 unit tests — venue truth precedence in :mod:`tyrex_pm.risk.venue_min_size`.

These tests pin the contract documented in
:func:`tyrex_pm.risk.venue_min_size._resolve_min_size`:

1. When ``RiskContext.market_info`` carries a ``MarketInfo`` for the intent's
   token, its ``min_order_size`` wins over the YAML default.
2. The evidence payload records ``venue_min_size_source`` so the audit trail
   shows whether ``"venue"`` or ``"config_default"`` truth was used.
3. When ``ctx.market_info`` is empty (shadow mode / unit-test default), the
   YAML ``default_min_size`` is used unchanged.

We construct a minimal :class:`RiskContext` with the few fields the gate
inspects and verify both the boolean outcome and the evidence row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import EnterIntent, RiskContext
from tyrex_pm.risk.venue_min_size import evaluate_venue_min_size
from tyrex_pm.runtime.config import VenueMinSizeConfig
from tyrex_pm.venue.polymarket.market_info import MarketInfo


def _ctx(market_info: dict[TokenId, MarketInfo] | None = None) -> RiskContext:
    return RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=None,
        mark_prices={},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        market_info=market_info or {},
    )


def _intent(token_id: str = "tok-1", size: str = "3") -> EnterIntent:
    return EnterIntent(
        token_id=TokenId(token_id),
        side=Side.BUY,
        size=Decimal(size),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )


def _mi(min_order_size: str) -> MarketInfo:
    return MarketInfo(
        token_id=TokenId("tok-1"),
        condition_id="0xc",
        tick_size=Decimal("0.01"),
        min_order_size=Decimal(min_order_size),
        neg_risk=False,
        fee_rate_bps=0,
        outcomes={},
        fetched_ts=datetime.now(timezone.utc),
    )


def _cfg(default_min_size: str = "5") -> VenueMinSizeConfig:
    return VenueMinSizeConfig(
        enabled=True, policy="deny", default_min_size=Decimal(default_min_size)
    )


def test_venue_truth_overrides_yaml_default_when_smaller() -> None:
    """Venue says ``mos=2`` → an intent of size 3 must pass even though YAML default is 5."""

    ctx = _ctx({TokenId("tok-1"): _mi("2")})
    res = evaluate_venue_min_size(_intent(size="3"), _cfg("5"), ctx)

    assert res.ok is True
    assert res.deny_reason is None
    assert res.evidence["venue_min_size_source"] == "venue"
    assert res.evidence["venue_min_size"] == "2"


def test_venue_truth_overrides_yaml_default_when_larger() -> None:
    """Venue says ``mos=10`` → an intent of size 5 must deny even though YAML default is 5."""

    ctx = _ctx({TokenId("tok-1"): _mi("10")})
    res = evaluate_venue_min_size(_intent(size="5"), _cfg("5"), ctx)

    assert res.ok is False
    assert res.deny_reason == "below_venue_min_size"
    assert res.evidence["venue_min_size_source"] == "venue"
    assert res.evidence["venue_min_size"] == "10"


def test_falls_back_to_yaml_default_when_market_info_missing() -> None:
    """No ``MarketInfo`` for the token (shadow mode) → YAML default is used."""

    ctx = _ctx({})
    res = evaluate_venue_min_size(_intent(size="3"), _cfg("5"), ctx)

    assert res.ok is False
    assert res.evidence["venue_min_size_source"] == "config_default"
    assert res.evidence["venue_min_size"] == "5"


def test_ctx_none_uses_yaml_default_for_backward_compat() -> None:
    """Legacy callers that pass ``ctx=None`` keep the pre-Phase-5 behaviour."""

    res = evaluate_venue_min_size(_intent(size="3"), _cfg("5"), None)

    assert res.ok is False
    assert res.evidence["venue_min_size_source"] == "config_default"
