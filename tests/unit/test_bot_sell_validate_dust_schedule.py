"""Scenario A: venue-aware ``size_increment`` dust vs real partial fills (validation harness only)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tyrex_pm.strategy.bot_sell_validate_strategy import (
    BotSellValidateStrategy,
    BotSellValidateStrategyConfig,
    _VALIDATION_DUST_SHARE_STEP_FLOOR,
    _validation_dust_size_step,
    _validation_order_effectively_complete,
)


class _HarnessSelf:
    """Minimal ``self`` for exercising :meth:`BotSellValidateStrategy._on_validation_sell_filled`."""

    cache: MagicMock

    def _validation_effectiveness_for_cached_order(self, cached):  # noqa: ANN001
        inst = self.cache.instrument(getattr(cached, "instrument_id", None))
        eff_step, reported = _validation_dust_size_step(inst)
        ev = _validation_order_effectively_complete(cached, size_step=eff_step)
        if reported + 1e-15 < eff_step:
            return replace(
                ev,
                tolerance_rule=(
                    f"{ev.tolerance_rule} | reported_size_increment={reported}; "
                    f"effective_dust_step={eff_step} (Scenario A floor {_VALIDATION_DUST_SHARE_STEP_FLOOR})"
                ),
            )
        return ev


def test_buy_not_closed_dust_below_size_increment_schedules_rule() -> None:
    """Log pattern: qty 5.128205 vs fill 5.12, step 0.01 — economically dust, not a 6-lot partial."""
    o = SimpleNamespace(
        is_closed=False,
        instrument_id="iid",
        quantity=5.128205,
        filled_qty=5.12,
        leaves_qty=0.008205,
        status=SimpleNamespace(name="PARTIALLY_FILLED"),
    )
    ev = _validation_order_effectively_complete(o, size_step=0.01)
    assert ev.effective_complete is True
    assert ev.reason == "remainder_below_size_increment"
    assert ev.is_closed is False


def test_reported_nautilus_size_increment_1e6_needs_floor_for_operator_dust() -> None:
    """Facts from bot-sell-validate-01: ``size_increment`` in cache was 1e-6 but remainder ~9e-4."""
    o = SimpleNamespace(
        is_closed=False,
        quantity=9.090909,
        filled_qty=9.09,
        leaves_qty=0.000909,
        status=SimpleNamespace(name="PARTIALLY_FILLED"),
    )
    assert _validation_order_effectively_complete(o, size_step=1e-6).effective_complete is False
    assert _validation_order_effectively_complete(o, size_step=0.01).effective_complete is True


def test_buy_not_closed_sqrt_pattern_9_09_vs_9_090909() -> None:
    o = SimpleNamespace(
        is_closed=False,
        quantity=9.090909,
        filled_qty=9.09,
        leaves_qty=0.000909,
        status=None,
    )
    ev = _validation_order_effectively_complete(o, size_step=0.01)
    assert ev.effective_complete is True
    assert ev.reason == "remainder_below_size_increment"


def test_sell_not_closed_dust_6_09_vs_6_099511() -> None:
    o = SimpleNamespace(
        is_closed=False,
        quantity=6.099511,
        filled_qty=6.09,
        leaves_qty=0.009511,
        status=None,
    )
    ev = _validation_order_effectively_complete(o, size_step=0.01)
    assert ev.effective_complete is True
    assert ev.reason == "remainder_below_size_increment"


def test_no_false_complete_when_remainder_is_one_full_step() -> None:
    """Exactly one minimum increment left — still working / not validation-complete."""
    o = SimpleNamespace(
        is_closed=False,
        quantity=10.0,
        filled_qty=9.99,
        leaves_qty=0.01,
        status=None,
    )
    ev = _validation_order_effectively_complete(o, size_step=0.01)
    assert ev.effective_complete is False
    assert ev.reason == "incomplete_remainder_ge_size_increment"


def test_leaves_zero_does_not_complete_if_qty_remainder_large() -> None:
    """Stale ``leaves_qty=0`` with real remainder must not arm or clear validation early."""
    o = SimpleNamespace(
        is_closed=False,
        quantity=10.0,
        filled_qty=9.0,
        leaves_qty=0.0,
        status=None,
    )
    ev = _validation_order_effectively_complete(o, size_step=0.01)
    assert ev.effective_complete is False


def test_no_false_complete_on_meaningful_partial() -> None:
    o = SimpleNamespace(
        is_closed=False,
        quantity=10.0,
        filled_qty=4.0,
        leaves_qty=6.0,
        status=None,
    )
    ev = _validation_order_effectively_complete(o, size_step=0.01)
    assert ev.effective_complete is False
    assert ev.reason == "incomplete_remainder_ge_size_increment"


def test_is_closed_short_circuits() -> None:
    o = SimpleNamespace(
        is_closed=True,
        quantity=10.0,
        filled_qty=9.0,
        leaves_qty=1.0,
        status=SimpleNamespace(name="FILLED"),
        instrument_id="x",
    )
    ev = _validation_order_effectively_complete(o, size_step=0.01)
    assert ev.effective_complete is True
    assert ev.reason == "is_closed"


def test_small_grid_coarse_partial_still_incomplete() -> None:
    """If venue step is 0.001, remainder 0.008 is still tradable multiples."""
    o = SimpleNamespace(
        is_closed=False,
        quantity=5.128,
        filled_qty=5.12,
        leaves_qty=0.008,
        status=None,
    )
    ev = _validation_order_effectively_complete(o, size_step=0.001)
    assert ev.effective_complete is False


def test_validation_sell_clears_pending_on_dust_without_is_closed() -> None:
    """Harness clears _validate_sell_pending when effectiveness says complete Nautilus OMS isn't."""
    strat = _HarnessSelf()
    strat._vcfg = BotSellValidateStrategyConfig(execution_mode="live")
    strat._validate_sell_pending = True
    strat._validation_sells_filled = 0
    strat.log = MagicMock()

    cached = SimpleNamespace(
        is_closed=False,
        instrument_id=MagicMock(),
        quantity=6.099511,
        filled_qty=6.09,
        leaves_qty=0.009511,
        status=None,
    )
    inst = SimpleNamespace(size_increment=0.01)
    mock_cache = MagicMock()
    mock_cache.instrument.return_value = inst
    mock_cache.order.return_value = cached
    strat.cache = mock_cache

    facts: list[tuple[str, dict]] = []

    def _emit(ft: str, pl: dict) -> None:
        facts.append((ft, pl))

    strat._reporting_emit = _emit

    BotSellValidateStrategy._on_validation_sell_filled(strat, "TXsell", "bot_sell_validate:r1:x")

    assert strat._validate_sell_pending is False
    assert strat._validation_sells_filled == 1
    kinds = [p.get("kind") for _, p in facts]
    assert "order_completion_eval" in kinds
    assert "validation_sell_filled" in kinds


def test_validation_sell_partial_does_not_clear_pending() -> None:
    strat = _HarnessSelf()
    strat._vcfg = BotSellValidateStrategyConfig(execution_mode="live")
    strat._validate_sell_pending = True
    strat._validation_sells_filled = 0
    strat.log = MagicMock()

    cached = SimpleNamespace(
        is_closed=False,
        instrument_id=MagicMock(),
        quantity=10.0,
        filled_qty=3.0,
        leaves_qty=7.0,
        status=None,
    )
    mock_cache = MagicMock()
    mock_cache.instrument.return_value = SimpleNamespace(size_increment=0.01)
    mock_cache.order.return_value = cached
    strat.cache = mock_cache
    strat._reporting_emit = lambda *a, **k: None

    BotSellValidateStrategy._on_validation_sell_filled(strat, "TXp", "bot_sell_validate:r1:y")

    assert strat._validate_sell_pending is True
    assert strat._validation_sells_filled == 0


def test_copy_strategy_module_independent() -> None:
    """Regression: production copy path must not import validation effectiveness helper."""
    import tyrex_pm.strategy.copy_strategy as cs

    assert "_validation_order_effectively_complete" not in cs.__dict__


@pytest.mark.parametrize(
    ("filled", "total", "step", "expect_complete"),
    [
        (5.12, 5.128205, 0.01, True),
        (5.12, 5.15, 0.01, False),
        (9.09, 9.090909, 0.01, True),
    ],
)
def test_remainder_param(filled: float, total: float, step: float, expect_complete: bool) -> None:
    o = SimpleNamespace(
        is_closed=False,
        quantity=total,
        filled_qty=filled,
        leaves_qty=max(0.0, total - filled),
        status=None,
    )
    ev = _validation_order_effectively_complete(o, size_step=step)
    assert ev.effective_complete is expect_complete
