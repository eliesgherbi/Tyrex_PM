"""Strict single-file configuration for the unified trading runtime."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.execution.settlement.governor import RunLimits, TrialLimits
from tyrex_pm.protection.spec import (
    ProtectionSpec,
    ProtectionSpecError,
    protection_spec_from_mapping,
)
from tyrex_pm.recording.config import RecordingConfig, recording_config_from_mapping
from tyrex_pm.runtime.market_family import get_market_family
from tyrex_pm.runtime.yaml_loading import load_yaml_mapping
from tyrex_pm.strategies.registry import (
    get_strategy_plugin,
    plugin_lifecycle_modes,
    plugin_requires_protection,
)


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


RUN_SINGLE_WINDOW = "single_window"
RUN_CONTINUOUS = "continuous"
RUN_MODES = frozenset({RUN_SINGLE_WINDOW, RUN_CONTINUOUS})


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
    entry_dispatch_margin_s: float
    # single_window: one trading window per process (the S6 trial shape).
    # continuous: roll through consecutive windows until stopped; maximum_duration_s,
    # when given explicitly, bounds a soak; it is never an implied "forever".
    run_mode: str = "single_window"
    maximum_duration_explicit: bool = True


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
    # Account refreshes and session reconciliation use their own SDK clients, so
    # their reads never delay order signing/POST (false: the earlier shared client
    # with the refresh wait, kept as a rollback).
    isolated_read_clients: bool = True


@dataclass(frozen=True)
class LifecycleRuntimeConfig:
    mandatory_exit_before_end_s: float
    manual_deadline_before_end_s: float
    evidence_timeout_s: float
    reconciliation_interval_s: float
    exit_retry_limit: int
    exit_retry_budget_s: float
    minimum_exit_price: Decimal
    sell_size_increment: Decimal
    dust_abs_shares: Decimal
    dust_max_notional: Decimal
    prepare_budget_ms: float
    refresh_wait_budget_ms: float
    # Optional: a sign that waited longer than this for an account refresh is refused
    # (never sent) so the strategy re-evaluates with a fresh intent. None: off.
    refresh_revalidate_after_ms: float | None = None


@dataclass(frozen=True)
class StartupRuntimeConfig:
    inspection_budget_s: float = 60
    prompt_timeout_s: float = 120
    monitor_interval_s: float = 15


LIFECYCLE_FLATTEN = "flatten_before_close"
LIFECYCLE_HOLD = "hold_to_resolution"
LIFECYCLE_MANAGED = "managed_exit"
LIFECYCLE_MODES = frozenset({LIFECYCLE_FLATTEN, LIFECYCLE_HOLD, LIFECYCLE_MANAGED})
SETTLEMENT_MODES = frozenset({LIFECYCLE_HOLD, LIFECYCLE_MANAGED})
VENUES = frozenset({"live", "paper"})


@dataclass(frozen=True)
class PaperRuntimeConfig:
    """Paper venue: live public data, simulated account/venue, no signing or credentials."""

    initial_collateral: Decimal
    # Venue latency applied before a simulated order meets the live book [ms].
    order_latency_ms: int = 300
    # Fraction of displayed depth a simulated taker order may consume (0, 1].
    participation: Decimal = Decimal("1")
    # Resolution of paper holdings: public read-only chain payout of the real market.
    resolution_source: str = "chain_read"
    # Simulated operator redemption: credit a proven payout after this delay; None keeps
    # redemption a manual step as in live.
    auto_redeem_after_s: float | None = None


@dataclass(frozen=True)
class SettlementRuntimeConfig:
    """Hold-to-resolution profile: durable ledger, trial limits, operator redemption."""

    ledger_path: Path
    trial: TrialLimits
    hold_commit_budget_s: float
    redemption_wallet: str | None
    redemption_mode: str = "operator"
    run_limits: RunLimits | None = None
    # Automatic resolution/payout reconciliation cadence of the runner [s].
    resolution_poll_interval_s: float = 30.0


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
    startup: StartupRuntimeConfig = StartupRuntimeConfig()
    lifecycle_mode: str = LIFECYCLE_FLATTEN
    settlement: SettlementRuntimeConfig | None = None
    venue: str = "live"
    paper: PaperRuntimeConfig | None = None
    # Decision diagnostics / explanations / raw capture (tyrex_pm.recording).
    recording: RecordingConfig = RecordingConfig()


RUN_LIMIT_FIELDS = (
    "daily_loss_limit",
    "max_open_worst_case_loss",
    "max_reserved_debit",
    "max_unredeemed_count",
    "max_unredeemed_cost",
    "max_consecutive_losses",
)


def _run_limits(raw: Any) -> RunLimits | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RunConfigError("settlement.run_limits must be a mapping")
    _reject_unknown(raw, set(RUN_LIMIT_FIELDS) | {"accounting_timezone"}, "settlement.run_limits")
    for key in RUN_LIMIT_FIELDS:
        if key not in raw:
            raise RunConfigError(
                f"settlement.run_limits.{key} is required (monetary limits have no defaults)"
            )
    try:
        return RunLimits(
            daily_loss_limit=_decimal(raw["daily_loss_limit"], "daily_loss_limit"),
            max_open_worst_case_loss=_decimal(
                raw["max_open_worst_case_loss"], "max_open_worst_case_loss"
            ),
            max_reserved_debit=_decimal(raw["max_reserved_debit"], "max_reserved_debit"),
            max_unredeemed_count=int(raw["max_unredeemed_count"]),
            max_unredeemed_cost=_decimal(raw["max_unredeemed_cost"], "max_unredeemed_cost"),
            max_consecutive_losses=int(raw["max_consecutive_losses"]),
            accounting_timezone=str(raw.get("accounting_timezone", "UTC")),
        )
    except ValueError as exc:
        raise RunConfigError(f"settlement.run_limits: {exc}") from exc


def _settlement_config(raw: Any, mode: str) -> SettlementRuntimeConfig | None:
    if mode not in SETTLEMENT_MODES:
        if raw is not None:
            raise RunConfigError(
                "settlement block requires lifecycle.mode: hold_to_resolution or managed_exit"
            )
        return None
    if not isinstance(raw, dict):
        raise RunConfigError(f"lifecycle.mode {mode} requires a settlement block")
    _reject_unknown(
        raw,
        {
            "ledger_path",
            "trial",
            "hold_commit_budget_s",
            "redemption_wallet",
            "redemption_mode",
            "run_limits",
            "resolution_poll_interval_s",
        },
        "settlement",
    )
    trial_raw = dict(raw.get("trial") or {})
    _reject_unknown(
        trial_raw,
        {
            "max_debit_per_episode",
            "max_episodes",
            "max_unresolved_count",
            "max_unresolved_cost",
            "trial_loss_budget",
        },
        "settlement.trial",
    )
    for key in (
        "max_debit_per_episode",
        "max_episodes",
        "max_unresolved_count",
        "max_unresolved_cost",
        "trial_loss_budget",
    ):
        if key not in trial_raw:
            raise RunConfigError(f"settlement.trial.{key} is required (no default trial budget)")
    try:
        trial = TrialLimits(
            max_debit_per_episode=_decimal(trial_raw["max_debit_per_episode"], "max_debit"),
            max_episodes=int(trial_raw["max_episodes"]),
            max_unresolved_count=int(trial_raw["max_unresolved_count"]),
            max_unresolved_cost=_decimal(trial_raw["max_unresolved_cost"], "max_unresolved_cost"),
            trial_loss_budget=_decimal(trial_raw["trial_loss_budget"], "trial_loss_budget"),
        )
    except ValueError as exc:
        raise RunConfigError(f"settlement.trial: {exc}") from exc
    mode_raw = str(raw.get("redemption_mode", "operator"))
    if mode_raw != "operator":
        raise RunConfigError(
            "settlement.redemption_mode supports only 'operator' (unattended deferred)"
        )
    budget = float(raw.get("hold_commit_budget_s", 10.0))
    if budget <= 0:
        raise RunConfigError("settlement.hold_commit_budget_s must be positive")
    wallet = raw.get("redemption_wallet")
    if wallet is not None and not (
        isinstance(wallet, str) and wallet.startswith("0x") and len(wallet) == 42
    ):
        raise RunConfigError("settlement.redemption_wallet must be a 0x-prefixed address")
    poll = float(raw.get("resolution_poll_interval_s", 30.0))
    if poll <= 0:
        raise RunConfigError("settlement.resolution_poll_interval_s must be positive")
    return SettlementRuntimeConfig(
        ledger_path=Path(str(raw.get("ledger_path", "var/runtime_state/settlement/ledger.jsonl"))),
        trial=trial,
        hold_commit_budget_s=budget,
        redemption_wallet=wallet,
        redemption_mode=mode_raw,
        run_limits=_run_limits(raw.get("run_limits")),
        resolution_poll_interval_s=poll,
    )


def _paper_config(raw: Any) -> PaperRuntimeConfig:
    if not isinstance(raw, dict):
        raise RunConfigError("venue: paper requires a paper block")
    _reject_unknown(
        raw,
        {
            "initial_collateral",
            "order_latency_ms",
            "participation",
            "resolution_source",
            "auto_redeem_after_s",
        },
        "paper",
    )
    if "initial_collateral" not in raw:
        raise RunConfigError("paper.initial_collateral is required")
    try:
        paper = PaperRuntimeConfig(
            initial_collateral=_decimal(raw["initial_collateral"], "paper.initial_collateral"),
            order_latency_ms=int(raw.get("order_latency_ms", 300)),
            participation=_decimal(raw.get("participation", "1"), "paper.participation"),
            resolution_source=str(raw.get("resolution_source", "chain_read")),
            auto_redeem_after_s=(
                None
                if raw.get("auto_redeem_after_s") is None
                else float(raw["auto_redeem_after_s"])
            ),
        )
    except (TypeError, ValueError) as exc:
        raise RunConfigError(f"paper: {exc}") from exc
    if paper.initial_collateral <= 0 or paper.order_latency_ms < 0:
        raise RunConfigError("paper.initial_collateral must be > 0 and order_latency_ms >= 0")
    if not Decimal(0) < paper.participation <= Decimal(1):
        raise RunConfigError("paper.participation must be in (0, 1]")
    if paper.resolution_source not in ("chain_read",):
        raise RunConfigError("paper.resolution_source supports only chain_read")
    if paper.auto_redeem_after_s is not None and paper.auto_redeem_after_s < 0:
        raise RunConfigError("paper.auto_redeem_after_s must be >= 0")
    return paper


def entry_tau_bounds(config: TradingRunConfig) -> tuple[float, float]:
    plugin = get_strategy_plugin(config.strategy_kind)
    return plugin.entry_tau_bounds(config.strategy)


def host_safety_floor_s(config: TradingRunConfig) -> float:
    settlement = getattr(config, "settlement", None)
    if getattr(config, "lifecycle_mode", LIFECYCLE_FLATTEN) in SETTLEMENT_MODES and settlement:
        # A filled position is committed to resolution instead of sold before close;
        # entry needs time for fill evidence plus the hold commitment.
        return config.lifecycle.evidence_timeout_s + settlement.hold_commit_budget_s
    return (
        config.lifecycle.mandatory_exit_before_end_s
        + config.lifecycle.evidence_timeout_s
    )


def effective_entry_cutoff_s(config: TradingRunConfig) -> float:
    """Authoritative BUY floor. Consume, the final gate, and POST use this."""
    tau_min_s, _tau_max_s = entry_tau_bounds(config)
    return max(tau_min_s, host_safety_floor_s(config))


def strategy_entry_cutoff_s(config: TradingRunConfig) -> float:
    """Strategy-facing close. Earlier than the hard floor by the dispatch margin.

    This only stops a new entry intent from being emitted too close to the hard
    boundary. It never authorizes a later BUY.
    """
    return effective_entry_cutoff_s(config) + config.market.entry_dispatch_margin_s


def _non_negative_finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunConfigError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise RunConfigError(f"{field} must be finite and >= 0")
    return result


def max_clock_uncertainty_ms(config: TradingRunConfig) -> float:
    plugin = get_strategy_plugin(config.strategy_kind)
    return plugin.max_clock_uncertainty_ms(config.strategy)


def _recording_config(raw: Any) -> RecordingConfig:
    if raw is not None and not isinstance(raw, dict):
        raise RunConfigError("recording must be a mapping")
    try:
        return recording_config_from_mapping(raw)
    except ValueError as exc:
        raise RunConfigError(str(exc)) from exc


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
            "startup",
            "settlement",
            "venue",
            "paper",
            "recording",
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
    if plugin_requires_protection(plugin, strategy) and protection is None:
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
            "entry_dispatch_margin_s",
            "run_mode",
        },
        "market",
    )
    evaluation_interval_s = float(market_raw.get("evaluation_interval_s", 1.0))
    if "entry_dispatch_margin_s" in market_raw:
        entry_dispatch_margin_s = _non_negative_finite(
            market_raw["entry_dispatch_margin_s"],
            "market.entry_dispatch_margin_s",
        )
    else:
        # One evaluation interval, taken from the same config. Not a hidden constant.
        entry_dispatch_margin_s = evaluation_interval_s
    market = MarketRuntimeConfig(
        family=str(market_raw.get("family", "btc_updown_5m")),
        binance_symbol=str(market_raw.get("binance_symbol", "BTCUSDT")),
        require_ssr_price_match=_boolean(
            market_raw.get("require_ssr_price_match", False),
            "market.require_ssr_price_match",
        ),
        preparation_lead_s=float(market_raw.get("preparation_lead_s", 45.0)),
        maximum_duration_s=float(market_raw.get("maximum_duration_s", 300.0)),
        run_mode=str(market_raw.get("run_mode", RUN_SINGLE_WINDOW)),
        maximum_duration_explicit="maximum_duration_s" in market_raw,
        evaluation_interval_s=evaluation_interval_s,
        maximum_book_age_ms=int(market_raw.get("maximum_book_age_ms", 2_000)),
        maximum_candidate_age_ms=int(market_raw.get("maximum_candidate_age_ms", 5_000)),
        entry_dispatch_margin_s=entry_dispatch_margin_s,
    )
    try:
        get_market_family(market.family)
    except Exception as exc:  # noqa: BLE001
        raise RunConfigError(str(exc)) from exc
    if market.run_mode not in RUN_MODES:
        raise RunConfigError(f"market.run_mode must be one of {sorted(RUN_MODES)}")
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
            "isolated_read_clients",
        },
        "account",
    )
    account = AccountRuntimeConfig(
        refresh_interval_s=float(account_raw.get("refresh_interval_s", 5.0)),
        snapshot_max_age_s=float(account_raw.get("snapshot_max_age_s", 15.0)),
        read_timeout_s=float(account_raw.get("read_timeout_s", 4.0)),
        retry_attempts=int(account_raw.get("retry_attempts", 3)),
        retry_base_delay_s=float(account_raw.get("retry_base_delay_s", 0.25)),
        isolated_read_clients=bool(account_raw.get("isolated_read_clients", True)),
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
            "sell_size_increment",
            "dust_abs_shares",
            "dust_max_notional",
            "prepare_budget_ms",
            "refresh_wait_budget_ms",
            "refresh_revalidate_after_ms",
            "mode",
        },
        "lifecycle",
    )
    lifecycle_mode = str(life_raw.pop("mode", LIFECYCLE_FLATTEN))
    if lifecycle_mode not in LIFECYCLE_MODES:
        raise RunConfigError(f"lifecycle.mode must be one of {sorted(LIFECYCLE_MODES)}")
    supported_modes = plugin_lifecycle_modes(plugin, strategy)
    if lifecycle_mode not in supported_modes:
        raise RunConfigError(
            f"strategy {strategy_kind} supports lifecycle.mode {sorted(supported_modes)}, "
            f"not {lifecycle_mode!r}"
        )
    settlement = _settlement_config(raw.get("settlement"), lifecycle_mode)
    if protection is not None and lifecycle_mode == LIFECYCLE_HOLD:
        raise RunConfigError(
            "protection cannot be combined with lifecycle.mode: hold_to_resolution: a "
            "committed hold is never sold; use lifecycle.mode: managed_exit for SL/TP"
        )
    venue = str(raw.get("venue", "live"))
    if venue not in VENUES:
        raise RunConfigError(f"venue must be one of {sorted(VENUES)}")
    if venue != "paper" and raw.get("paper") is not None:
        raise RunConfigError("a paper block requires venue: paper")
    paper = _paper_config(raw.get("paper")) if venue == "paper" else None
    if market.run_mode == RUN_CONTINUOUS:
        if lifecycle_mode not in SETTLEMENT_MODES or settlement is None:
            raise RunConfigError(
                "market.run_mode: continuous requires lifecycle.mode hold_to_resolution or "
                "managed_exit with a settlement block"
            )
        if settlement.run_limits is None:
            raise RunConfigError(
                "market.run_mode: continuous requires explicit settlement.run_limits"
            )
    for required in (
        "sell_size_increment",
        "dust_abs_shares",
        "dust_max_notional",
        "prepare_budget_ms",
        "refresh_wait_budget_ms",
    ):
        if required not in life_raw:
            raise RunConfigError(f"lifecycle.{required} is required")
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
        sell_size_increment=_decimal(life_raw["sell_size_increment"], "sell_size_increment"),
        dust_abs_shares=_decimal(life_raw["dust_abs_shares"], "dust_abs_shares"),
        dust_max_notional=_decimal(life_raw["dust_max_notional"], "dust_max_notional"),
        prepare_budget_ms=float(life_raw["prepare_budget_ms"]),
        refresh_wait_budget_ms=float(life_raw["refresh_wait_budget_ms"]),
        refresh_revalidate_after_ms=(
            None
            if life_raw.get("refresh_revalidate_after_ms") is None
            else float(life_raw["refresh_revalidate_after_ms"])
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
        or lifecycle.sell_size_increment <= 0
        or lifecycle.dust_abs_shares <= 0
        or lifecycle.dust_max_notional <= 0
        or lifecycle.prepare_budget_ms <= 0
        or lifecycle.refresh_wait_budget_ms < 0
        or (
            lifecycle.refresh_revalidate_after_ms is not None
            and not 0 <= lifecycle.refresh_revalidate_after_ms <= lifecycle.refresh_wait_budget_ms
        )
    ):
        raise RunConfigError("lifecycle retry settings must be positive")
    startup_raw = dict(raw.get("startup") or {})
    _reject_unknown(
        startup_raw,
        {"inspection_budget_s", "prompt_timeout_s", "monitor_interval_s"},
        "startup",
    )
    startup = StartupRuntimeConfig(
        inspection_budget_s=float(startup_raw.get("inspection_budget_s", 60)),
        prompt_timeout_s=float(startup_raw.get("prompt_timeout_s", 120)),
        monitor_interval_s=float(startup_raw.get("monitor_interval_s", 15)),
    )
    if (
        startup.inspection_budget_s <= 0
        or startup.prompt_timeout_s <= 0
        or startup.monitor_interval_s <= 0
    ):
        raise RunConfigError("startup budgets must be positive")
    if lifecycle.prepare_budget_ms >= float(market.maximum_candidate_age_ms):
        raise RunConfigError("prepare_budget_ms must be less than market.maximum_candidate_age_ms")
    config = TradingRunConfig(
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
        startup=startup,
        lifecycle_mode=lifecycle_mode,
        settlement=settlement,
        venue=venue,
        paper=paper,
        recording=_recording_config(raw.get("recording")),
    )
    if venue == "paper":
        for label, path in (
            ("state_directory", config.state_directory),
            ("settlement.ledger_path", None if settlement is None else settlement.ledger_path),
            ("report_directory", config.report_directory),
        ):
            if path is not None and "paper" not in path.as_posix().lower():
                raise RunConfigError(
                    f"venue: paper requires a separate {label} whose path contains 'paper'"
                )
    if lifecycle_mode == LIFECYCLE_MANAGED:
        exit_cfg = getattr(strategy, "exit", None)
        cutoff = getattr(exit_cfg, "exit_cutoff_tau_s", None)
        if cutoff is not None and cutoff <= lifecycle.mandatory_exit_before_end_s:
            raise RunConfigError(
                "managed_exit: strategy exit.exit_cutoff_tau_s must exceed "
                "lifecycle.mandatory_exit_before_end_s so the declared residual policy "
                "applies before the host's pre-commit flatten"
            )
    if (
        settlement is not None
        and settlement.trial.max_debit_per_episode > risk.maximum_total_debit
    ):
        raise RunConfigError(
            "settlement.trial.max_debit_per_episode exceeds risk.maximum_total_debit"
        )
    tau_min_s, tau_max_s = entry_tau_bounds(config)
    floor_s = host_safety_floor_s(config)
    if tau_min_s < floor_s:
        raise RunConfigError(
            f"strategy tau_min_s={tau_min_s} must be >= host safety floor "
            f"{floor_s} (mandatory_exit_before_end_s + evidence_timeout_s)"
        )
    soft_cutoff_s = strategy_entry_cutoff_s(config)
    if tau_max_s <= soft_cutoff_s:
        raise RunConfigError(
            "tau_max_s must be greater than the strategy-facing entry cutoff "
            f"{soft_cutoff_s} (effective hard cutoff + entry_dispatch_margin_s)"
        )
    return config
