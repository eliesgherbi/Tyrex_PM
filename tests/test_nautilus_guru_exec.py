"""Step 4: Nautilus framework guru submit path."""

from __future__ import annotations

from unittest.mock import MagicMock

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.execution.nautilus_guru_exec import (
    NautilusGuruExecutionPort,
    _client_order_id_from_guru_correlation,
)


def _runtime_live_nautilus(*, token_map: tuple[tuple[str, str], ...]) -> RuntimeSettings:
    return RuntimeSettings(
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
        polymarket_nautilus_live=True,
        polymarket_instrument_ids=("0xabc-12345.POLYMARKET",),
        polymarket_framework_submit=True,
        polymarket_token_to_instrument=token_map,
        polymarket_dynamic_instruments=False,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=32,
    )


def test_client_order_id_deterministic() -> None:
    a = _client_order_id_from_guru_correlation("0xabc")
    b = _client_order_id_from_guru_correlation("0xabc")
    c = _client_order_id_from_guru_correlation("0xdef")
    assert a == b
    assert a != c
    assert str(a).startswith("TX")


def test_nautilus_guru_port_calls_submit_order() -> None:
    instr_s = "0xcond-12345.POLYMARKET"
    rt = _runtime_live_nautilus(token_map=(("12345", instr_s),))

    inst_stub = MagicMock()
    inst_stub.make_qty.return_value = MagicMock()
    inst_stub.make_price.return_value = MagicMock()

    order_sent = MagicMock()
    order_sent.client_order_id = "TXplaceholder"

    of = MagicMock()
    of.limit.return_value = order_sent

    strategy = MagicMock()
    strategy.cache = MagicMock()
    strategy.cache.instrument.return_value = inst_stub
    strategy.order_factory = of
    strategy.submit_order = MagicMock()
    strategy.log = MagicMock()

    port = NautilusGuruExecutionPort(strategy, rt)  # type: ignore[arg-type]
    intent = OrderIntent(
        correlation_id="guru-tx-1",
        token_id="12345",
        side="BUY",
        quantity=2.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.51,
    )
    port.submit_intent(intent, mode="live")

    strategy.submit_order.assert_called_once()
    args, _kwargs = strategy.submit_order.call_args
    assert args[0] is order_sent
    of.limit.assert_called_once()
    _, fk = of.limit.call_args
    assert fk["instrument_id"] == InstrumentId.from_str(instr_s)
    assert fk["order_side"] == OrderSide.BUY
    assert fk["client_order_id"] == _client_order_id_from_guru_correlation("guru-tx-1")
    assert fk["tags"] and "guru_cid=" in fk["tags"][0]


def test_nautilus_guru_port_unmapped_skips_with_reason() -> None:
    rt = _runtime_live_nautilus(token_map=())

    strategy = MagicMock()
    strategy.cache = MagicMock()
    strategy.log = MagicMock()

    port = NautilusGuruExecutionPort(strategy, rt)  # type: ignore[arg-type]
    intent = OrderIntent(
        correlation_id="guru-tx-2",
        token_id="unknown_token",
        side="BUY",
        quantity=10.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.5,
    )
    port.submit_intent(intent, mode="live")

    strategy.submit_order.assert_not_called()
    strategy.log.error.assert_called()
    msg = strategy.log.error.call_args[0][0]
    assert str(ReasonCode.GURU_INSTRUMENT_UNMAPPED) in msg


def test_nautilus_guru_port_dynamic_submits_when_bootstrap_missing() -> None:
    """Dynamic controller supplies instrument when token not in YAML-derived map."""
    rt = _runtime_live_nautilus(token_map=())

    instr_s = "0xcond-77777.POLYMARKET"
    inst_stub = MagicMock()
    inst_stub.id = InstrumentId.from_str(instr_s)
    inst_stub.make_qty.return_value = MagicMock()
    inst_stub.make_price.return_value = MagicMock()

    dynamic = MagicMock()
    dynamic.resolve_and_activate.return_value = (inst_stub, "")

    order_sent = MagicMock()
    order_sent.client_order_id = "TXdyn"

    of = MagicMock()
    of.limit.return_value = order_sent

    strategy = MagicMock()
    strategy.cache = MagicMock()
    strategy.order_factory = of
    strategy.submit_order = MagicMock()
    strategy.log = MagicMock()

    port = NautilusGuruExecutionPort(strategy, rt, dynamic=dynamic)  # type: ignore[arg-type]
    intent = OrderIntent(
        correlation_id="guru-tx-dyn",
        token_id="77777",
        side="BUY",
        quantity=10.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.55,
    )
    port.submit_intent(intent, mode="live")

    dynamic.resolve_and_activate.assert_called_once_with("77777")
    strategy.submit_order.assert_called_once()


def test_nautilus_guru_port_dynamic_cap_skips() -> None:
    rt = _runtime_live_nautilus(token_map=())

    dynamic = MagicMock()
    dynamic.resolve_and_activate.return_value = (None, "activation_cap")

    strategy = MagicMock()
    strategy.cache = MagicMock()
    strategy.log = MagicMock()

    port = NautilusGuruExecutionPort(strategy, rt, dynamic=dynamic)  # type: ignore[arg-type]
    intent = OrderIntent(
        correlation_id="guru-tx-cap",
        token_id="999",
        side="BUY",
        quantity=10.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.5,
    )
    port.submit_intent(intent, mode="live")

    strategy.submit_order.assert_not_called()
    strategy.log.error.assert_called()
    assert str(ReasonCode.GURU_DYNAMIC_ACTIVATION_CAP) in strategy.log.error.call_args[0][0]


def test_nautilus_guru_static_overlay_when_dynamic_fails() -> None:
    """YAML map + cache used when dynamic resolution returns failure."""
    instr_s = "0xcond-12345.POLYMARKET"
    rt = _runtime_live_nautilus(token_map=(("12345", instr_s),))

    inst_stub = MagicMock()
    inst_stub.make_qty.return_value = MagicMock()
    inst_stub.make_price.return_value = MagicMock()

    dynamic = MagicMock()
    dynamic.resolve_and_activate.return_value = (None, "resolve_failed")

    order_sent = MagicMock()
    order_sent.client_order_id = "TXoverlay"
    of = MagicMock()
    of.limit.return_value = order_sent

    strategy = MagicMock()
    strategy.cache = MagicMock()
    strategy.cache.instrument.return_value = inst_stub
    strategy.order_factory = of
    strategy.submit_order = MagicMock()
    strategy.log = MagicMock()

    port = NautilusGuruExecutionPort(strategy, rt, dynamic=dynamic)  # type: ignore[arg-type]
    intent = OrderIntent(
        correlation_id="guru-overlay",
        token_id="12345",
        side="BUY",
        quantity=10.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.51,
    )
    port.submit_intent(intent, mode="live")

    strategy.submit_order.assert_called_once()
    strategy.cache.instrument.assert_called()
