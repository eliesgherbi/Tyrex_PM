"""Bridge ResolvedRunConfig → N7 sealed config / operator session.

YAML ``--mode live`` reuses the validated N7 lifecycle; this module never
implements a second live engine.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.runtime.n7_sealed import N7SealedConfig, n7_sealed_from_mapping
from tyrex_pm.runtime.yaml_config.resolve import ResolvedRunConfig, RunMode
from tyrex_pm.runtime.yaml_config.serialize import resolved_to_show_dict
from tyrex_pm.runtime.yaml_config.zgap_schema import zgap_config_to_parameters_dict


def n7_mapping_from_resolved(resolved: ResolvedRunConfig) -> dict[str, Any]:
    """Build the N7 sealed JSON mapping from a live ResolvedRunConfig."""
    if resolved.mode is not RunMode.LIVE:
        raise ValueError("n7_mapping_from_resolved requires --mode live")
    if resolved.n7_mapping is None:
        raise ValueError("resolved live config missing n7_mapping")
    return dict(resolved.n7_mapping)


def sealed_from_resolved(resolved: ResolvedRunConfig) -> N7SealedConfig:
    """Typed N7SealedConfig bound from YAML resolution."""
    return n7_sealed_from_mapping(n7_mapping_from_resolved(resolved))


def write_effective_n7_sealed(resolved: ResolvedRunConfig, path: Path) -> N7SealedConfig:
    """Materialize sealed JSON for preflight/session loaders; return typed sealed."""
    sealed = sealed_from_resolved(resolved)
    payload = sealed.to_dict()
    # Preserve operator-facing notes and z_gap wiring for evidence.
    payload["mode"] = "n7_oneshot"
    payload["allow_same_window_reentry"] = False
    payload["allow_same_window_reversal"] = False
    payload["z_gap"] = {
        "resolution_capability": False,
        "target_notional": format(resolved.risk.target_notional, "f"),
        "fee_rate": format(resolved.zgap.friction.fee_curve.fee_rate, "f"),
        "fee_exponent": format(resolved.zgap.friction.fee_curve.exponent, "f"),
        "parameters": zgap_config_to_parameters_dict(resolved.zgap),
    }
    payload["yaml_run"] = {
        "requested_mode": "live",
        "effective_mode": "live",
        "data_source": resolved.runtime.source,
        "strategy_path": str(resolved.strategy_path).replace("\\", "/"),
        "risk_path": str(resolved.risk_path).replace("\\", "/"),
        "execution_path": str(resolved.execution_path).replace("\\", "/"),
        "runtime_path": str(resolved.runtime_path).replace("\\", "/"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return sealed


def yaml_report_envelope(resolved: ResolvedRunConfig) -> dict[str, Any]:
    """Fields every live YAML run report must preserve."""
    show = resolved_to_show_dict(resolved)
    return {
        "requested_mode": "live",
        "effective_mode": "live",
        "data_source": resolved.runtime.source,
        "resolved_configuration": show,
        "resolved_strategy": show["strategy"],
        "resolved_risk": show["risk"],
        "resolved_execution": show["execution"],
        "require_ssr_price_match": bool(resolved.runtime.require_ssr_price_match),
        "ptb_authority": (
            "chainlink_sealed_k_and_ssr_match"
            if resolved.runtime.require_ssr_price_match
            else "chainlink_sealed_k"
        ),
        "market_family": resolved.runtime.market_family,
        "one_shot": resolved.runtime.one_shot,
        "max_buy_collateral": format(
            Decimal(str((resolved.n7_mapping or {}).get("max_buy_collateral", "5.00"))),
            "f",
        ),
    }


def merge_yaml_into_operator_payload(
    payload: Mapping[str, Any],
    *,
    resolved: ResolvedRunConfig,
) -> dict[str, Any]:
    out = dict(payload)
    out.update(yaml_report_envelope(resolved))
    # Prefer operator session trust fields when already present.
    if "ptb_authority" in payload:
        out["ptb_authority"] = payload["ptb_authority"]
    if "require_ssr_price_match" in payload:
        out["require_ssr_price_match"] = payload["require_ssr_price_match"]
    out["host_binding"] = "n7_operator_oneshot"
    return out
