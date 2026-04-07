"""C3 execution helpers and runtime loader knobs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from tyrex_pm.config.loaders import load_runtime_settings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.execution.c3_book_top import BookTop
from tyrex_pm.execution.c3_depth import clip_to_book_depth
from tyrex_pm.execution.c3_entry_guard import check_entry_guard
from tyrex_pm.execution.c3_normalize import floor_quantity_to_step, quantize_limit_order_for_instrument
from tyrex_pm.execution.nautilus_guru_exec import NautilusGuruExecutionPort


def _inst(*, tick=0.01, step=1.0, min_q=1.0):
    m = MagicMock()
    m.price_increment = tick
    m.size_increment = step
    m.min_quantity = min_q
    return m


def test_quantize_floors_qty_and_price_buy() -> None:
    inst = _inst()
    r = quantize_limit_order_for_instrument(
        inst,
        side="BUY",
        price=0.505,
        quantity=10.7,
    )
    assert r.ok
    assert r.quantity <= 10.7 + 1e-9
    assert r.price <= 0.505 + 1e-9


def test_quantize_allows_small_buy_notional_when_above_min_q() -> None:
    """Business min notional is risk; quantize only checks instrument grid."""
    inst = _inst(step=1.0, min_q=1.0)
    r = quantize_limit_order_for_instrument(
        inst,
        side="BUY",
        price=0.01,
        quantity=3.0,
    )
    assert r.ok
    assert abs(r.price * r.quantity - 0.03) < 1e-6


def test_quantize_fails_below_min_quantity_without_bump() -> None:
    inst = _inst(step=1.0, min_q=5.0)
    r = quantize_limit_order_for_instrument(
        inst,
        side="BUY",
        price=0.5,
        quantity=4.0,
    )
    assert not r.ok
    assert "min_quantity" in r.detail


def test_quantize_ok_when_min_q_satisfied_after_floor() -> None:
    inst = _inst(step=1.0, min_q=5.0)
    r = quantize_limit_order_for_instrument(
        inst,
        side="BUY",
        price=0.5,
        quantity=7.2,
    )
    assert r.ok
    assert r.quantity == 7.0


def test_entry_guard_buy_blocks_when_ask_far() -> None:
    b = BookTop(best_bid=0.4, best_ask=0.6, best_bid_size=10.0, best_ask_size=5.0, source="cache")
    g = check_entry_guard(
        side="BUY",
        reference_price=0.5,
        book=b,
        max_slippage_ticks=3,
        tick_size=0.01,
    )
    assert not g.ok


def test_entry_guard_buy_allows_within_ticks() -> None:
    b = BookTop(best_bid=0.49, best_ask=0.51, best_bid_size=10.0, best_ask_size=5.0, source="cache")
    g = check_entry_guard(
        side="BUY",
        reference_price=0.5,
        book=b,
        max_slippage_ticks=5,
        tick_size=0.01,
    )
    assert g.ok


def test_depth_clip_reduces() -> None:
    b = BookTop(best_bid=0.4, best_ask=0.55, best_bid_size=100.0, best_ask_size=10.0, source="cache")
    d = clip_to_book_depth(side="BUY", quantity=50.0, book=b, utilization_cap=0.5)
    assert d.quantity == 5.0
    assert d.clipped


def test_floor_quantity_to_step() -> None:
    inst = _inst(step=2.0)
    assert floor_quantity_to_step(inst, 7.9, approved_quantity=10.0) == 6.0


def test_load_runtime_c3_defaults(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "X-Y",
                "execution_mode": "shadow",
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert not r.execution_limit_timeout_enabled


@pytest.mark.parametrize(
    "bad_key",
    ("venue_size_alignment_mode", "execution_venue_normalize_enabled"),
)
def test_load_runtime_rejects_obsolete_venue_alignment_keys(tmp_path: Path, bad_key: str) -> None:
    raw = {"trader_id": "X-Y", "execution_mode": "shadow", bad_key: "align"}
    if bad_key == "execution_venue_normalize_enabled":
        raw[bad_key] = True
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="obsolete key"):
        load_runtime_settings(p)


def test_load_runtime_guard_requires_ticks(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "X-Y",
                "execution_mode": "shadow",
                "execution_entry_guard_enabled": True,
                "execution_max_entry_slippage_ticks": 0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="execution_max_entry_slippage_ticks"):
        load_runtime_settings(p)


def test_nautilus_port_c3_off_preserves_submit() -> None:
    from tyrex_pm.config.loaders import RuntimeSettings

    rt = RuntimeSettings(
        trader_id="T-001",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/dedup.json",
        guru_state_path="var/wm.json",
        guru_activity_limit=100,
        guru_startup_backfill_seconds=0.0,
        guru_max_activity_pages_per_poll=4,
        logging_level="INFO",
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        polymarket_instrument_ids=("0xabc-12345.POLYMARKET",),
        polymarket_token_to_instrument=(("12345", "0xabc-12345.POLYMARKET"),),
        polymarket_dynamic_instruments=False,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=32,
    )
    inst_stub = MagicMock()
    inst_stub.make_qty.return_value = MagicMock()
    inst_stub.make_price.return_value = MagicMock()
    inst_stub.price_increment = 0.01
    inst_stub.size_increment = 1.0
    inst_stub.min_quantity = 1.0

    order_sent = MagicMock()
    coid = MagicMock()
    coid.value = "TXabc"
    order_sent.client_order_id = coid

    of = MagicMock()
    of.limit.return_value = order_sent

    strategy = MagicMock()
    strategy.cache = MagicMock()
    strategy.cache.instrument.return_value = inst_stub
    strategy.order_factory = of
    strategy.submit_order = MagicMock()
    strategy.log = MagicMock()
    strategy.clock = MagicMock()

    port = NautilusGuruExecutionPort(strategy, rt)  # type: ignore[arg-type]
    intent = OrderIntent(
        correlation_id="g",
        token_id="12345",
        side="BUY",
        quantity=2.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.51,
    )
    port.submit_intent(intent, mode="live")
    strategy.submit_order.assert_called_once()
    strategy.clock.set_timer.assert_not_called()


def test_nautilus_port_strict_skips_without_book() -> None:
    from tyrex_pm.config.loaders import RuntimeSettings

    instr_s = "0xcond-12345.POLYMARKET"
    rt = RuntimeSettings(
        trader_id="T-001",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/dedup.json",
        guru_state_path="var/wm.json",
        guru_activity_limit=100,
        guru_startup_backfill_seconds=0.0,
        guru_max_activity_pages_per_poll=4,
        logging_level="INFO",
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        polymarket_instrument_ids=("0xabc-12345.POLYMARKET",),
        polymarket_token_to_instrument=(("12345", instr_s),),
        polymarket_dynamic_instruments=False,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=32,
        execution_entry_guard_enabled=True,
        execution_max_entry_slippage_ticks=5,
        execution_book_strict=True,
    )
    inst_stub = MagicMock()
    inst_stub.make_qty.return_value = MagicMock()
    inst_stub.make_price.return_value = MagicMock()
    inst_stub.price_increment = 0.01
    inst_stub.size_increment = 1.0
    inst_stub.min_quantity = 1.0

    strategy = MagicMock()
    strategy.cache = MagicMock()
    strategy.cache.instrument.return_value = inst_stub
    strategy.cache.has_order_book.return_value = False
    strategy.order_factory = MagicMock()
    strategy.submit_order = MagicMock()
    strategy.log = MagicMock()

    port = NautilusGuruExecutionPort(strategy, rt)  # type: ignore[arg-type]
    intent = OrderIntent(
        correlation_id="x",
        token_id="12345",
        side="BUY",
        quantity=5.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.5,
    )
    port.submit_intent(intent, mode="live")
    strategy.submit_order.assert_not_called()
    msg = strategy.log.info.call_args[0][0]
    assert ReasonCode.EXEC_BOOK_UNAVAILABLE_SKIP in msg
