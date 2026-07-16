"""Unit tests for survival exit order-type policy selection."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.core.enums import Side
from tyrex_pm.runtime.config import SurvivalExitOrderPolicyConfig
from tyrex_pm.survival.order_policy import (
    SurvivalExitOrderPolicyMode,
    SurvivalExitOrderType,
    is_retryable_fak_reject,
    reprice_for_attempt,
    select_survival_exit_order,
)


def _cfg(**overrides) -> SurvivalExitOrderPolicyConfig:
    base = {
        "mode": "fak_retry",
        "managed_rest_enabled": False,
        "max_fak_retries": 3,
        "reprice_on_retry": True,
        "sell_reprice_ticks": (0, 1, 3),
        "buy_reprice_ticks": (0, 1, 3),
        "allow_partial_retry": True,
        "disable_managed_rest_when_seconds_to_close_lt": 30.0,
        "urgent_when_seconds_to_close_lt": 20.0,
    }
    base.update(overrides)
    return SurvivalExitOrderPolicyConfig(**base)


def test_first_enforce_attempt_chooses_fak() -> None:
    dec = select_survival_exit_order(
        cfg=_cfg(),
        side=Side.SELL,
        qty=Decimal("5"),
        touch_price=Decimal("0.65"),
        executable_price=Decimal("0.65"),
        attempt_count=0,
        fak_reject_count=0,
        seconds_to_close=600.0,
        allow_partial=True,
        resting_order_open=False,
        trigger_type="survival_trailing_stop",
        module="trailing_stop",
    )
    assert dec is not None
    assert dec.order_type == SurvivalExitOrderType.FAK
    assert dec.reason == "first_survival_enforce_fak"


def test_retryable_fak_reject_retries_fak_with_reprice() -> None:
    dec = select_survival_exit_order(
        cfg=_cfg(),
        side=Side.SELL,
        qty=Decimal("5"),
        touch_price=Decimal("0.65"),
        executable_price=Decimal("0.65"),
        attempt_count=1,
        fak_reject_count=1,
        seconds_to_close=600.0,
        allow_partial=True,
        resting_order_open=False,
        trigger_type="survival_trailing_stop",
        module="trailing_stop",
    )
    assert dec is not None
    assert dec.order_type == SurvivalExitOrderType.FAK
    assert dec.reason == "fak_reject_retry"
    assert dec.price == Decimal("0.64")
    assert dec.reprice_ticks_applied == 1


def test_sell_retry_reprices_lower_by_ticks() -> None:
    price, ticks = reprice_for_attempt(
        Decimal("0.70"),
        side=Side.SELL,
        attempt_index=2,
        cfg=_cfg(),
    )
    assert ticks == 3
    assert price == Decimal("0.67")


def test_buy_retry_reprices_higher_by_ticks() -> None:
    price, ticks = reprice_for_attempt(
        Decimal("0.30"),
        side=Side.BUY,
        attempt_index=2,
        cfg=_cfg(),
    )
    assert ticks == 3
    assert price == Decimal("0.33")


def test_managed_gtc_only_when_enabled_and_not_near_close() -> None:
    cfg = _cfg(mode="fak_then_managed_rest", managed_rest_enabled=True)
    near = select_survival_exit_order(
        cfg=cfg,
        side=Side.SELL,
        qty=Decimal("5"),
        touch_price=Decimal("0.65"),
        executable_price=Decimal("0.65"),
        attempt_count=4,
        fak_reject_count=4,
        seconds_to_close=15.0,
        allow_partial=True,
        resting_order_open=False,
        trigger_type="survival_trailing_stop",
        module="trailing_stop",
    )
    assert near is not None
    assert near.order_type == SurvivalExitOrderType.FAK
    assert near.reason == "near_close_fak_only"

    far = select_survival_exit_order(
        cfg=cfg,
        side=Side.SELL,
        qty=Decimal("5"),
        touch_price=Decimal("0.65"),
        executable_price=Decimal("0.65"),
        attempt_count=4,
        fak_reject_count=4,
        seconds_to_close=120.0,
        allow_partial=True,
        resting_order_open=False,
        trigger_type="survival_trailing_stop",
        module="trailing_stop",
    )
    assert far is not None
    assert far.order_type == SurvivalExitOrderType.GTC
    assert far.requires_cancel_watchdog is True
    assert far.local_ttl_s == 2.0


def test_fok_not_selected_when_partial_allowed() -> None:
    cfg = _cfg(retry_order_type="FOK")
    dec = select_survival_exit_order(
        cfg=cfg,
        side=Side.SELL,
        qty=Decimal("5"),
        touch_price=Decimal("0.65"),
        executable_price=Decimal("0.65"),
        attempt_count=1,
        fak_reject_count=1,
        seconds_to_close=600.0,
        allow_partial=True,
        resting_order_open=False,
        trigger_type="survival_trailing_stop",
        module="trailing_stop",
    )
    assert dec is not None
    assert dec.order_type == SurvivalExitOrderType.FAK


def test_post_only_config_returns_none() -> None:
    cfg = _cfg(post_only_for_survival_exit=True)
    dec = select_survival_exit_order(
        cfg=cfg,
        side=Side.SELL,
        qty=Decimal("5"),
        touch_price=Decimal("0.65"),
        executable_price=Decimal("0.65"),
        attempt_count=0,
        fak_reject_count=0,
        seconds_to_close=600.0,
        allow_partial=True,
        resting_order_open=False,
        trigger_type="survival_trailing_stop",
        module="trailing_stop",
    )
    assert dec is None


def test_is_retryable_fak_reject() -> None:
    assert is_retryable_fak_reject("no orders found to match with FAK order. orders: []")
    assert not is_retryable_fak_reject("insufficient balance")
