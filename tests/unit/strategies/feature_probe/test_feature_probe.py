"""feature_probe plugin: contract closure, evaluate seam, emit_entry gate."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, MarketId, RunId, TokenId
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.intents import EnterIntent, LiquidityRole
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.facts import ids as F
from tyrex_pm.facts.mappings import FEATURE_PROBE_IMBALANCE, FEATURE_PROBE_INPUT_CONTRACT
from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.indicators.spec import IndicatorSpec
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.runtime.composition import missing_live_adapters, starts_adapter
from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.feature_probe.config import FeatureProbeConfig
from tyrex_pm.strategies.feature_probe.driver import create_feature_probe_driver
from tyrex_pm.strategies.feature_probe.reasons import FeatureProbeReason
from tyrex_pm.strategies.registry import (
    get_strategy_plugin,
    plugin_indicator_specs,
    registered_strategy_kinds,
)


def _fresh() -> FreshnessAssessment:
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    return FreshnessAssessment(
        is_fresh=True,
        age_ms=0,
        threshold_ms=5_000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH,
        observed_at=now,
    )


def _quote(ask: Decimal | None) -> ExecutableQuote:
    return ExecutableQuote(
        best_ask=ask,
        best_bid=None,
        mid=None,
        spread=None,
        ask_size_at_touch=Decimal("10") if ask is not None else Decimal("0"),
        bid_size_at_touch=Decimal("0"),
    )


def _market() -> BinaryMarket:
    yes = Instrument(
        market_id=MarketId("m1"),
        token_id=TokenId("yes"),
        instrument_id=InstrumentId("yes"),
        outcome=OutcomeSide.YES,
    )
    no = Instrument(
        market_id=MarketId("m1"),
        token_id=TokenId("no"),
        instrument_id=InstrumentId("no"),
        outcome=OutcomeSide.NO,
    )
    return BinaryMarket(
        market_id=MarketId("m1"),
        condition_id="condition-1",
        question="feature_probe",
        yes=yes,
        no=no,
        event_start=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
        event_end=datetime(2026, 9, 13, 12, 5, tzinfo=timezone.utc),
    )


def _snapshot(
    *,
    up_ask: Decimal | None = Decimal("0.71"),
    down_ask: Decimal | None = Decimal("0.30"),
):
    now = datetime(2026, 9, 13, 12, 1, tzinfo=timezone.utc)
    return DecisionSnapshot(
        market=_market(),
        yes_book=None,
        no_book=None,
        yes_quote=_quote(up_ask),
        no_quote=_quote(down_ask),
        reference=None,
        yes_freshness=_fresh(),
        no_freshness=_fresh(),
        reference_freshness=_fresh(),
        observed_at=now,
        correlation_id=CorrelationId("c1"),
        causation_id=EventId("e1"),
    )


def _imbalance_bundle(value: Decimal | None = Decimal("0.25")) -> FeatureBundle:
    spec = FEATURE_PROBE_IMBALANCE
    assert isinstance(spec, IndicatorSpec)
    key = f"{spec.instance_id}|1"
    return FeatureBundle(values={key: value})


def _config(*, emit_entry: bool) -> FeatureProbeConfig:
    return FeatureProbeConfig(
        emit_entry=emit_entry,
        tau_min_s=10.0,
        tau_max_s=290.0,
        max_clock_uncertainty_ms=750.0,
    )


def _evaluate(
    config: FeatureProbeConfig,
    *,
    features: FeatureBundle,
    snap: DecisionSnapshot | None = None,
):
    driver = create_feature_probe_driver(
        config=config,
        clock=FakeClock(_wall=datetime(2026, 9, 13, 12, 1, tzinfo=timezone.utc)),
    )
    snap = snap or _snapshot()
    ctx = DecisionContext(
        run_id=RunId("fp"),
        snapshot=snap,
        target_notional=Decimal("5"),
        now=snap.observed_at,
        entry_allowed=True,
    )
    return driver.evaluate(
        market_snapshot=snap,
        causation_id=snap.causation_id,
        correlation_id=snap.correlation_id,
        context=ctx,
        trigger="feed",
        features=features,
    )


def test_feature_probe_kind_loads_and_unknown_still_fails(tmp_path: Path) -> None:
    cfg = load_trading_run_config(Path("config/runs/feature_probe_catalog.yaml"))
    assert cfg.strategy_kind == "feature_probe"
    assert cfg.strategy.emit_entry is False
    assert cfg.protection is None
    raw = Path("config/runs/feature_probe_catalog.yaml").read_text(encoding="utf-8")
    tmp = tmp_path / "unknown.yaml"
    tmp.write_text(raw.replace("kind: feature_probe", "kind: not_a_strategy"), encoding="utf-8")
    with pytest.raises(RunConfigError, match="not registered"):
        load_trading_run_config(tmp)


def test_registered_kinds_are_ask70_feature_probe_z_gap() -> None:
    assert registered_strategy_kinds() == ("ask70", "feature_probe", "z_gap")
    assert "q_edge" not in registered_strategy_kinds()


def test_feature_probe_starts_spot_l2_not_chainlink_or_perp() -> None:
    contract = FEATURE_PROBE_INPUT_CONTRACT
    assert starts_adapter(contract, F.BINANCE_SPOT_L2)
    assert starts_adapter(contract, F.POLYMARKET_BOOKS)
    assert not starts_adapter(contract, F.CHAINLINK_TWAP)
    assert not starts_adapter(contract, F.BINANCE_SPOT_TRADES)
    assert not starts_adapter(contract, F.BINANCE_PERP_L2)
    assert not starts_adapter(contract, F.BINANCE_PERP_TRADES)
    assert missing_live_adapters(contract) == frozenset()
    plugin = get_strategy_plugin("feature_probe")
    specs = plugin_indicator_specs(plugin, _config(emit_entry=False))
    assert len(specs) == 1
    assert specs[0].name == "imbalance"
    assert specs[0].source == F.BINANCE_SPOT_L2


def test_ask70_still_starts_no_binance() -> None:
    ask70 = get_strategy_plugin("ask70").input_contract
    assert not starts_adapter(ask70, F.BINANCE_SPOT_L2)
    assert not starts_adapter(ask70, F.BINANCE_SPOT_TRADES)


def test_empty_bundle_sets_feature_not_ready() -> None:
    result = _evaluate(_config(emit_entry=False), features=FeatureBundle())
    assert result.eligibility.strategy_inputs_eligible is False
    assert FeatureProbeReason.FEATURE_NOT_READY.value in result.eligibility.blockers
    assert result.intents == ()
    assert result.decision.action is StrategyAction.SKIP


def test_bundle_with_imbalance_skips_and_records_evidence() -> None:
    result = _evaluate(_config(emit_entry=False), features=_imbalance_bundle())
    assert result.eligibility.strategy_inputs_eligible is True
    assert result.intents == ()
    assert result.decision.action is StrategyAction.SKIP
    assert result.decision.reason_code == FeatureProbeReason.FEATURE_OBSERVED.value
    assert result.decision.evidence["imbalance"] == "0.25"


def test_emit_entry_true_without_protection_fails_load(tmp_path: Path) -> None:
    text = Path("config/runs/feature_probe_catalog.yaml").read_text(encoding="utf-8")
    path = tmp_path / "emit.yaml"
    path.write_text(text.replace("emit_entry: false", "emit_entry: true"), encoding="utf-8")
    with pytest.raises(RunConfigError, match="requires an explicit top-level protection block"):
        load_trading_run_config(path)


def test_emit_entry_true_with_protection_can_enter(tmp_path: Path) -> None:
    catalog = Path("config/runs/feature_probe_catalog.yaml").read_text(encoding="utf-8")
    protection = """
protection:
  mark: bid
  size_mode: full
  take_profit:
    style: reactive_fak
    absolute: "0.85"
  stop_loss:
    style: stop_market_fak
    absolute: "0.55"
  trailing:
    enabled: false
    activation: immediate
    mark: bid
"""
    body = catalog.replace("emit_entry: false", "emit_entry: true")
    body = body.replace("\nmarket:", protection + "\nmarket:")
    path = tmp_path / "emit_protected.yaml"
    path.write_text(body, encoding="utf-8")
    cfg = load_trading_run_config(path)
    assert cfg.strategy.emit_entry is True
    assert cfg.protection is not None
    result = _evaluate(cfg.strategy, features=_imbalance_bundle())
    assert result.intents
    assert isinstance(result.intents[0], EnterIntent)
    assert result.intents[0].liquidity_role is LiquidityRole.TAKER
    assert result.decision.action is StrategyAction.ENTER
