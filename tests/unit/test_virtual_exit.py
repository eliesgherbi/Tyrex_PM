"""Virtual TP/SL manager, store, and guru-rest identity."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from tyrex_pm.config.loaders import (
    RiskSettings,
    RuntimeSettings,
    VirtualExitRuntimeSettings,
    VirtualExitStrategySettings,
)
from tyrex_pm.execution.nautilus_guru_exec import guru_client_order_id_value
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.state_readers import OrderSnapshot, is_guru_resting_order
from tyrex_pm.runtime.virtual_exit.lot import ProtectedLot
from tyrex_pm.reporting.schema.facts_v1 import validate_fact_row
from tyrex_pm.reporting.versioning import REPORTING_FACT_SCHEMA_VERSION
from tyrex_pm.runtime.virtual_exit.manager import VirtualExitManager
from tyrex_pm.runtime.virtual_exit.store import VirtualExitStore


def _risk() -> RiskSettings:
    return RiskSettings(
        max_notional_usd_per_order=1000.0,
        max_token_notional_usd_open=float("inf"),
        max_portfolio_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
        capital_gate_enabled=False,
        max_concurrent_guru_resting_orders=2,
    )


def _runtime(ve_rt: VirtualExitRuntimeSettings | None = None) -> RuntimeSettings:
    instr = TestInstrumentProvider.binary_option()
    iid_s = str(instr.id)
    tid = str(get_polymarket_token_id(instr.id))
    ve_rt = ve_rt or replace(
        VirtualExitRuntimeSettings(),
        trigger_price_source="last",
        max_venue_staleness_seconds=0.0,
    )
    return RuntimeSettings(
        trader_id="T-VE-UT",
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
        polymarket_instrument_ids=(iid_s,),
        polymarket_token_to_instrument=((tid, iid_s),),
        polymarket_dynamic_instruments=False,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=32,
        virtual_exit=ve_rt,
    )


def _make_manager(
    tmp_path: Path,
    *,
    ve_rt: VirtualExitRuntimeSettings | None = None,
    venue_stale: bool = False,
    tier_qty: float = 1000.0,
    exec_mock: MagicMock | None = None,
    risk_settings: RiskSettings | None = None,
) -> tuple[VirtualExitManager, str, str, MagicMock]:
    instr = TestInstrumentProvider.binary_option()
    iid_s = str(instr.id)
    token_id = str(get_polymarket_token_id(instr.id))
    rt = _runtime(ve_rt)
    rs = risk_settings or _risk()
    reader = MagicMock()
    reader.list_open_orders.return_value = ()
    reader.count_guru_resting_orders_open = MagicMock(return_value=0)
    db = MagicMock()
    db.filled_usd_for_token.return_value = (500.0, True)
    pol = ConfiguredRiskPolicy(rs, execution_reader=reader, deployment_budget=db)

    venue = MagicMock()
    venue.is_stale.return_value = venue_stale
    venue.position_size = MagicMock(return_value=tier_qty)
    venue.venue_state_cash_ready = True

    lc = MagicMock()
    lc.block_reason_for_side = MagicMock(return_value=None)

    strategy = MagicMock()
    strategy._cfg.execution_mode = "live"
    strategy.cache.instrument.return_value = instr
    strategy.cache.price = MagicMock(
        side_effect=lambda *_a, **_k: instr.make_price(0.60),
    )

    exec_p = exec_mock or MagicMock()

    store = VirtualExitStore(tmp_path / "ve.json")
    mgr = VirtualExitManager(
        strategy,
        ve_strategy=VirtualExitStrategySettings(
            enabled=True,
            take_profit_pct=10.0,
            stop_loss_pct=5.0,
        ),
        ve_runtime=rt.virtual_exit,
        runtime=rt,
        store=store,
        venue_state=venue,
        risk=pol,
        execution=exec_p,
        emit=MagicMock(),
        wallet_sync_ready=lambda: True,
        venue_cash_ready=lambda: True,
        lifecycle=lc,
        risk_settings=rs,
    )
    return mgr, token_id, iid_s, exec_p


def test_store_corrupt_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json {{{", encoding="utf-8")
    st = VirtualExitStore(p)
    assert st.load_lots() == []


def test_store_roundtrip(tmp_path: Path) -> None:
    st = VirtualExitStore(tmp_path / "ok.json")
    lot = ProtectedLot(lot_id="L1", token_id="t", entry_client_order_id="TX" + "a" * 26)
    st.save_lots([lot])
    loaded = st.load_lots()
    assert len(loaded) == 1
    assert loaded[0].lot_id == "L1"


def test_fsm_partial_entry_before_arm(tmp_path: Path) -> None:
    ve_rt = replace(VirtualExitRuntimeSettings(), min_entry_qty_to_arm=100.0, trigger_price_source="last")
    mgr, token_id, _iid, _ex = _make_manager(tmp_path, ve_rt=ve_rt)
    entry_coid = guru_client_order_id_value("g1")
    mgr.register_pending_entry(
        client_order_id=entry_coid,
        guru_correlation_id="g1",
        token_id=token_id,
    )
    instr = TestInstrumentProvider.binary_option()
    order = TestExecStubs.limit_order(
        instrument=instr,
        client_order_id=ClientOrderId(entry_coid),
        quantity=instr.make_qty(50.0),
        price=instr.make_price(0.5),
    )
    fill = TestEventStubs.order_filled(
        order,
        instr,
        last_qty=instr.make_qty(50.0),
        last_px=instr.make_price(0.5),
    )
    mgr.on_order_event(fill)
    lots = mgr._lots  # noqa: SLF001
    assert lots[-1].state == "PENDING_ENTRY"

    fill2 = TestEventStubs.order_filled(
        order,
        instr,
        last_qty=instr.make_qty(50.0),
        last_px=instr.make_price(0.5),
    )
    mgr.on_order_event(fill2)
    assert lots[-1].state == "ARMED"


def test_tier_a_flat_grace_prevents_immediate_disarm_after_arm(tmp_path: Path) -> None:
    ve_rt = replace(
        VirtualExitRuntimeSettings(),
        trigger_price_source="last",
        max_venue_staleness_seconds=0.0,
        tier_a_flat_disarm_grace_seconds=60.0,
    )
    mgr, token_id, _iid, _ex = _make_manager(tmp_path, ve_rt=ve_rt, tier_qty=0.0)
    instr_px = TestInstrumentProvider.binary_option()
    # vwap 0.5 → TP 0.55 / SL 0.475; keep last inside band so TP does not fire while Tier A=0.
    mgr._strategy.cache.price = MagicMock(return_value=instr_px.make_price(0.52))  # noqa: SLF001
    t0 = 1_700_000_000.0
    clk = SimpleNamespace(t=t0)
    with patch(
        "tyrex_pm.runtime.virtual_exit.manager.time.time",
        side_effect=lambda: float(clk.t),
    ):
        entry_coid = guru_client_order_id_value("g-grace")
        mgr.register_pending_entry(
            client_order_id=entry_coid,
            guru_correlation_id="g-grace",
            token_id=token_id,
        )
        instr = TestInstrumentProvider.binary_option()
        mgr.on_order_event(
            TestEventStubs.order_filled(
                TestExecStubs.limit_order(
                    instrument=instr,
                    client_order_id=ClientOrderId(entry_coid),
                    quantity=instr.make_qty(10.0),
                    price=instr.make_price(0.5),
                ),
                instr,
                last_qty=instr.make_qty(10.0),
                last_px=instr.make_price(0.5),
            ),
        )
        assert mgr._lots[-1].state == "ARMED"  # noqa: SLF001
        clk.t = t0 + 0.5
        mgr._on_timer(MagicMock())  # noqa: SLF001
    assert mgr._lots[-1].state == "ARMED"  # noqa: SLF001


def test_tier_a_flat_disarms_after_grace_elapsed(tmp_path: Path) -> None:
    ve_rt = replace(
        VirtualExitRuntimeSettings(),
        trigger_price_source="last",
        max_venue_staleness_seconds=0.0,
        tier_a_flat_disarm_grace_seconds=2.0,
    )
    mgr, token_id, _iid, _ex = _make_manager(tmp_path, ve_rt=ve_rt, tier_qty=0.0)
    instr_px = TestInstrumentProvider.binary_option()
    mgr._strategy.cache.price = MagicMock(return_value=instr_px.make_price(0.52))  # noqa: SLF001
    t0 = 1_700_000_000.0
    clk = SimpleNamespace(t=t0)
    with patch(
        "tyrex_pm.runtime.virtual_exit.manager.time.time",
        side_effect=lambda: float(clk.t),
    ):
        entry_coid = guru_client_order_id_value("g-flat")
        mgr.register_pending_entry(
            client_order_id=entry_coid,
            guru_correlation_id="g-flat",
            token_id=token_id,
        )
        instr = TestInstrumentProvider.binary_option()
        mgr.on_order_event(
            TestEventStubs.order_filled(
                TestExecStubs.limit_order(
                    instrument=instr,
                    client_order_id=ClientOrderId(entry_coid),
                    quantity=instr.make_qty(10.0),
                    price=instr.make_price(0.5),
                ),
                instr,
                last_qty=instr.make_qty(10.0),
                last_px=instr.make_price(0.5),
            ),
        )
        clk.t = t0 + 1.0
        mgr._on_timer(MagicMock())  # noqa: SLF001
        assert mgr._lots[-1].state == "ARMED"  # noqa: SLF001
        clk.t = t0 + 5.0
        mgr._on_timer(MagicMock())  # noqa: SLF001
    assert mgr._lots[-1].state == "DISARMED_EXTERNAL_FLAT"  # noqa: SLF001


def test_tier_a_flat_grace_zero_disarms_immediately(tmp_path: Path) -> None:
    ve_rt = replace(
        VirtualExitRuntimeSettings(),
        trigger_price_source="last",
        max_venue_staleness_seconds=0.0,
        tier_a_flat_disarm_grace_seconds=0.0,
    )
    mgr, token_id, _iid, _ex = _make_manager(tmp_path, ve_rt=ve_rt, tier_qty=0.0)
    entry_coid = guru_client_order_id_value("g-flat0")
    mgr.register_pending_entry(
        client_order_id=entry_coid,
        guru_correlation_id="g-flat0",
        token_id=token_id,
    )
    instr = TestInstrumentProvider.binary_option()
    mgr.on_order_event(
        TestEventStubs.order_filled(
            TestExecStubs.limit_order(
                instrument=instr,
                client_order_id=ClientOrderId(entry_coid),
                quantity=instr.make_qty(10.0),
                price=instr.make_price(0.5),
            ),
            instr,
            last_qty=instr.make_qty(10.0),
            last_px=instr.make_price(0.5),
        ),
    )
    mgr._on_timer(MagicMock())  # noqa: SLF001
    assert mgr._lots[-1].state == "DISARMED_EXTERNAL_FLAT"  # noqa: SLF001


def test_virtual_exit_fact_payloads_validate_for_reporting() -> None:
    env = {
        "run_id": "run-x",
        "fact_schema_version": REPORTING_FACT_SCHEMA_VERSION,
        "recorded_at_utc": "2026-04-16T12:00:00Z",
    }
    rows = (
        {
            **env,
            "fact_type": "virtual_exit_arm",
            "lot_id": "l1",
            "token_id": "tok",
            "guru_correlation_id": "g1",
            "phase": "pending_entry",
            "entry_client_order_id": "x",
        },
        {
            **env,
            "fact_type": "virtual_exit_arm",
            "lot_id": "l1",
            "token_id": "tok",
            "guru_correlation_id": "g1",
            "instrument_id": "iid",
            "entry_qty_filled": 1.0,
            "entry_vwap": 0.5,
            "tp_trigger_price": 0.55,
            "sl_trigger_price": 0.45,
        },
        {
            **env,
            "fact_type": "virtual_exit_trigger",
            "lot_id": "l1",
            "kind": "tp",
            "executable_price": 0.6,
            "trigger_basis": "last",
        },
        {
            **env,
            "fact_type": "virtual_exit_submit",
            "lot_id": "l1",
            "kind": "tp",
            "order_style": "aggressive_limit",
            "qty": 10.0,
            "correlation_id": "ve:l1:tp:n1",
            "intent_origin": "virtual_tp",
        },
        {
            **env,
            "fact_type": "virtual_exit_hold",
            "reason": "venue_stale",
        },
        {
            **env,
            "fact_type": "virtual_exit_hold",
            "lot_id": "l1",
            "reason": "no_price",
        },
        {
            **env,
            "fact_type": "virtual_exit_retry",
            "lot_id": "l1",
            "reason": "OrderRejected",
            "attempt": 1,
        },
        {
            **env,
            "fact_type": "virtual_exit_reconcile",
            "lot_id": "l1",
            "reason": "clamp_to_venue",
            "after_qty": 6.0,
            "tier_a_qty": 6.0,
        },
        {
            **env,
            "fact_type": "virtual_exit_disarm",
            "lot_id": "l1",
            "reason": "tier_a_flat",
            "token_id": "tok",
        },
        {
            **env,
            "fact_type": "virtual_exit_recovery",
            "action": "load_store",
            "lot_count": 0,
        },
        {
            **env,
            "fact_type": "virtual_exit_recovery",
            "action": "clear_stale_exit_coid",
            "lot_id": "l1",
            "detail": "cache_closed_or_missing",
        },
    )
    for row in rows:
        validate_fact_row(row)


def test_tp_trigger_submits_aggressive_limit(tmp_path: Path) -> None:
    mgr, token_id, _iid, ex = _make_manager(tmp_path)
    entry_coid = guru_client_order_id_value("g1")
    mgr.register_pending_entry(
        client_order_id=entry_coid,
        guru_correlation_id="g1",
        token_id=token_id,
    )
    instr = TestInstrumentProvider.binary_option()
    order = TestExecStubs.limit_order(
        instrument=instr,
        client_order_id=ClientOrderId(entry_coid),
        quantity=instr.make_qty(10.0),
        price=instr.make_price(0.5),
    )
    fill = TestEventStubs.order_filled(
        order,
        instr,
        last_qty=instr.make_qty(10.0),
        last_px=instr.make_price(0.5),
    )
    mgr.on_order_event(fill)

    instr2 = TestInstrumentProvider.binary_option()
    mgr._strategy.cache.price = MagicMock(  # noqa: SLF001
        return_value=instr2.make_price(0.60),
    )
    mgr._on_timer(MagicMock())  # noqa: SLF001
    ex.submit_virtual_exit_intent.assert_called()
    assert ex.submit_virtual_exit_intent.call_args.kwargs["order_style"] == "aggressive_limit"
    assert mgr._lots[-1].state == "EXIT_SUBMITTED"  # noqa: SLF001


def test_sl_trigger_uses_market_style(tmp_path: Path) -> None:
    ve_rt = replace(
        VirtualExitRuntimeSettings(),
        trigger_price_source="last",
        max_venue_staleness_seconds=0.0,
        exit_stop_loss_style="market",
    )
    mgr, token_id, _iid, ex = _make_manager(tmp_path, ve_rt=ve_rt)
    entry_coid = guru_client_order_id_value("g2")
    mgr.register_pending_entry(
        client_order_id=entry_coid,
        guru_correlation_id="g2",
        token_id=token_id,
    )
    instr = TestInstrumentProvider.binary_option()
    order = TestExecStubs.limit_order(
        instrument=instr,
        client_order_id=ClientOrderId(entry_coid),
        quantity=instr.make_qty(10.0),
        price=instr.make_price(0.5),
    )
    fill = TestEventStubs.order_filled(
        order,
        instr,
        last_qty=instr.make_qty(10.0),
        last_px=instr.make_price(0.5),
    )
    mgr.on_order_event(fill)

    instr2 = TestInstrumentProvider.binary_option()
    mgr._strategy.cache.price = MagicMock(  # noqa: SLF001
        return_value=instr2.make_price(0.30),
    )
    mgr._on_timer(MagicMock())  # noqa: SLF001
    kwargs = ex.submit_virtual_exit_intent.call_args[1]
    assert kwargs["order_style"] == "market"


def test_partial_exit_fill_rearms(tmp_path: Path) -> None:
    mgr, token_id, _iid, ex = _make_manager(tmp_path)
    entry_coid = guru_client_order_id_value("g3")
    mgr.register_pending_entry(
        client_order_id=entry_coid,
        guru_correlation_id="g3",
        token_id=token_id,
    )
    instr = TestInstrumentProvider.binary_option()
    order = TestExecStubs.limit_order(
        instrument=instr,
        client_order_id=ClientOrderId(entry_coid),
        quantity=instr.make_qty(10.0),
        price=instr.make_price(0.5),
    )
    mgr.on_order_event(
        TestEventStubs.order_filled(
            order,
            instr,
            last_qty=instr.make_qty(10.0),
            last_px=instr.make_price(0.5),
        ),
    )
    instr2 = TestInstrumentProvider.binary_option()
    mgr._strategy.cache.price = MagicMock(return_value=instr2.make_price(0.60))  # noqa: SLF001
    mgr._on_timer(MagicMock())  # noqa: SLF001
    exit_coid = mgr._lots[-1].exit_client_order_id  # noqa: SLF001
    assert exit_coid

    exit_order = TestExecStubs.limit_order(
        instrument=instr,
        order_side=OrderSide.SELL,
        client_order_id=ClientOrderId(str(exit_coid)),
        quantity=instr.make_qty(10.0),
        price=instr.make_price(0.55),
    )
    mgr.on_order_event(
        TestEventStubs.order_filled(
            exit_order,
            instr,
            last_qty=instr.make_qty(4.0),
            last_px=instr.make_price(0.55),
        ),
    )
    assert mgr._lots[-1].state == "EXIT_PARTIAL"  # noqa: SLF001
    assert mgr._lots[-1].tp_armed is True  # noqa: SLF001


def test_stale_venue_hold_skips_trigger(tmp_path: Path) -> None:
    ve_rt = replace(
        VirtualExitRuntimeSettings(),
        trigger_price_source="last",
        max_venue_staleness_seconds=30.0,
    )
    mgr, token_id, _iid, ex = _make_manager(tmp_path, ve_rt=ve_rt, venue_stale=True)
    entry_coid = guru_client_order_id_value("g4")
    mgr.register_pending_entry(
        client_order_id=entry_coid,
        guru_correlation_id="g4",
        token_id=token_id,
    )
    instr = TestInstrumentProvider.binary_option()
    mgr.on_order_event(
        TestEventStubs.order_filled(
            TestExecStubs.limit_order(
                instrument=instr,
                client_order_id=ClientOrderId(entry_coid),
                quantity=instr.make_qty(10.0),
                price=instr.make_price(0.5),
            ),
            instr,
            last_qty=instr.make_qty(10.0),
            last_px=instr.make_price(0.5),
        ),
    )
    mgr._on_timer(MagicMock())  # noqa: SLF001
    ex.submit_virtual_exit_intent.assert_not_called()
    fe = mgr._emit  # noqa: SLF001
    assert fe is not None
    types = [c[0][0] for c in fe.call_args_list]
    assert "virtual_exit_hold" in types


def test_recovery_keeps_open_exit_coid(tmp_path: Path) -> None:
    mgr, token_id, iid_s, _ = _make_manager(tmp_path)
    exit_coid = "VE0123456789abcdef01234567"
    lot = ProtectedLot(
        lot_id="L-rec",
        instrument_id=iid_s,
        token_id=token_id,
        entry_client_order_id=guru_client_order_id_value("g9"),
        entry_qty_filled=10.0,
        entry_vwap=0.5,
        qty_open=10.0,
        state="EXIT_SUBMITTED",
        tp_armed=False,
        sl_armed=True,
        exit_client_order_id=exit_coid,
        exit_kind="sl",
        tp_trigger_price=0.55,
        sl_trigger_price=0.475,
    )
    mgr._store.save_lots([lot])  # noqa: SLF001

    open_o = MagicMock()
    open_o.is_closed = False
    mgr._strategy.cache.order = MagicMock(return_value=open_o)  # noqa: SLF001

    mgr._loaded = False  # noqa: SLF001
    mgr._load_once()  # noqa: SLF001
    assert mgr._lots[0].exit_client_order_id == exit_coid  # noqa: SLF001


def test_recovery_clears_closed_exit_without_double_submit(tmp_path: Path) -> None:
    mgr, token_id, iid_s, ex = _make_manager(tmp_path)
    exit_coid = "VE0123456789abcdef01234567"
    lot = ProtectedLot(
        lot_id="L-rec2",
        instrument_id=iid_s,
        token_id=token_id,
        entry_client_order_id=guru_client_order_id_value("g8"),
        entry_qty_filled=10.0,
        entry_vwap=0.5,
        qty_open=10.0,
        state="EXIT_SUBMITTED",
        tp_armed=True,
        sl_armed=True,
        exit_client_order_id=exit_coid,
        exit_kind="tp",
        tp_trigger_price=0.55,
        sl_trigger_price=0.475,
    )
    mgr._store.save_lots([lot])  # noqa: SLF001

    closed_o = MagicMock()
    closed_o.is_closed = True
    mgr._strategy.cache.order = MagicMock(return_value=closed_o)  # noqa: SLF001

    mgr._loaded = False  # noqa: SLF001
    mgr._load_once()  # noqa: SLF001
    assert mgr._lots[0].exit_client_order_id is None  # noqa: SLF001
    assert mgr._lots[0].state == "ARMED"  # noqa: SLF001

    ex.reset_mock()
    instr2 = TestInstrumentProvider.binary_option()
    mgr._strategy.cache.price = MagicMock(return_value=instr2.make_price(0.52))  # noqa: SLF001
    mgr._on_timer(MagicMock())  # noqa: SLF001
    ex.submit_virtual_exit_intent.assert_not_called()


def test_tier_a_clamp_reduces_qty_open(tmp_path: Path) -> None:
    mgr, token_id, _iid, _ex = _make_manager(tmp_path, tier_qty=6.0)
    entry_coid = guru_client_order_id_value("g5")
    mgr.register_pending_entry(
        client_order_id=entry_coid,
        guru_correlation_id="g5",
        token_id=token_id,
    )
    instr = TestInstrumentProvider.binary_option()
    mgr.on_order_event(
        TestEventStubs.order_filled(
            TestExecStubs.limit_order(
                instrument=instr,
                client_order_id=ClientOrderId(entry_coid),
                quantity=instr.make_qty(10.0),
                price=instr.make_price(0.5),
            ),
            instr,
            last_qty=instr.make_qty(10.0),
            last_px=instr.make_price(0.5),
        ),
    )
    assert mgr._lots[-1].qty_open == 10.0  # noqa: SLF001
    mgr._on_timer(MagicMock())  # noqa: SLF001
    assert mgr._lots[-1].qty_open == pytest.approx(6.0)  # noqa: SLF001


def test_virtual_exit_snapshot_not_guru_resting() -> None:
    snap = OrderSnapshot(
        client_order_id="VE" + "a" * 24,
        venue_order_id="v",
        status="OPEN",
        side="SELL",
        quantity="10",
        leaves_quantity="10",
        price="0.5",
        instrument_id="0xcond-1.POLYMARKET",
        tags=("virt_exit_lot=L1", "virt_exit_kind=sl"),
    )
    assert is_guru_resting_order(snap) is False


def test_sl_market_reject_falls_back_to_limit(tmp_path: Path) -> None:
    ve_rt = replace(
        VirtualExitRuntimeSettings(),
        trigger_price_source="last",
        max_venue_staleness_seconds=0.0,
        exit_stop_loss_style="market",
        market_sl_fallback_to_limit=True,
    )
    mgr, token_id, _iid, ex = _make_manager(tmp_path, ve_rt=ve_rt)
    entry_coid = guru_client_order_id_value("g6")
    mgr.register_pending_entry(
        client_order_id=entry_coid,
        guru_correlation_id="g6",
        token_id=token_id,
    )
    instr = TestInstrumentProvider.binary_option()
    mgr.on_order_event(
        TestEventStubs.order_filled(
            TestExecStubs.limit_order(
                instrument=instr,
                client_order_id=ClientOrderId(entry_coid),
                quantity=instr.make_qty(10.0),
                price=instr.make_price(0.5),
            ),
            instr,
            last_qty=instr.make_qty(10.0),
            last_px=instr.make_price(0.5),
        ),
    )
    instr2 = TestInstrumentProvider.binary_option()
    mgr._strategy.cache.price = MagicMock(return_value=instr2.make_price(0.20))  # noqa: SLF001
    mgr._on_timer(MagicMock())  # noqa: SLF001
    exit_coid = mgr._lots[-1].exit_client_order_id  # noqa: SLF001
    exo = TestExecStubs.limit_order(
        instrument=instr,
        order_side=OrderSide.SELL,
        client_order_id=ClientOrderId(str(exit_coid)),
        quantity=instr.make_qty(10.0),
        price=instr.make_price(0.45),
    )
    mgr.on_order_event(TestEventStubs.order_rejected(exo))
    assert ex.submit_virtual_exit_intent.call_count >= 2
    last_kw = ex.submit_virtual_exit_intent.call_args_list[-1][1]
    assert last_kw["order_style"] == "aggressive_limit"


def test_tp_trigger_fsm_terminal_states_in_lot_module() -> None:
    from tyrex_pm.runtime.virtual_exit.lot import LOT_TERMINAL_STATES

    assert "COMPLETED" in LOT_TERMINAL_STATES
    assert "FAILED" in LOT_TERMINAL_STATES
