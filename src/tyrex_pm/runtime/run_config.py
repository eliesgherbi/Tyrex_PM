"""Strict single-file configuration for the unified trading runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.protection.spec import ProtectionSpec, ProtectionSpecError, protection_spec_from_mapping
from tyrex_pm.runtime.market_family import get_market_family
from tyrex_pm.runtime.yaml_loading import load_yaml_mapping
from tyrex_pm.strategies.registry import get_strategy_plugin


class RunConfigError(ValueError):
    pass


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise RunConfigError(f"unknown {where} field(s): {', '.join(unknown)}")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise RunConfigError(f"{field} must be decimal") from exc
    if not result.is_finite():
        raise RunConfigError(f"{field} must be finite")
    return result


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RunConfigError(f"{field} must be a YAML boolean")
    return value


@dataclass(frozen=True)
class MarketRuntimeConfig:
    family: str
    binance_symbol: str
    require_ssr_price_match: bool
    preparation_lead_s: float
    maximum_duration_s: float
    evaluation_interval_s: float
    maximum_book_age_ms: int
    maximum_candidate_age_ms: int


@dataclass(frozen=True)
class RiskRuntimeConfig:
    target_notional: Decimal
    maximum_total_debit: Decimal
    fee_reserve_rate: Decimal
    kill_switch_active: bool


@dataclass(frozen=True)
class AccountRuntimeConfig:
    refresh_interval_s: float
    snapshot_max_age_s: float
    read_timeout_s: float
    retry_attempts: int
    retry_base_delay_s: float


@dataclass(frozen=True)
class LifecycleRuntimeConfig:
    mandatory_exit_before_end_s: float
    manual_deadline_before_end_s: float
    evidence_timeout_s: float
    reconciliation_interval_s: float
    exit_retry_limit: int
    exit_retry_budget_s: float
    minimum_exit_price: Decimal


@dataclass(frozen=True)
class TradingRunConfig:
    schema_version: int
    run_name: str
    strategy_kind: str
    strategy: Any
    market: MarketRuntimeConfig
    account: AccountRuntimeConfig
    risk: RiskRuntimeConfig
    lifecycle: LifecycleRuntimeConfig
    state_directory: Path
    report_directory: Path
    protection: ProtectionSpec | None


def entry_tau_bounds(config: TradingRunConfig) -> tuple[float, float]:
    plugin = get_strategy_plugin(config.strategy_kind)
    return plugin.entry_tau_bounds(config.strategy)


def max_clock_uncertainty_ms(config: TradingRunConfig) -> float:
    plugin = get_strategy_plugin(config.strategy_kind)
    return plugin.max_clock_uncertainty_ms(config.strategy)


def load_trading_run_config(path: Path) -> TradingRunConfig:
    raw = load_yaml_mapping(path)
    _reject_unknown(
        raw,
        {
            "schema_version",
            "run_name",
            "strategy",
            "market",
            "account",
            "risk",
            "lifecycle",
            "state_directory",
            "report_directory",
            "protection",
        },
        "top-level",
    )
    version = int(raw.get("schema_version", 1))
    if version != 1:
        raise RunConfigError(f"unsupported schema_version: {version}")
    run_name = str(raw.get("run_name", "z_gap_live"))
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_name) is None:
        raise RunConfigError("run_name contains unsupported characters")

    strategy_raw = dict(raw.get("strategy") or {})
    _reject_unknown(strategy_raw, {"kind", "parameters"}, "strategy")
    strategy_kind = str(strategy_raw.get("kind", "z_gap"))
    try:
        plugin = get_strategy_plugin(strategy_kind)
    except KeyError as exc:
        raise RunConfigError(str(exc)) from exc
    try:
        strategy = plugin.load_config(strategy_raw.get("parameters") or {}, str(path))
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, RunConfigError):
            raise
        raise RunConfigError(str(exc)) from exc

    protection: ProtectionSpec | None = None
    if "protection" in raw:
        if raw["protection"] is None:
            raise RunConfigError("protection must be a mapping when present")
        try:
            protection = protection_spec_from_mapping(dict(raw["protection"]), where="protection")
        except ProtectionSpecError as exc:
            raise RunConfigError(str(exc)) from exc
    if plugin.requires_protection and protection is None:
        raise RunConfigError(f"{strategy_kind} requires an explicit top-level protection block")

    market_raw = dict(raw.get("market") or {})
    _reject_unknown(
        market_raw,
        {
            "family",
            "binance_symbol",
            "require_ssr_price_match",
            "preparation_lead_s",
            "maximum_duration_s",
            "evaluation_interval_s",
            "maximum_book_age_ms",
            "maximum_candidate_age_ms",
        },
        "market",
    )
    market = MarketRuntimeConfig(
        family=str(market_raw.get("family", "btc_updown_5m")),
        binance_symbol=str(market_raw.get("binance_symbol", "BTCUSDT")),
        require_ssr_price_match=_boolean(
            market_raw.get("require_ssr_price_match", False),
            "market.require_ssr_price_match",
        ),
        preparation_lead_s=float(market_raw.get("preparation_lead_s", 45.0)),
        maximum_duration_s=float(market_raw.get("maximum_duration_s", 300.0)),
        evaluation_interval_s=float(market_raw.get("evaluation_interval_s", 1.0)),
        maximum_book_age_ms=int(market_raw.get("maximum_book_age_ms", 2_000)),
        maximum_candidate_age_ms=int(market_raw.get("maximum_candidate_age_ms", 5_000)),
    )
    try:
        get_market_family(market.family)
    except Exception as exc:  # noqa: BLE001
        raise RunConfigError(str(exc)) from exc
    if (
        min(
            market.preparation_lead_s,
            market.maximum_duration_s,
            market.evaluation_interval_s,
            market.maximum_book_age_ms,
            market.maximum_candidate_age_ms,
        )
        <= 0
    ):
        raise RunConfigError("market timing and freshness values must be positive")

    account_raw = dict(raw.get("account") or {})
    _reject_unknown(
        account_raw,
        {
            "refresh_interval_s",
            "snapshot_max_age_s",
            "read_timeout_s",
            "retry_attempts",
            "retry_base_delay_s",
        },
        "account",
    )
    account = AccountRuntimeConfig(
        refresh_interval_s=float(account_raw.get("refresh_interval_s", 5.0)),
        snapshot_max_age_s=float(account_raw.get("snapshot_max_age_s", 15.0)),
        read_timeout_s=float(account_raw.get("read_timeout_s", 4.0)),
        retry_attempts=int(account_raw.get("retry_attempts", 3)),
        retry_base_delay_s=float(account_raw.get("retry_base_delay_s", 0.25)),
    )
    if min(
        account.refresh_interval_s,
        account.snapshot_max_age_s,
        account.read_timeout_s,
        account.retry_attempts,
        account.retry_base_delay_s,
    ) <= 0:
        raise RunConfigError(
            "account refresh, freshness, timeout, and retry values must be positive"
        )

    risk_raw = dict(raw.get("risk") or {})
    _reject_unknown(
        risk_raw,
        {"target_notional", "maximum_total_debit", "fee_reserve_rate", "kill_switch_active"},
        "risk",
    )
    risk = RiskRuntimeConfig(
        target_notional=_decimal(risk_raw.get("target_notional", "5"), "target_notional"),
        maximum_total_debit=_decimal(
            risk_raw.get("maximum_total_debit", "5"), "maximum_total_debit"
        ),
        fee_reserve_rate=_decimal(risk_raw.get("fee_reserve_rate", "0.02"), "fee_reserve_rate"),
        kill_switch_active=_boolean(
            risk_raw.get("kill_switch_active", False), "risk.kill_switch_active"
        ),
    )
    if risk.target_notional <= 0 or risk.maximum_total_debit <= 0:
        raise RunConfigError("risk notionals must be positive")
    if risk.maximum_total_debit > Decimal("5"):
        raise RunConfigError("tiny-live maximum_total_debit cannot exceed 5 USDC")
    if risk.fee_reserve_rate < 0:
        raise RunConfigError("fee_reserve_rate cannot be negative")
    if plugin.validate_run_config is not None:
        try:
            plugin.validate_run_config(strategy)
        except Exception as exc:  # noqa: BLE001
            raise RunConfigError(str(exc)) from exc

    life_raw = dict(raw.get("lifecycle") or {})
    _reject_unknown(
        life_raw,
        {
            "mandatory_exit_before_end_s",
            "manual_deadline_before_end_s",
            "evidence_timeout_s",
            "reconciliation_interval_s",
            "exit_retry_limit",
            "exit_retry_budget_s",
            "minimum_exit_price",
        },
        "lifecycle",
    )
    lifecycle = LifecycleRuntimeConfig(
        mandatory_exit_before_end_s=float(life_raw.get("mandatory_exit_before_end_s", 90.0)),
        manual_deadline_before_end_s=float(life_raw.get("manual_deadline_before_end_s", 45.0)),
        evidence_timeout_s=float(life_raw.get("evidence_timeout_s", 15.0)),
        reconciliation_interval_s=float(life_raw.get("reconciliation_interval_s", 0.25)),
        exit_retry_limit=int(life_raw.get("exit_retry_limit", 3)),
        exit_retry_budget_s=float(life_raw.get("exit_retry_budget_s", 30.0)),
        minimum_exit_price=_decimal(
            life_raw.get("minimum_exit_price", "0.01"), "minimum_exit_price"
        ),
    )
    if lifecycle.mandatory_exit_before_end_s <= lifecycle.manual_deadline_before_end_s:
        raise RunConfigError("mandatory exit must begin before the manual deadline")
    if (
        lifecycle.exit_retry_limit < 1
        or lifecycle.reconciliation_interval_s <= 0
        or lifecycle.evidence_timeout_s <= 0
        or lifecycle.exit_retry_budget_s <= 0
        or lifecycle.manual_deadline_before_end_s < 0
        or lifecycle.minimum_exit_price <= 0
        or lifecycle.minimum_exit_price > 1
    ):
        raise RunConfigError("lifecycle retry settings must be positive")
    return TradingRunConfig(
        schema_version=version,
        run_name=run_name,
        strategy_kind=strategy_kind,
        strategy=strategy,
        market=market,
        account=account,
        risk=risk,
        lifecycle=lifecycle,
        state_directory=Path(raw.get("state_directory", "var/runtime_state/execution")),
        report_directory=Path(raw.get("report_directory", "var/runs/z_gap")),
        protection=protection,
    )
