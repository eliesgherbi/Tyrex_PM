"""Phase A closure: pending leaves, position reader, capital gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Money

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.state_readers import (
    AccountSnapshot,
    AllowanceSnapshot,
    NautilusPositionStateReader,
    instrument_id_for_outcome_token,
)


def _risk(**over: object) -> RiskSettings:
    base: dict[str, object] = {
        "max_order_quantity": 100.0,
        "max_notional_usd_per_order": 1000.0,
        "max_token_notional_usd_open": 100.0,
        "kill_switch": False,
        "fail_on_missing_price_for_notional": True,
        "capital_gate_enabled": False,
        "max_account_snapshot_age_seconds": 30.0,
        "max_allowance_snapshot_age_seconds": 120.0,
        "min_collateral_balance_usd": None,
        "min_allowance_usd": None,
        "fail_on_unresolved_position_for_token_cap": False,
        "max_portfolio_notional_usd_open": float("inf"),
        "fail_on_unresolved_portfolio_exposure": True,
        "max_concurrent_guru_resting_orders": None,
        "collateral_reserve_usd": 0.0,
    }
    base.update(over)
    return RiskSettings(**base)  # type: ignore[arg-type]


_d_intent = OrderIntent(
    correlation_id="x",
    token_id="88888",
    side="BUY",
    quantity=5.0,
    signal_kind="entry",
    reason_code="ok",
    price_ref=0.5,
)


def test_instrument_id_resolve_static_map() -> None:
    cache = MagicMock()
    cache.instruments.return_value = ()
    iid = instrument_id_for_outcome_token(
        cache,
        "88888",
        static_token_to_instrument={"88888": "0xabc-88888.POLYMARKET"},
    )
    assert iid == InstrumentId.from_str("0xabc-88888.POLYMARKET")


def test_pending_zero_when_orders_open_empty() -> None:
    reader = MagicMock()
    reader.list_open_orders.return_value = ()
    pol = ConfiguredRiskPolicy(
        _risk(max_token_notional_usd_open=10.0),
        execution_reader=reader,
        token_open_authoritative_for_pending=False,
    )
    ok, _ = pol.evaluate(_d_intent)
    assert ok is True


def test_capital_gate_denies_missing_account_present() -> None:
    acct = MagicMock()

    def snap_off() -> AccountSnapshot:
        return AccountSnapshot(
            venue="POLYMARKET",
            captured_at_utc=datetime.now(tz=UTC),
            account_present=False,
            balances=None,
            raw_summary=None,
        )

    acct.snapshot.return_value = snap_off()
    pol = ConfiguredRiskPolicy(
        _risk(capital_gate_enabled=True),
        account_snapshot=acct,
        allowance_provider=MagicMock(),
    )
    ok, rc = pol.evaluate(_d_intent)
    assert ok is False
    assert rc == ReasonCode.RISK_ACCOUNT_UNAVAILABLE


def test_capital_gate_collateral_below_min() -> None:
    acct = MagicMock()

    def snap_on() -> AccountSnapshot:
        return AccountSnapshot(
            venue="POLYMARKET",
            captured_at_utc=datetime.now(tz=UTC),
            account_present=True,
            balances={"type": "stub"},
            raw_summary=None,
        )

    acct.snapshot.return_value = snap_on()
    allow = MagicMock()
    allow.snapshot.return_value = AllowanceSnapshot(
        captured_at_utc=datetime.now(tz=UTC),
        raw={"balance": "0.5", "allowance": "999999"},
    )
    pol = ConfiguredRiskPolicy(
        _risk(
            capital_gate_enabled=True,
            fail_on_missing_price_for_notional=True,
            min_collateral_balance_usd=1.0,
        ),
        account_snapshot=acct,
        allowance_provider=allow,
    )
    ok, rc = pol.evaluate(_d_intent)
    assert ok is False
    assert rc == ReasonCode.RISK_INSUFFICIENT_COLLATERAL_BALANCE


def test_capital_gate_allowance_below_min() -> None:
    acct = MagicMock()
    acct.snapshot.return_value = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={},
        raw_summary=None,
    )
    allow = MagicMock()
    allow.snapshot.return_value = AllowanceSnapshot(
        captured_at_utc=datetime.now(tz=UTC),
        raw={"balance": "100", "allowance": "0"},
    )
    pol = ConfiguredRiskPolicy(
        _risk(
            capital_gate_enabled=True,
            min_allowance_usd=10.0,
        ),
        account_snapshot=acct,
        allowance_provider=allow,
    )
    ok, rc = pol.evaluate(_d_intent)
    assert ok is False
    assert rc == ReasonCode.RISK_INSUFFICIENT_ALLOWANCE


def test_capital_gate_reuses_cached_allowance_within_age() -> None:
    acct = MagicMock()
    acct.snapshot.return_value = AccountSnapshot(
        venue="POLYMARKET",
        captured_at_utc=datetime.now(tz=UTC),
        account_present=True,
        balances={},
        raw_summary=None,
    )
    allow = MagicMock()
    old = datetime.now(tz=UTC) - timedelta(seconds=5)
    snap = AllowanceSnapshot(
        captured_at_utc=old,
        raw={"balance": "100", "allowance": "100"},
    )
    allow.snapshot.return_value = snap
    pol = ConfiguredRiskPolicy(
        _risk(
            capital_gate_enabled=True,
            min_collateral_balance_usd=1.0,
            max_allowance_snapshot_age_seconds=60.0,
        ),
        account_snapshot=acct,
        allowance_provider=allow,
    )
    pol.evaluate(_d_intent)
    pol.evaluate(_d_intent)
    assert allow.snapshot.call_count == 1


def test_position_reader_increases_token_cap_usage() -> None:
    reader = MagicMock()
    reader.list_open_orders.return_value = ()
    pr = MagicMock()
    pr.filled_exposure_usd_best_effort.return_value = 80.0
    pol = ConfiguredRiskPolicy(
        _risk(max_token_notional_usd_open=100.0),
        execution_reader=reader,
        position_reader=pr,
        token_open_authoritative_for_pending=False,
    )
    ok, _ = pol.evaluate(_d_intent)
    assert ok is True
    pr.filled_exposure_usd_best_effort.return_value = 99.0
    ok2, rc = pol.evaluate(_d_intent)
    assert ok2 is False
    assert rc == ReasonCode.RISK_TOKEN_NOTIONAL_OPEN


def test_fail_on_unresolved_position() -> None:
    reader = MagicMock()
    reader.list_open_orders.return_value = ()
    pr = MagicMock()
    pr.filled_exposure_usd_best_effort.return_value = None
    pol = ConfiguredRiskPolicy(
        _risk(
            max_token_notional_usd_open=100.0,
            fail_on_unresolved_position_for_token_cap=True,
        ),
        execution_reader=reader,
        position_reader=pr,
        token_open_authoritative_for_pending=False,
    )
    ok, rc = pol.evaluate(_d_intent)
    assert ok is False
    assert rc == ReasonCode.RISK_POSITION_EXPOSURE_UNRESOLVED


def test_nautilus_position_reader_net_exposure() -> None:
    iid = InstrumentId.from_str("0xabc-88888.POLYMARKET")
    inst = MagicMock()
    inst.make_price.return_value = MagicMock()
    cache = MagicMock()
    cache.instrument.return_value = inst
    cache.instruments.return_value = (inst,)
    inst.id = iid
    portfolio = MagicMock()
    portfolio.net_exposure.return_value = Money(Decimal("7.5"), USDC)
    pr = NautilusPositionStateReader(
        portfolio,
        cache,
        {"88888": str(iid)},
    )
    got = pr.filled_exposure_usd_best_effort("88888", 0.5)
    assert got == 7.5
