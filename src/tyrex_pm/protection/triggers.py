"""Pure protection trigger evaluation (no I/O, no mutation)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.protection.reasons import ProtectionReason
from tyrex_pm.protection.spec import ProtectionSpec


@dataclass(frozen=True)
class ResolvedThresholds:
    take_profit: Decimal | None
    stop_loss: Decimal | None
    trail_distance: Decimal | None
    evidence: dict[str, str]


@dataclass(frozen=True)
class TriggerDecision:
    fired: bool
    reason: ProtectionReason | None
    mark: Decimal
    peak: Decimal
    trailing_active: bool
    evidence: dict[str, Any]


def resolve_thresholds(*, spec: ProtectionSpec, entry_price: Decimal) -> ResolvedThresholds:
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    evidence: dict[str, str] = {"entry_price": str(entry_price)}
    tp: Decimal | None = None
    sl: Decimal | None = None
    trail: Decimal | None = None

    if spec.take_profit is not None:
        if spec.take_profit.absolute is not None:
            tp = spec.take_profit.absolute
            evidence["take_profit_mode"] = "absolute"
        else:
            assert spec.take_profit.pct_from_entry is not None
            tp = entry_price * (Decimal("1") + spec.take_profit.pct_from_entry)
            evidence["take_profit_mode"] = "pct_from_entry"
            evidence["take_profit_pct_from_entry"] = str(spec.take_profit.pct_from_entry)
        evidence["take_profit_trigger"] = str(tp)

    if spec.stop_loss is not None:
        if spec.stop_loss.absolute is not None:
            sl = spec.stop_loss.absolute
            evidence["stop_loss_mode"] = "absolute"
        else:
            assert spec.stop_loss.pct_from_entry is not None
            sl = entry_price * (Decimal("1") - spec.stop_loss.pct_from_entry)
            evidence["stop_loss_mode"] = "pct_from_entry"
            evidence["stop_loss_pct_from_entry"] = str(spec.stop_loss.pct_from_entry)
        evidence["stop_loss_trigger"] = str(sl)

    if spec.trailing is not None and spec.trailing.enabled:
        if spec.trailing.trail_abs is not None:
            trail = spec.trailing.trail_abs
            evidence["trail_mode"] = "abs"
        else:
            assert spec.trailing.trail_pct is not None
            trail = entry_price * spec.trailing.trail_pct
            evidence["trail_mode"] = "pct"
            evidence["trail_pct"] = str(spec.trailing.trail_pct)
        evidence["trail_distance"] = str(trail)
        evidence["trail_activation"] = spec.trailing.activation
        if spec.trailing.activation_profit_abs is not None:
            evidence["activation_profit_abs"] = str(spec.trailing.activation_profit_abs)

    return ResolvedThresholds(
        take_profit=tp,
        stop_loss=sl,
        trail_distance=trail,
        evidence=evidence,
    )


def evaluate_triggers(
    *,
    spec: ProtectionSpec,
    mark: Decimal,
    entry_price: Decimal,
    peak: Decimal,
    trailing_active: bool,
    thresholds: ResolvedThresholds,
) -> TriggerDecision:
    """Precedence: stop-loss, then trailing, then take-profit."""
    if mark <= 0:
        raise ValueError("mark must be > 0")
    new_peak = max(peak, mark)
    active = trailing_active
    evidence: dict[str, Any] = {
        **thresholds.evidence,
        "mark": str(mark),
        "peak_before": str(peak),
        "peak_after": str(new_peak),
        "trailing_active_before": trailing_active,
    }

    if (
        spec.trailing is not None
        and spec.trailing.enabled
        and not active
        and spec.trailing.activation == "after_profit"
        and spec.trailing.activation_profit_abs is not None
        and new_peak >= entry_price + spec.trailing.activation_profit_abs
    ):
        active = True
        evidence["trailing_activated"] = True
    if (
        spec.trailing is not None
        and spec.trailing.enabled
        and spec.trailing.activation == "immediate"
    ):
        active = True

    evidence["trailing_active_after"] = active

    if thresholds.stop_loss is not None and mark <= thresholds.stop_loss:
        return TriggerDecision(
            fired=True,
            reason=ProtectionReason.PROTECTION_SL,
            mark=mark,
            peak=new_peak,
            trailing_active=active,
            evidence={**evidence, "layer": "stop_loss"},
        )

    if (
        active
        and thresholds.trail_distance is not None
        and new_peak - mark >= thresholds.trail_distance
    ):
        return TriggerDecision(
            fired=True,
            reason=ProtectionReason.PROTECTION_TRAIL,
            mark=mark,
            peak=new_peak,
            trailing_active=active,
            evidence={**evidence, "layer": "trailing"},
        )

    if thresholds.take_profit is not None and mark >= thresholds.take_profit:
        return TriggerDecision(
            fired=True,
            reason=ProtectionReason.PROTECTION_TP,
            mark=mark,
            peak=new_peak,
            trailing_active=active,
            evidence={**evidence, "layer": "take_profit"},
        )

    return TriggerDecision(
        fired=False,
        reason=None,
        mark=mark,
        peak=new_peak,
        trailing_active=active,
        evidence=evidence,
    )
