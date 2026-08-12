"""Unit tests for pure protection trigger evaluation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.protection.reasons import ProtectionReason
from tyrex_pm.protection.spec import (
    ProtectionSpec,
    ProtectionSpecError,
    StopLossSpec,
    TakeProfitSpec,
    TrailingSpec,
    protection_spec_from_mapping,
)
from tyrex_pm.protection.triggers import evaluate_triggers, resolve_thresholds


def _spec(**kwargs) -> ProtectionSpec:
    base = {
        "mark": "bid",
        "size_mode": "full",
        "take_profit": {"style": "reactive_fak", "absolute": "0.80"},
        "stop_loss": {"style": "stop_market_fak", "absolute": "0.40"},
        "trailing": {
            "enabled": True,
            "activation": "immediate",
            "trail_abs": "0.05",
            "mark": "bid",
        },
    }
    base.update(kwargs)
    return protection_spec_from_mapping(base)


def test_spec_rejects_both_absolute_and_pct() -> None:
    with pytest.raises(ProtectionSpecError, match="exactly one"):
        protection_spec_from_mapping(
            {
                "mark": "bid",
                "size_mode": "full",
                "take_profit": {
                    "style": "reactive_fak",
                    "absolute": "0.8",
                    "pct_from_entry": "0.1",
                },
                "stop_loss": {"style": "stop_market_fak", "absolute": "0.4"},
                "trailing": {
                    "enabled": False,
                    "activation": "immediate",
                    "mark": "bid",
                },
            }
        )


def test_stop_loss_fires_before_take_profit() -> None:
    spec = ProtectionSpec(
        mark="bid",
        size_mode="full",
        take_profit=TakeProfitSpec(style="reactive_fak", absolute=Decimal("0.30"), pct_from_entry=None),
        stop_loss=StopLossSpec(style="stop_market_fak", absolute=Decimal("0.40"), pct_from_entry=None),
        trailing=TrailingSpec(
            enabled=False,
            activation="immediate",
            trail_abs=None,
            trail_pct=None,
            activation_profit_abs=None,
            mark="bid",
        ),
    )
    thresholds = resolve_thresholds(spec=spec, entry_price=Decimal("0.50"))
    decision = evaluate_triggers(
        spec=spec,
        mark=Decimal("0.35"),
        entry_price=Decimal("0.50"),
        peak=Decimal("0.50"),
        trailing_active=False,
        thresholds=thresholds,
    )
    assert decision.fired
    assert decision.reason is ProtectionReason.PROTECTION_SL


def test_trailing_fires_on_pullback() -> None:
    spec = _spec(take_profit=None)
    thresholds = resolve_thresholds(spec=spec, entry_price=Decimal("0.50"))
    mid = evaluate_triggers(
        spec=spec,
        mark=Decimal("0.70"),
        entry_price=Decimal("0.50"),
        peak=Decimal("0.50"),
        trailing_active=True,
        thresholds=thresholds,
    )
    assert not mid.fired
    assert mid.peak == Decimal("0.70")
    fire = evaluate_triggers(
        spec=spec,
        mark=Decimal("0.64"),
        entry_price=Decimal("0.50"),
        peak=mid.peak,
        trailing_active=True,
        thresholds=thresholds,
    )
    assert fire.fired
    assert fire.reason is ProtectionReason.PROTECTION_TRAIL


def test_take_profit_fires_on_bid() -> None:
    spec = _spec(
        trailing={
            "enabled": False,
            "activation": "immediate",
            "mark": "bid",
        }
    )
    thresholds = resolve_thresholds(spec=spec, entry_price=Decimal("0.50"))
    decision = evaluate_triggers(
        spec=spec,
        mark=Decimal("0.81"),
        entry_price=Decimal("0.50"),
        peak=Decimal("0.50"),
        trailing_active=False,
        thresholds=thresholds,
    )
    assert decision.fired
    assert decision.reason is ProtectionReason.PROTECTION_TP


def test_pct_thresholds_resolve_from_entry() -> None:
    spec = protection_spec_from_mapping(
        {
            "mark": "bid",
            "size_mode": "full",
            "take_profit": {"style": "reactive_fak", "pct_from_entry": "0.20"},
            "stop_loss": {"style": "stop_market_fak", "pct_from_entry": "0.10"},
            "trailing": {
                "enabled": False,
                "activation": "immediate",
                "mark": "bid",
            },
        }
    )
    thresholds = resolve_thresholds(spec=spec, entry_price=Decimal("0.50"))
    assert thresholds.take_profit == Decimal("0.60")
    assert thresholds.stop_loss == Decimal("0.45")
