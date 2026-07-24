"""Resolve YAML files + scenario + CLI into one ResolvedRunConfig."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.market_data.freshness import FreshnessConfig, TimestampBasis
from tyrex_pm.runtime.config import RiskPlanConfig
from tyrex_pm.runtime.shadow_config import ShadowConfig, shadow_config_from_mapping
from tyrex_pm.runtime.yaml_config.errors import ConfigError
from tyrex_pm.runtime.yaml_config.load import load_yaml_mapping
from tyrex_pm.runtime.yaml_config.overlay import apply_leaf_overlay, validate_scenario_name
from tyrex_pm.runtime.yaml_config.zgap_schema import (
    zgap_config_from_parameters,
    zgap_config_to_parameters_dict,
    zgap_leaf_paths,
)
from tyrex_pm.strategies.z_gap.config import ZGapConfig

_RUN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_RISK_LEAVES = frozenset(
    {
        "risk.target_notional",
        "risk.max_notional",
        "risk.min_price",
        "risk.max_price",
        "risk.max_spread",
        "risk.min_liquidity_notional",
        "risk.no_entry_before_close_s",
        "risk.duplicate_lifetime_s",
        "risk.kill_switch_active",
        "risk.live_limits.max_buy_collateral",
        "risk.live_limits.max_daily_notional",
        "risk.live_limits.max_daily_loss",
        "risk.live_limits.max_positions_per_window",
        "risk.live_limits.max_entry_lineages",
        "risk.live_limits.allow_same_window_reentry",
        "risk.live_limits.allow_same_window_reversal",
        "risk.live_limits.skip_if_min_exceeds_cap",
    }
)

_EXEC_LEAVES = frozenset(
    {
        "execution.kind",
        "execution.shadow.enable_oms",
        "execution.shadow.max_position_notional",
        "execution.shadow.max_total_exposure",
        "execution.shadow.max_hold_s",
        "execution.shadow.flatten_before_close_s",
        "execution.shadow.exit_on_flat",
        "execution.shadow.persistence_path",
        "execution.shadow.cancel_unfilled_residual",
        "execution.shadow.fee_rate",
        "execution.shadow.fee_model_id",
        "execution.shadow.entry_retry_cooldown_s",
        "execution.shadow.entry_max_attempts",
        "execution.shadow.exit_retry_cooldown_s",
        "execution.shadow.exit_max_normal_retries",
        "execution.shadow.exit_escalate_after",
        "execution.shadow.require_flat_for_promote",
        "execution.shadow.fills.model_id",
        "execution.shadow.fills.latency_ms",
        "execution.shadow.fills.extra_slip_ticks",
        "execution.shadow.fills.tick_size",
        "execution.polymarket_live.order_style",
        "execution.polymarket_live.live.enabled",
        "execution.polymarket_live.live.mutations_enabled",
        "execution.polymarket_live.live.scope",
        "execution.polymarket_live.live.ack_timeout_ms",
        "execution.polymarket_live.live.max_order_notional",
        "execution.polymarket_live.live.hard_collateral_cap",
        "execution.polymarket_live.live.order_style",
        "execution.polymarket_live.timing.last_allowed_entry_before_end_s",
        "execution.polymarket_live.timing.discretionary_exit_cutoff_before_end_s",
        "execution.polymarket_live.timing.mandatory_flatten_start_before_end_s",
        "execution.polymarket_live.timing.residual_operator_deadline_before_end_s",
        "execution.polymarket_live.timing.event_end_safety_buffer_s",
        "execution.polymarket_live.timing.acknowledgment_timeout_s",
        "execution.polymarket_live.timing.cancel_recon_budget_s",
        "execution.polymarket_live.timing.exit_retry_max_attempts",
        "execution.polymarket_live.timing.exit_retry_time_budget_ms",
        "execution.polymarket_live.timing.ack_timeout_ms",
    }
)

_RUNTIME_LEAVES = frozenset(
    {
        "runtime.source",
        "runtime.fixture_path",
        "runtime.binance_symbol",
        "runtime.signal_max_book_spread",
        "runtime.freshness.book_threshold_ms",
        "runtime.freshness.reference_threshold_ms",
        "runtime.freshness.future_tolerance_ms",
        "runtime.freshness.timestamp_basis",
        "runtime.runtime_duration_s",
        "runtime.evaluate_on_reference",
        "runtime.evaluate_interval_s",
        "runtime.timer_eval_count",
        "runtime.window_id",
        "runtime.ptb_k",
        "runtime.resolution_capability",
        "runtime.resolution_evidence_path",
        "runtime.event_slug",
        "runtime.event_url",
        "runtime.condition_id",
        "runtime.momentum_lookback_ms",
        "runtime.momentum_threshold",
        "runtime.momentum_min_samples",
        "runtime.require_ssr_price_match",
        "runtime.market_family",
        "runtime.one_shot",
    }
)

_STRATEGY_TOP = frozenset({"strategy.strategy"}) | frozenset(
    f"strategy.{p}" for p in zgap_leaf_paths()
)


class RunMode(str, Enum):
    OBSERVE = "observe"
    SHADOW = "shadow"
    LIVE = "live"


@dataclass(frozen=True, kw_only=True)
class RuntimeYamlConfig:
    source: str  # fixture | live
    fixture_path: Path | None
    binance_symbol: str
    signal_max_book_spread: Decimal
    freshness: FreshnessConfig
    runtime_duration: timedelta | None
    evaluate_on_reference: bool
    evaluate_interval_s: float
    timer_eval_count: int
    window_id: str
    ptb_k: Decimal
    resolution_capability: bool
    resolution_evidence_path: str | None
    event_slug: str | None
    event_url: str | None
    condition_id: str | None
    momentum_lookback: timedelta
    momentum_threshold: Decimal
    momentum_min_samples: int
    # Live-mode / PTB policy (ignored by observe/shadow hosts except SSR wiring).
    require_ssr_price_match: bool = False
    market_family: str = "btc_updown_5m"
    one_shot: bool = True


@dataclass(frozen=True, kw_only=True)
class ResolvedRunConfig:
    strategy_kind: str
    zgap: ZGapConfig
    risk: RiskPlanConfig
    shadow: ShadowConfig
    runtime: RuntimeYamlConfig
    mode: RunMode
    run_name: str | None
    strategy_path: Path
    risk_path: Path
    execution_path: Path
    runtime_path: Path
    scenario_name: str | None
    scenario_path: Path | None
    execution_kind: str = "shadow"
    # Material for n7_sealed_from_mapping when mode=live; None otherwise.
    n7_mapping: dict[str, Any] | None = None


def _scenario_allowed_paths() -> frozenset[str]:
    return frozenset(_STRATEGY_TOP | _RISK_LEAVES | _EXEC_LEAVES | _RUNTIME_LEAVES)


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], *, file: str, prefix: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown field(s): {', '.join(unknown)}",
            file=file,
            field=f"{prefix}{unknown[0]}",
        )


def _dec(value: Any, *, file: str, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"invalid decimal {value!r}", file=file, field=field) from exc


def _default_strategy_dict() -> dict[str, Any]:
    return {
        "strategy": "z_gap",
        "parameters": zgap_config_to_parameters_dict(ZGapConfig()),
    }


def _default_risk_dict() -> dict[str, Any]:
    return {
        "target_notional": "5",
        "max_notional": "10",
        "min_price": "0.01",
        "max_price": "0.99",
        "max_spread": "0.50",
        "min_liquidity_notional": "1",
        "no_entry_before_close_s": 0,
        "duplicate_lifetime_s": 3600,
        "kill_switch_active": False,
        "live_limits": None,
    }


def _default_execution_dict() -> dict[str, Any]:
    return {
        "kind": "shadow",
        "shadow": {
            "enable_oms": False,
            "max_position_notional": "20",
            "max_total_exposure": "50",
            "max_hold_s": 600.0,
            "flatten_before_close_s": 20.0,
            "exit_on_flat": True,
            "persistence_path": "var/state/z_gap_shadow_snapshot.json",
            "cancel_unfilled_residual": False,
            "fee_rate": "0",
            "fee_model_id": "shadow_zero_fee_v1",
            "entry_retry_cooldown_s": 5.0,
            "entry_max_attempts": 3,
            "exit_retry_cooldown_s": 3.0,
            "exit_max_normal_retries": 3,
            "exit_escalate_after": 2,
            "require_flat_for_promote": True,
            "fills": {
                "model_id": "shadow_immediate_visible_depth_v0",
                "latency_ms": 0.0,
                "extra_slip_ticks": "0",
                "tick_size": "0.01",
            },
        },
        "polymarket_live": None,
    }


def _default_polymarket_live_dict() -> dict[str, Any]:
    return {
        "order_style": "marketable_limit",
        "live": {
            "enabled": False,
            "mutations_enabled": False,
            "scope": "A",
            "ack_timeout_ms": 15000,
            "max_order_notional": "5.00",
            "hard_collateral_cap": "5.00",
            "order_style": "marketable_limit",
        },
        "timing": {
            "last_allowed_entry_before_end_s": 180.0,
            "discretionary_exit_cutoff_before_end_s": 120.0,
            "mandatory_flatten_start_before_end_s": 90.0,
            "residual_operator_deadline_before_end_s": 45.0,
            "event_end_safety_buffer_s": 30.0,
            "acknowledgment_timeout_s": 15.0,
            "cancel_recon_budget_s": 10.0,
            "exit_retry_max_attempts": 3,
            "exit_retry_time_budget_ms": 60000,
            "ack_timeout_ms": 15000,
        },
    }


def _default_live_limits_dict() -> dict[str, Any]:
    return {
        "max_buy_collateral": "5.00",
        "max_daily_notional": "5.00",
        "max_daily_loss": "5.00",
        "max_positions_per_window": 1,
        "max_entry_lineages": 1,
        "allow_same_window_reentry": False,
        "allow_same_window_reversal": False,
        "skip_if_min_exceeds_cap": True,
    }


def _default_runtime_dict() -> dict[str, Any]:
    return {
        "source": "fixture",
        "fixture_path": None,
        "binance_symbol": "BTCUSDT",
        "signal_max_book_spread": "0.50",
        "freshness": {
            "book_threshold_ms": 120000,
            "reference_threshold_ms": 120000,
            "future_tolerance_ms": 500,
            "timestamp_basis": "EVENT_TIME",
        },
        "runtime_duration_s": None,
        "evaluate_on_reference": False,
        "evaluate_interval_s": 1.0,
        "timer_eval_count": 2,
        "window_id": "zgap-default",
        "ptb_k": "100000",
        "resolution_capability": False,
        "resolution_evidence_path": None,
        "event_slug": None,
        "event_url": None,
        "condition_id": None,
        # ObserveConfig still requires momentum fields for host construction
        # even when strategy is z_gap (unused by ZGapBinding).
        "momentum_lookback_ms": 5000,
        "momentum_threshold": "0.001",
        "momentum_min_samples": 2,
        "require_ssr_price_match": False,
        "market_family": "btc_updown_5m",
        "one_shot": True,
    }


def _deep_merge_defaults(defaults: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Fill defaults then apply YAML values (YAML wins). Nested dicts merge."""
    out = copy.deepcopy(defaults)
    for k, v in overlay.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_defaults(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _parse_strategy_file(path: Path) -> dict[str, Any]:
    raw = load_yaml_mapping(path)
    _reject_unknown(raw, {"strategy", "parameters"}, file=str(path), prefix="")
    if "strategy" not in raw:
        raise ConfigError("missing required field 'strategy'", file=str(path), field="strategy")
    kind = str(raw["strategy"]).strip().lower().replace("-", "_")
    if kind not in {"z_gap", "zgap"}:
        raise ConfigError(
            f"unsupported strategy {raw['strategy']!r} (only z_gap in this release)",
            file=str(path),
            field="strategy",
        )
    params = raw.get("parameters")
    if params is not None and not isinstance(params, Mapping):
        raise ConfigError("parameters must be a mapping", file=str(path), field="parameters")
    # Validate against schema early (unknown fields).
    zgap_config_from_parameters(params if isinstance(params, Mapping) else {}, file=str(path))
    base = _default_strategy_dict()
    merged_params = _deep_merge_defaults(base["parameters"], dict(params or {}))
    return {"strategy": "z_gap", "parameters": merged_params}


def _parse_risk_file(path: Path) -> dict[str, Any]:
    raw = load_yaml_mapping(path)
    allowed = {p.split(".", 1)[1] for p in _RISK_LEAVES}
    # live_limits is a nested mapping key at top level
    allowed = {a.split(".", 1)[0] if a.startswith("live_limits.") else a for a in allowed}
    allowed.add("live_limits")
    _reject_unknown(raw, allowed, file=str(path), prefix="")
    if "runtime_mode" in raw:
        raise ConfigError(
            "runtime_mode is CLI-owned (--mode); do not set it in risk YAML",
            file=str(path),
            field="runtime_mode",
        )
    merged = _deep_merge_defaults(_default_risk_dict(), raw)
    ll = merged.get("live_limits")
    if ll is not None:
        if not isinstance(ll, Mapping):
            raise ConfigError(
                "live_limits must be a mapping",
                file=str(path),
                field="live_limits",
            )
        merged["live_limits"] = _deep_merge_defaults(_default_live_limits_dict(), dict(ll))
        _reject_unknown(
            merged["live_limits"],
            set(_default_live_limits_dict().keys()),
            file=str(path),
            prefix="live_limits.",
        )
    return merged


def _parse_shadow_block(shadow_raw: Mapping[str, Any], *, path: Path) -> dict[str, Any]:
    base = _default_execution_dict()
    merged = _deep_merge_defaults(base, {"kind": "shadow", "shadow": dict(shadow_raw)})
    sh = merged["shadow"]
    allowed_shadow = {
        "enable_oms",
        "max_position_notional",
        "max_total_exposure",
        "max_hold_s",
        "flatten_before_close_s",
        "exit_on_flat",
        "persistence_path",
        "cancel_unfilled_residual",
        "fee_rate",
        "fee_model_id",
        "entry_retry_cooldown_s",
        "entry_max_attempts",
        "exit_retry_cooldown_s",
        "exit_max_normal_retries",
        "exit_escalate_after",
        "require_flat_for_promote",
        "fills",
        "fill_model_id",
        "fill_latency_ms",
        "fill_extra_slip_ticks",
        "fill_tick_size",
    }
    _reject_unknown(sh, allowed_shadow, file=str(path), prefix="shadow.")
    if "fills" in sh:
        if not isinstance(sh["fills"], Mapping):
            raise ConfigError(
                "shadow.fills must be a mapping",
                file=str(path),
                field="shadow.fills",
            )
        _reject_unknown(
            sh["fills"],
            {"model_id", "latency_ms", "extra_slip_ticks", "tick_size"},
            file=str(path),
            prefix="shadow.fills.",
        )
    return merged


def _parse_polymarket_live_block(raw_block: Mapping[str, Any], *, path: Path) -> dict[str, Any]:
    merged = _deep_merge_defaults(_default_polymarket_live_dict(), dict(raw_block))
    _reject_unknown(
        merged,
        {"order_style", "live", "timing"},
        file=str(path),
        prefix="polymarket_live.",
    )
    live = merged["live"]
    if not isinstance(live, Mapping):
        raise ConfigError(
            "polymarket_live.live must be a mapping",
            file=str(path),
            field="polymarket_live.live",
        )
    _reject_unknown(
        live,
        {
            "enabled",
            "mutations_enabled",
            "scope",
            "ack_timeout_ms",
            "max_order_notional",
            "hard_collateral_cap",
            "order_style",
        },
        file=str(path),
        prefix="polymarket_live.live.",
    )
    timing = merged["timing"]
    if not isinstance(timing, Mapping):
        raise ConfigError(
            "polymarket_live.timing must be a mapping",
            file=str(path),
            field="polymarket_live.timing",
        )
    _reject_unknown(
        timing,
        set(_default_polymarket_live_dict()["timing"].keys()),
        file=str(path),
        prefix="polymarket_live.timing.",
    )
    # YAML must keep mutations OFF; CLI --live arms the host.
    if bool(live.get("mutations_enabled")) or bool(live.get("enabled")):
        raise ConfigError(
            "polymarket_live.live.enabled/mutations_enabled must be false in YAML; "
            "arm with CLI --live",
            file=str(path),
            field="polymarket_live.live.mutations_enabled",
        )
    return merged


def _parse_execution_file(path: Path) -> dict[str, Any]:
    raw = load_yaml_mapping(path)
    _reject_unknown(raw, {"kind", "shadow", "polymarket_live"}, file=str(path), prefix="")
    kind = str(raw.get("kind", "shadow")).strip().lower()
    if kind == "shadow":
        shadow_raw = raw.get("shadow")
        if shadow_raw is None:
            raise ConfigError(
                "execution.shadow mapping is required", file=str(path), field="shadow"
            )
        if not isinstance(shadow_raw, Mapping):
            raise ConfigError(
                "execution.shadow must be a mapping", file=str(path), field="shadow"
            )
        if raw.get("polymarket_live") is not None:
            raise ConfigError(
                "execution.polymarket_live is only valid with kind=polymarket_live",
                file=str(path),
                field="polymarket_live",
            )
        return _parse_shadow_block(shadow_raw, path=path)
    if kind == "polymarket_live":
        block = raw.get("polymarket_live")
        if block is None:
            raise ConfigError(
                "execution.polymarket_live mapping is required",
                file=str(path),
                field="polymarket_live",
            )
        if not isinstance(block, Mapping):
            raise ConfigError(
                "execution.polymarket_live must be a mapping",
                file=str(path),
                field="polymarket_live",
            )
        if raw.get("shadow") is not None:
            raise ConfigError(
                "execution.shadow is only valid with kind=shadow",
                file=str(path),
                field="shadow",
            )
        pl = _parse_polymarket_live_block(block, path=path)
        # Placeholder shadow (unused by live host) for type compatibility.
        stub = _parse_shadow_block(_default_execution_dict()["shadow"], path=path)
        return {
            "kind": "polymarket_live",
            "polymarket_live": pl,
            "shadow": stub["shadow"],
        }
    raise ConfigError(
        f"unsupported execution.kind {kind!r} (shadow|polymarket_live)",
        file=str(path),
        field="kind",
    )


def _parse_runtime_file(path: Path) -> dict[str, Any]:
    raw = load_yaml_mapping(path)
    # Top-level keys only (nested checked separately).
    allowed = {
        "source",
        "fixture_path",
        "binance_symbol",
        "signal_max_book_spread",
        "freshness",
        "runtime_duration_s",
        "evaluate_on_reference",
        "evaluate_interval_s",
        "timer_eval_count",
        "window_id",
        "ptb_k",
        "resolution_capability",
        "resolution_evidence_path",
        "event_slug",
        "event_url",
        "condition_id",
        "momentum_lookback_ms",
        "momentum_threshold",
        "momentum_min_samples",
        "require_ssr_price_match",
        "market_family",
        "one_shot",
    }
    _reject_unknown(raw, allowed, file=str(path), prefix="")
    if "mode" in raw:
        raise ConfigError(
            "use 'source: fixture|live' for data source; CLI --mode selects observe|shadow|live",
            file=str(path),
            field="mode",
        )
    merged = _deep_merge_defaults(_default_runtime_dict(), raw)
    fr = merged["freshness"]
    if not isinstance(fr, Mapping):
        raise ConfigError("freshness must be a mapping", file=str(path), field="freshness")
    _reject_unknown(
        fr,
        {
            "book_threshold_ms",
            "reference_threshold_ms",
            "future_tolerance_ms",
            "timestamp_basis",
        },
        file=str(path),
        prefix="freshness.",
    )
    src = str(merged["source"]).strip().lower()
    if src not in {"fixture", "live"}:
        raise ConfigError(
            f"runtime.source must be fixture|live, got {merged['source']!r}",
            file=str(path),
            field="source",
        )
    merged["source"] = src
    if not isinstance(merged["require_ssr_price_match"], bool):
        raise ConfigError(
            "require_ssr_price_match must be a boolean",
            file=str(path),
            field="require_ssr_price_match",
        )
    if not isinstance(merged["one_shot"], bool):
        raise ConfigError(
            "one_shot must be a boolean",
            file=str(path),
            field="one_shot",
        )
    return merged


def sanitize_run_name(name: str | None) -> str | None:
    if name is None:
        return None
    if not _RUN_NAME_RE.match(name):
        raise ConfigError(
            "invalid --run-name (use letters, digits, '.', '_', '-' only)",
            field="run_name",
        )
    return name


def resolve_run_config(
    *,
    strategy_path: Path | str,
    risk_path: Path | str,
    execution_path: Path | str,
    runtime_path: Path | str,
    mode: str,
    scenario: str | None = None,
    scenarios_dir: Path | str = Path("config/scenarios"),
    run_name: str | None = None,
) -> ResolvedRunConfig:
    strategy_p = Path(strategy_path)
    risk_p = Path(risk_path)
    execution_p = Path(execution_path)
    runtime_p = Path(runtime_path)

    mode_norm = str(mode).strip().lower()
    if mode_norm not in {"observe", "shadow", "live"}:
        raise ConfigError(
            f"--mode must be observe|shadow|live, got {mode!r}",
            field="mode",
        )
    run_mode = RunMode(mode_norm)

    strategy_dict = _parse_strategy_file(strategy_p)
    risk_dict = _parse_risk_file(risk_p)
    execution_dict = _parse_execution_file(execution_p)
    runtime_dict = _parse_runtime_file(runtime_p)

    effective = {
        "strategy": strategy_dict,
        "risk": risk_dict,
        "execution": execution_dict,
        "runtime": runtime_dict,
    }

    scenario_name: str | None = None
    scenario_path: Path | None = None
    if scenario is not None:
        scenario_name = validate_scenario_name(scenario)
        scenario_path = Path(scenarios_dir) / f"{scenario_name}.yaml"
        overlay_raw = load_yaml_mapping(scenario_path)
        _reject_unknown(
            overlay_raw,
            {"strategy", "risk", "execution", "runtime"},
            file=str(scenario_path),
            prefix="",
        )
        effective = apply_leaf_overlay(
            effective,
            overlay_raw,
            allowed_paths=_scenario_allowed_paths(),
            file=str(scenario_path),
        )

    # Build typed objects from effective dicts
    zgap = zgap_config_from_parameters(
        effective["strategy"].get("parameters"),
        file=str(strategy_p),
    )
    if run_mode is RunMode.OBSERVE:
        risk_mode = RuntimeMode.OBSERVE
    elif run_mode is RunMode.SHADOW:
        risk_mode = RuntimeMode.SHADOW
    else:
        risk_mode = RuntimeMode.LIVE_TINY
    risk = RiskPlanConfig(
        runtime_mode=risk_mode,
        target_notional=_dec(
            effective["risk"]["target_notional"], file=str(risk_p), field="target_notional"
        ),
        max_notional=_dec(
            effective["risk"]["max_notional"], file=str(risk_p), field="max_notional"
        ),
        min_price=_dec(effective["risk"]["min_price"], file=str(risk_p), field="min_price"),
        max_price=_dec(effective["risk"]["max_price"], file=str(risk_p), field="max_price"),
        max_spread=_dec(effective["risk"]["max_spread"], file=str(risk_p), field="max_spread"),
        min_liquidity_notional=_dec(
            effective["risk"]["min_liquidity_notional"],
            file=str(risk_p),
            field="min_liquidity_notional",
        ),
        no_entry_before_close=timedelta(
            seconds=float(effective["risk"]["no_entry_before_close_s"])
        ),
        duplicate_lifetime=timedelta(seconds=float(effective["risk"]["duplicate_lifetime_s"])),
        kill_switch_active=bool(effective["risk"]["kill_switch_active"]),
    )

    exec_kind = str(effective["execution"]["kind"]).strip().lower()
    sh = effective["execution"]["shadow"]
    try:
        shadow = shadow_config_from_mapping(sh)
    except ValueError as exc:
        raise ConfigError(str(exc), file=str(execution_p), field="shadow") from exc

    rt = effective["runtime"]
    fr = rt["freshness"]
    try:
        freshness = FreshnessConfig(
            book_threshold_ms=int(fr["book_threshold_ms"]),
            reference_threshold_ms=int(fr["reference_threshold_ms"]),
            future_tolerance_ms=int(fr.get("future_tolerance_ms", 500)),
            timestamp_basis=TimestampBasis(str(fr.get("timestamp_basis", "EVENT_TIME"))),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc), file=str(runtime_p), field="freshness") from exc

    fixture_path = None if rt.get("fixture_path") in (None, "") else Path(str(rt["fixture_path"]))
    duration = rt.get("runtime_duration_s")
    runtime_cfg = RuntimeYamlConfig(
        source=str(rt["source"]),
        fixture_path=fixture_path,
        binance_symbol=str(rt["binance_symbol"]).upper(),
        signal_max_book_spread=_dec(
            rt["signal_max_book_spread"],
            file=str(runtime_p),
            field="signal_max_book_spread",
        ),
        freshness=freshness,
        runtime_duration=None if duration is None else timedelta(seconds=float(duration)),
        evaluate_on_reference=bool(rt["evaluate_on_reference"]),
        evaluate_interval_s=float(rt["evaluate_interval_s"]),
        timer_eval_count=int(rt["timer_eval_count"]),
        window_id=str(rt["window_id"]),
        ptb_k=_dec(rt["ptb_k"], file=str(runtime_p), field="ptb_k"),
        resolution_capability=bool(rt["resolution_capability"]),
        resolution_evidence_path=(
            None
            if rt.get("resolution_evidence_path") in (None, "")
            else str(rt["resolution_evidence_path"])
        ),
        event_slug=rt.get("event_slug"),
        event_url=rt.get("event_url"),
        condition_id=rt.get("condition_id"),
        momentum_lookback=timedelta(milliseconds=int(rt["momentum_lookback_ms"])),
        momentum_threshold=_dec(
            rt["momentum_threshold"], file=str(runtime_p), field="momentum_threshold"
        ),
        momentum_min_samples=int(rt["momentum_min_samples"]),
        require_ssr_price_match=bool(rt["require_ssr_price_match"]),
        market_family=str(rt.get("market_family") or "btc_updown_5m"),
        one_shot=bool(rt["one_shot"]),
    )

    # Cross-section validation (reject, do not clamp) — kind before shadow flags
    if run_mode is RunMode.OBSERVE and exec_kind != "shadow":
        raise ConfigError(
            "--mode observe requires execution.kind=shadow",
            file=str(execution_p),
            field="kind",
        )
    if run_mode is RunMode.SHADOW and exec_kind != "shadow":
        raise ConfigError(
            "--mode shadow requires execution.kind=shadow",
            file=str(execution_p),
            field="kind",
        )
    if run_mode is RunMode.LIVE and exec_kind != "polymarket_live":
        raise ConfigError(
            "--mode live requires execution.kind=polymarket_live",
            file=str(execution_p),
            field="kind",
        )
    if run_mode is RunMode.SHADOW and not shadow.enable_oms:
        raise ConfigError(
            "--mode shadow requires execution.shadow.enable_oms=true",
            file=str(execution_p),
            field="shadow.enable_oms",
        )
    if run_mode is RunMode.LIVE and runtime_cfg.source != "live":
        raise ConfigError(
            "--mode live requires runtime.source=live (data source, not host mode)",
            file=str(runtime_p),
            field="source",
        )
    if run_mode is RunMode.LIVE and not runtime_cfg.one_shot:
        raise ConfigError(
            "--mode live requires runtime.one_shot=true",
            file=str(runtime_p),
            field="one_shot",
        )
    if run_mode is RunMode.LIVE and runtime_cfg.resolution_capability:
        raise ConfigError(
            "--mode live requires resolution_capability=false",
            file=str(runtime_p),
            field="resolution_capability",
        )
    if runtime_cfg.source == "fixture" and runtime_cfg.fixture_path is None:
        raise ConfigError(
            "runtime.source=fixture requires fixture_path",
            file=str(runtime_p),
            field="fixture_path",
        )
    # Observe/shadow live data still needs an explicit market selector.
    # Live mode discovers one upcoming BTC 5m via the N7 host.
    if (
        run_mode is not RunMode.LIVE
        and runtime_cfg.source == "live"
        and not any((runtime_cfg.event_slug, runtime_cfg.event_url, runtime_cfg.condition_id))
    ):
        raise ConfigError(
            "runtime.source=live requires event_slug, event_url, or condition_id",
            file=str(runtime_p),
            field="event_slug",
        )
    if risk.target_notional > risk.max_notional:
        raise ConfigError(
            "target_notional must be <= max_notional",
            file=str(risk_p),
            field="target_notional",
        )

    n7_mapping: dict[str, Any] | None = None
    if run_mode is RunMode.LIVE:
        ll = effective["risk"].get("live_limits")
        if not isinstance(ll, Mapping):
            raise ConfigError(
                "--mode live requires risk.live_limits mapping",
                file=str(risk_p),
                field="live_limits",
            )
        pl = effective["execution"].get("polymarket_live")
        if not isinstance(pl, Mapping):
            raise ConfigError(
                "--mode live requires execution.polymarket_live mapping",
                file=str(execution_p),
                field="polymarket_live",
            )
        n7_mapping = {
            "mode": "n7_oneshot",
            "one_shot": True,
            "market_family": runtime_cfg.market_family,
            "max_buy_collateral": str(ll["max_buy_collateral"]),
            "max_daily_notional": str(ll["max_daily_notional"]),
            "max_daily_loss": str(ll["max_daily_loss"]),
            "max_positions_per_window": int(ll["max_positions_per_window"]),
            "max_entry_lineages": int(ll["max_entry_lineages"]),
            "allow_same_window_reentry": bool(ll["allow_same_window_reentry"]),
            "allow_same_window_reversal": bool(ll["allow_same_window_reversal"]),
            "skip_if_min_exceeds_cap": bool(ll["skip_if_min_exceeds_cap"]),
            "order_style": str(pl.get("order_style") or "marketable_limit"),
            "require_ssr_price_match": bool(runtime_cfg.require_ssr_price_match),
            "live": dict(pl["live"]),
            "timing": dict(pl["timing"]),
            "z_gap": {
                "resolution_capability": False,
                "target_notional": format(risk.target_notional, "f"),
                "fee_rate": format(zgap.friction.fee_curve.fee_rate, "f"),
                "fee_exponent": format(zgap.friction.fee_curve.exponent, "f"),
            },
        }
        # Validate sealed constraints early (reject bad live YAML).
        from tyrex_pm.runtime.n7_sealed import n7_sealed_from_mapping

        try:
            n7_sealed_from_mapping(n7_mapping)
        except ValueError as exc:
            raise ConfigError(str(exc), file=str(execution_p), field="polymarket_live") from exc

    return ResolvedRunConfig(
        strategy_kind="z_gap",
        zgap=zgap,
        risk=risk,
        shadow=shadow,
        runtime=runtime_cfg,
        mode=run_mode,
        run_name=sanitize_run_name(run_name),
        strategy_path=strategy_p,
        risk_path=risk_p,
        execution_path=execution_p,
        runtime_path=runtime_p,
        scenario_name=scenario_name,
        scenario_path=scenario_path,
        execution_kind=exec_kind,
        n7_mapping=n7_mapping,
    )
