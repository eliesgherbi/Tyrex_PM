"""Phase 1 (architecture_enhance): generic signal/strategy dispatch.

Verifies guru runs through the same ``process_signals`` path as a non-guru
source, that facts for guru are unchanged, and that a ``simple_signal_test``
signal produces an ``EnterIntent`` and a ``signal_received`` fact.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import GuruTradeSignal
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_GURU_SIGNAL,
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RISK,
    FACT_TYPE_SIGNAL_RECEIVED,
    FACT_TYPE_STRATEGY_SKIP,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.allocation_ids import OWNER_SIMPLE_SIGNAL_TEST
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_new_guru_signals, process_signals
from tyrex_pm.signals.guru_copy_signal import to_copy_signal
from tyrex_pm.signals.simple_signal import SimpleSignal
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.strategies.simple_signal_test.strategy import SimpleSignalTestStrategy

_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "500", "portfolio_cap_usd": "5000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False, "max_wallet_age_s": 120},
    "concurrency": {"max_orders_in_flight": 8},
    "readiness": {
        "require_wallet_sync": False,
        "max_wallet_age_s_live": 120,
        "require_heartbeat_live": False,
        "require_user_ws_live": False,
    },
}

_RUNTIME = {
    "execution_mode": "shadow",
    "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
    "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
    "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
    "logging": {"level": "WARNING"},
}

TOKEN = TokenId("token-x")


def _guru_strategy_dict() -> dict:
    return {
        "kind": "guru_follow",
        "guru": {"wallet": "0xg"},
        "filters": {},
        "sizing": {"static_enabled": True, "static_amount_usd": "5", "copy_scale": "1"},
        "exits": {},
    }


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    return coord


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _guru_buy(dedup: str = "g1", price: Decimal | None = Decimal("0.5")) -> GuruTradeSignal:
    return GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("10"),
        price=price,
        notional_usd=Decimal("5"),
        dedup_key=dedup,
        ts_venue=None,
        conviction_score=None,
    )


def _run_guru(tmp_path: Path, sigs: list[GuruTradeSignal]) -> list[dict]:
    app = parse_app_config(risk=dict(_RISK), strategy=_guru_strategy_dict(), runtime=dict(_RUNTIME))
    strat = GuruFollowStrategy(app.strategy)
    coord = _coord(tmp_path)
    facts = tmp_path / f"facts-{uuid4()}.jsonl"
    with JsonlSink(facts) as sink:
        asyncio.run(
            process_new_guru_signals(
                sigs,
                app=app,
                run_id=RunId(str(uuid4())),
                strategy=strat,
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
            )
        )
    return _read(facts)


def _simple_signal(side: Side = Side.BUY, dedup: str = "s1") -> SimpleSignal:
    return SimpleSignal(
        token_id=TOKEN,
        side=side,
        order_style=OrderStyle.GTC,
        dedup_key=dedup,
        notional_usd=Decimal("5"),
        limit_price=Decimal("0.5"),
    )


def _run_simple(tmp_path: Path, signals: list[SimpleSignal]) -> list[dict]:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime=dict(_RUNTIME),
    )
    strat = SimpleSignalTestStrategy()
    coord = _coord(tmp_path)
    facts = tmp_path / f"facts-{uuid4()}.jsonl"
    with JsonlSink(facts) as sink:
        asyncio.run(
            process_signals(
                signals,
                app=app,
                run_id=RunId(str(uuid4())),
                strategy=strat,
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
            )
        )
    return _read(facts)


def test_guru_signal_uses_generic_dispatch(tmp_path: Path) -> None:
    # GuruFollowStrategy implements the generic contract and the guru path
    # produces intents through the shared dispatch.
    assert hasattr(GuruFollowStrategy, "on_signal")
    rows = _run_guru(tmp_path, [_guru_buy()])
    types = [r["fact_type"] for r in rows]
    assert FACT_TYPE_INTENT in types
    assert FACT_TYPE_RISK in types
    assert FACT_TYPE_OMS_SUBMIT in types


def test_guru_signal_fact_unchanged(tmp_path: Path) -> None:
    rows = _run_guru(tmp_path, [_guru_buy(dedup="gx")])
    guru = [r for r in rows if r["fact_type"] == FACT_TYPE_GURU_SIGNAL]
    assert len(guru) == 1
    payload = guru[0]["payload"]
    assert set(payload.keys()) == {
        "dedup_key",
        "guru_wallet",
        "token_id",
        "side",
        "size",
        "price",
        "notional_usd",
        "conviction_score",
    }
    assert guru[0]["correlation_id"] == "gx"
    # Guru must NOT emit the generic signal_received fact (backward compatibility).
    assert all(r["fact_type"] != FACT_TYPE_SIGNAL_RECEIVED for r in rows)


def test_intent_created_fact_unchanged_for_guru(tmp_path: Path) -> None:
    rows = _run_guru(tmp_path, [_guru_buy()])
    intents = [r for r in rows if r["fact_type"] == FACT_TYPE_INTENT]
    assert len(intents) == 1
    payload = intents[0]["payload"]
    assert payload["kind"] == "EnterIntent"
    assert payload["side"] == "BUY"
    assert payload["order_style"] == "GTC"
    assert Decimal(payload["size"]) == Decimal("10")
    # guru sizing meta is still merged into the intent fact.
    assert payload["sizing_mode"] == "static"


def test_strategy_skip_still_emits_same_reason_codes(tmp_path: Path) -> None:
    from tyrex_pm.core import reason_codes as rc

    # Guru BUY with no price → sizing returns None → GURU_PRICE_REQUIRED skip.
    rows = _run_guru(tmp_path, [_guru_buy(dedup="noprice", price=None)])
    skips = [r for r in rows if r["fact_type"] == FACT_TYPE_STRATEGY_SKIP]
    assert any(s["payload"]["reason"] == rc.GURU_PRICE_REQUIRED for s in skips)
    assert all(r["fact_type"] != FACT_TYPE_INTENT for r in rows)


def test_simple_signal_test_emits_intent_through_process_signals(tmp_path: Path) -> None:
    rows = _run_simple(tmp_path, [_simple_signal()])
    types = [r["fact_type"] for r in rows]
    assert types.count(FACT_TYPE_INTENT) == 1
    intent = [r for r in rows if r["fact_type"] == FACT_TYPE_INTENT][0]
    assert intent["payload"]["kind"] == "EnterIntent"
    assert intent["payload"]["side"] == "BUY"
    assert Decimal(intent["payload"]["size"]) == Decimal("10")  # 5 / 0.5
    risk = [r for r in rows if r["fact_type"] == FACT_TYPE_RISK][0]
    assert risk["payload"]["approved"] is True
    assert FACT_TYPE_OMS_SUBMIT in types


def test_simple_signal_test_emits_signal_received_fact(tmp_path: Path) -> None:
    rows = _run_simple(tmp_path, [_simple_signal(dedup="sigr")])
    received = [r for r in rows if r["fact_type"] == FACT_TYPE_SIGNAL_RECEIVED]
    assert len(received) == 1
    payload = received[0]["payload"]
    assert payload["source"] == "simple_signal_test"
    assert payload["token_id"] == str(TOKEN)
    assert payload["side"] == "BUY"
    assert received[0]["correlation_id"] == "sigr"


def test_simple_signal_test_sell_clamps_to_allocation(tmp_path: Path) -> None:
    from tyrex_pm.core import reason_codes as rc

    app = parse_app_config(
        risk=dict(_RISK),
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime=dict(_RUNTIME),
    )
    strat = SimpleSignalTestStrategy()
    coord = _coord(tmp_path)
    # No allocation → SELL must be skipped (naked sell), not pushed to OMS.
    facts = tmp_path / f"facts-{uuid4()}.jsonl"
    with JsonlSink(facts) as sink:
        asyncio.run(
            process_signals(
                [_simple_signal(side=Side.SELL, dedup="sell-noalloc")],
                app=app,
                run_id=RunId(str(uuid4())),
                strategy=strat,
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
            )
        )
    rows = _read(facts)
    skips = [r for r in rows if r["fact_type"] == FACT_TYPE_STRATEGY_SKIP]
    assert any(s["payload"]["reason"] == rc.NAKED_SELL for s in skips)
    assert all(r["fact_type"] != FACT_TYPE_OMS_SUBMIT for r in rows)

    # With allocation, SELL clamps to the available allocated qty.
    coord.allocation_ledger.apply_buy(OWNER_SIMPLE_SIGNAL_TEST, TOKEN, Decimal("3"))
    from tyrex_pm.core.models import WalletPosition

    coord.wallet.positions[TOKEN] = WalletPosition(
        token_id=TOKEN, qty=Decimal("100"), avg_price_usd=Decimal("0.5")
    )
    strat2 = SimpleSignalTestStrategy()
    from tyrex_pm.strategies.base import StrategyContext

    result = strat2.on_signal(
        _simple_signal(side=Side.SELL, dedup="sell-alloc"),
        StrategyContext(coord=coord),
    )
    assert result.skip_reason is None
    assert len(result.intents) == 1
    assert result.intents[0].size == Decimal("3")


def test_simple_signal_test_config_loads() -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={
            "kind": "simple_signal_test",
            "token_id": "tok-1",
            "side": "BUY",
            "notional_usd": "5",
            "limit_price": "0.5",
        },
        runtime=dict(_RUNTIME),
    )
    assert app.simple_signal_test is not None
    assert app.simple_signal_test.token_id == "tok-1"
    assert app.simple_signal_test.side == Side.BUY


def test_no_strategy_imports_venue_or_mutates_state() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "tyrex_pm" / "strategies"
    forbidden_import = "from tyrex_pm.venue"
    forbidden_mutations = (".apply_buy(", ".apply_sell(", ".reserve_exit(", ".submit(", ".cancel(")
    for mod in (
        root / "simple_signal_test" / "strategy.py",
        root / "guru_follow" / "strategy.py",
        root / "base.py",
    ):
        src = mod.read_text(encoding="utf-8")
        assert forbidden_import not in src, f"{mod} imports venue clients"
        for bad in forbidden_mutations:
            assert bad not in src, f"{mod} performs forbidden mutation {bad}"


def test_legacy_harnesses_still_run() -> None:
    # sell_test / allocation_test / tp_sl_test remain importable and functional
    # on their existing loops (not migrated in Phase 1).
    from tyrex_pm.strategies.sell_test.strategy import SellTestStrategy
    from tyrex_pm.strategies.allocation_test.strategy import AllocationTestStrategy  # noqa: F401
    from tyrex_pm.strategies.tp_sl_test.strategy import TpSlTestStrategy  # noqa: F401
    from tyrex_pm.runtime.config import (
        SellTestBuyConfig,
        SellTestSellConfig,
        SellTestStrategyConfig,
    )

    cfg = SellTestStrategyConfig(
        enabled=True,
        token_id="tok-legacy",
        buy=SellTestBuyConfig(
            enabled=True,
            notional_usd=Decimal("5"),
            limit_price=Decimal("0.5"),
            order_style=OrderStyle.GTC,
        ),
        sell=SellTestSellConfig(
            enabled=True, delay_s=0.0, order_style=OrderStyle.GTC, limit_price=None
        ),
        run_once=True,
    )
    strat = SellTestStrategy(cfg)
    work = strat.initial_buy_work_units()
    assert len(work) == 1
    assert work[0].intent.side == Side.BUY
