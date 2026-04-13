"""Scenario A validation SELL quantity: portfolio net cap + step floor (no live trader)."""

from __future__ import annotations

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.strategy.bot_sell_validate_strategy import (
    BotSellValidateStrategyConfig,
    resolve_validation_sell_quantity,
)
from tyrex_pm.strategy.validation_constants import DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS

_IID = InstrumentId.from_str("0xcond-tok.POLYMARKET")


class _Inst:
    def __init__(self, step: float = 0.01) -> None:
        self.size_increment = step
        self.min_quantity = 0.0


class _Cache:
    def __init__(self, inst: _Inst | None) -> None:
        self._inst = inst

    def instrument(self, iid: InstrumentId) -> _Inst | None:
        _ = iid
        return self._inst


class _Portfolio:
    def __init__(self, net: float) -> None:
        self._net = net

    def net_position(self, iid: InstrumentId) -> float:
        _ = iid
        return self._net


def test_resolve_caps_sell_to_net_below_buy_fill_and_floors_step() -> None:
    step = 0.01
    inst = _Inst(step=step)
    qty, meta = resolve_validation_sell_quantity(
        portfolio=_Portfolio(8.489),
        cache=_Cache(inst),
        instrument_id=_IID,
        quantity_from_buy_fill=8.69,
        haircut_bps=0.0,
    )
    assert qty == pytest.approx(8.48)
    assert meta["portfolio_net_long"] == pytest.approx(8.489)
    assert meta["raw_cap"] == pytest.approx(8.489)
    assert "capped_vs_buy_fill" in meta["resolution_note"]
    assert qty <= meta["portfolio_net_long"] + 1e-9


def test_resolve_uncapped_when_net_covers_buy_fill() -> None:
    inst = _Inst(step=0.01)
    qty, meta = resolve_validation_sell_quantity(
        portfolio=_Portfolio(10.0),
        cache=_Cache(inst),
        instrument_id=_IID,
        quantity_from_buy_fill=8.69,
        haircut_bps=0.0,
    )
    assert qty == pytest.approx(8.69)
    assert meta["resolution_note"] == "uncapped"


def test_resolve_haircut_reduces_cap_before_min_vs_buy() -> None:
    inst = _Inst(step=0.01)
    qty, meta = resolve_validation_sell_quantity(
        portfolio=_Portfolio(100.0),
        cache=_Cache(inst),
        instrument_id=_IID,
        quantity_from_buy_fill=100.0,
        haircut_bps=100.0,
    )
    assert meta["inventory_after_haircut"] == pytest.approx(99.0)
    assert meta["raw_cap_before_haircut"] == pytest.approx(100.0)
    assert meta["inventory_long_before_haircut"] == pytest.approx(100.0)
    assert meta["validation_only_inventory_haircut"] is True
    assert qty == pytest.approx(99.0)
    assert "capped_vs_buy_fill" in meta["resolution_note"]
    assert "inventory_haircut_applied" in meta["resolution_note"]


def test_resolve_observed_float_mismatch_net_5_26_haircut_200_bps() -> None:
    """Live run: Nautilus net/buy 5.26 vs venue atomic ~5.16 — ~200 bps conservative shave."""
    inst = _Inst(step=0.01)
    qty, meta = resolve_validation_sell_quantity(
        portfolio=_Portfolio(5.26),
        cache=_Cache(inst),
        instrument_id=_IID,
        quantity_from_buy_fill=5.26,
        haircut_bps=200.0,
    )
    assert meta["inventory_long_before_haircut"] == pytest.approx(5.26)
    assert meta["raw_cap_before_haircut"] == pytest.approx(5.26)
    assert meta["inventory_after_haircut"] == pytest.approx(5.26 * 0.98)
    assert meta["raw_cap"] == pytest.approx(5.26 * 0.98)
    assert qty == pytest.approx(5.15)
    assert qty < 5.26
    assert "inventory_haircut_applied" in meta["resolution_note"]
    assert "capped_vs_buy_fill" in meta["resolution_note"]


def test_default_haircut_constant_matches_strategy_config() -> None:
    cfg = BotSellValidateStrategyConfig(execution_mode="shadow")
    assert cfg.validation_sell_inventory_haircut_bps == pytest.approx(
        DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS,
    )
    assert DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS == pytest.approx(200.0)


def test_resolve_buy_fill_only_when_portfolio_missing() -> None:
    inst = _Inst()
    qty, meta = resolve_validation_sell_quantity(
        portfolio=None,
        cache=_Cache(inst),
        instrument_id=_IID,
        quantity_from_buy_fill=3.33,
        haircut_bps=0.0,
    )
    assert qty == pytest.approx(3.33)
    assert meta["resolution_note"] == "portfolio_or_cache_unavailable"
    assert meta.get("validation_inventory_haircut_note") == "haircut_skipped_no_portfolio_cache"


def test_resolve_non_positive_buy_fill() -> None:
    qty, meta = resolve_validation_sell_quantity(
        portfolio=_Portfolio(5.0),
        cache=_Cache(_Inst()),
        instrument_id=_IID,
        quantity_from_buy_fill=0.0,
        haircut_bps=0.0,
    )
    assert qty == 0.0
    assert meta["resolution_note"] == "non_positive_buy_fill"


def test_resolve_short_portfolio_yields_zero_sell() -> None:
    qty, _meta = resolve_validation_sell_quantity(
        portfolio=_Portfolio(-1.0),
        cache=_Cache(_Inst()),
        instrument_id=_IID,
        quantity_from_buy_fill=5.0,
        haircut_bps=0.0,
    )
    assert qty == 0.0


def test_resolve_micro_step_floors_without_exceeding_raw_cap() -> None:
    inst = _Inst(step=1e-6)
    raw_inv = 8.4891234
    qty, meta = resolve_validation_sell_quantity(
        portfolio=_Portfolio(raw_inv),
        cache=_Cache(inst),
        instrument_id=_IID,
        quantity_from_buy_fill=9.0,
        haircut_bps=0.0,
    )
    n = int(raw_inv / 1e-6 + 1e-12)
    assert qty == pytest.approx(n * 1e-6)
    assert qty <= raw_inv + 1e-12
    assert meta["quantity_after_step"] == qty


def test_resolve_haircut_then_micro_step_floor() -> None:
    inst = _Inst(step=1e-6)
    inv_long = 5.26
    hb = 200.0
    inv_adj = inv_long * (1.0 - hb / 10_000.0)
    qty, meta = resolve_validation_sell_quantity(
        portfolio=_Portfolio(inv_long),
        cache=_Cache(inst),
        instrument_id=_IID,
        quantity_from_buy_fill=5.26,
        haircut_bps=hb,
    )
    n = int(inv_adj / 1e-6 + 1e-12)
    assert qty == pytest.approx(n * 1e-6)
    assert qty <= inv_adj + 1e-12
    assert "size_step_floor" in meta["resolution_note"] or qty == pytest.approx(inv_adj)
