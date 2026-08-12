"""Explicit ProtectionSpec contracts — no hidden defaults for thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Mapping

MarkSource = Literal["bid"]
TakeProfitStyle = Literal["reactive_fak"]
StopLossStyle = Literal["stop_market_fak"]
TrailingActivation = Literal["immediate", "after_profit"]
SizeMode = Literal["full"]


class ProtectionSpecError(ValueError):
    pass


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ProtectionSpecError(f"{field} must be decimal") from exc
    if not result.is_finite():
        raise ProtectionSpecError(f"{field} must be finite")
    return result


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ProtectionSpecError(f"unknown {where} field(s): {', '.join(unknown)}")


@dataclass(frozen=True)
class TakeProfitSpec:
    style: TakeProfitStyle
    absolute: Decimal | None
    pct_from_entry: Decimal | None

    def __post_init__(self) -> None:
        if self.style != "reactive_fak":
            raise ProtectionSpecError(
                "take_profit.style must be reactive_fak (resting_gtc is not implemented)"
            )
        if self.absolute is None and self.pct_from_entry is None:
            raise ProtectionSpecError("take_profit requires absolute or pct_from_entry")
        if self.absolute is not None and self.pct_from_entry is not None:
            raise ProtectionSpecError(
                "take_profit must set exactly one of absolute or pct_from_entry"
            )
        if self.absolute is not None and not (Decimal("0") < self.absolute < Decimal("1")):
            raise ProtectionSpecError("take_profit.absolute must be in (0, 1)")
        if self.pct_from_entry is not None and self.pct_from_entry <= 0:
            raise ProtectionSpecError("take_profit.pct_from_entry must be > 0")


@dataclass(frozen=True)
class StopLossSpec:
    style: StopLossStyle
    absolute: Decimal | None
    pct_from_entry: Decimal | None

    def __post_init__(self) -> None:
        if self.style != "stop_market_fak":
            raise ProtectionSpecError("stop_loss.style must be stop_market_fak")
        if self.absolute is None and self.pct_from_entry is None:
            raise ProtectionSpecError("stop_loss requires absolute or pct_from_entry")
        if self.absolute is not None and self.pct_from_entry is not None:
            raise ProtectionSpecError(
                "stop_loss must set exactly one of absolute or pct_from_entry"
            )
        if self.absolute is not None and not (Decimal("0") < self.absolute < Decimal("1")):
            raise ProtectionSpecError("stop_loss.absolute must be in (0, 1)")
        if self.pct_from_entry is not None and self.pct_from_entry <= 0:
            raise ProtectionSpecError("stop_loss.pct_from_entry must be > 0")


@dataclass(frozen=True)
class TrailingSpec:
    enabled: bool
    activation: TrailingActivation
    trail_abs: Decimal | None
    trail_pct: Decimal | None
    activation_profit_abs: Decimal | None
    mark: MarkSource

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if self.mark != "bid":
            raise ProtectionSpecError("trailing.mark must be bid")
        if self.trail_abs is None and self.trail_pct is None:
            raise ProtectionSpecError("trailing requires trail_abs or trail_pct when enabled")
        if self.trail_abs is not None and self.trail_pct is not None:
            raise ProtectionSpecError(
                "trailing must set exactly one of trail_abs or trail_pct"
            )
        if self.trail_abs is not None and self.trail_abs <= 0:
            raise ProtectionSpecError("trailing.trail_abs must be > 0")
        if self.trail_pct is not None and self.trail_pct <= 0:
            raise ProtectionSpecError("trailing.trail_pct must be > 0")
        if self.activation == "after_profit":
            if self.activation_profit_abs is None or self.activation_profit_abs <= 0:
                raise ProtectionSpecError(
                    "trailing.activation_profit_abs must be > 0 when activation=after_profit"
                )
        elif self.activation_profit_abs is not None:
            raise ProtectionSpecError(
                "trailing.activation_profit_abs is only valid when activation=after_profit"
            )


@dataclass(frozen=True)
class ProtectionSpec:
    """Strategy-declared bracket policy; framework arms and evaluates it."""

    mark: MarkSource
    size_mode: SizeMode
    take_profit: TakeProfitSpec | None
    stop_loss: StopLossSpec | None
    trailing: TrailingSpec | None

    def __post_init__(self) -> None:
        if self.mark != "bid":
            raise ProtectionSpecError("protection.mark must be bid")
        if self.size_mode != "full":
            raise ProtectionSpecError("protection.size_mode must be full")
        if self.take_profit is None and self.stop_loss is None and (
            self.trailing is None or not self.trailing.enabled
        ):
            raise ProtectionSpecError(
                "protection requires at least one of take_profit, stop_loss, or enabled trailing"
            )


def protection_spec_from_mapping(raw: Mapping[str, Any], *, where: str = "protection") -> ProtectionSpec:
    data = dict(raw)
    _reject_unknown(
        data,
        {"mark", "size_mode", "take_profit", "stop_loss", "trailing"},
        where,
    )
    mark = str(data.get("mark", ""))
    size_mode = str(data.get("size_mode", ""))
    if mark != "bid":
        raise ProtectionSpecError(f"{where}.mark must be explicitly set to bid")
    if size_mode != "full":
        raise ProtectionSpecError(f"{where}.size_mode must be explicitly set to full")

    tp_raw = data.get("take_profit")
    sl_raw = data.get("stop_loss")
    trail_raw = data.get("trailing")

    take_profit = None
    if tp_raw is not None:
        if not isinstance(tp_raw, Mapping):
            raise ProtectionSpecError(f"{where}.take_profit must be a mapping")
        tp = dict(tp_raw)
        _reject_unknown(tp, {"style", "absolute", "pct_from_entry"}, f"{where}.take_profit")
        if "style" not in tp:
            raise ProtectionSpecError(f"{where}.take_profit.style is required")
        absolute = None if tp.get("absolute") is None else _decimal(tp["absolute"], f"{where}.take_profit.absolute")
        pct = (
            None
            if tp.get("pct_from_entry") is None
            else _decimal(tp["pct_from_entry"], f"{where}.take_profit.pct_from_entry")
        )
        take_profit = TakeProfitSpec(style=str(tp["style"]), absolute=absolute, pct_from_entry=pct)  # type: ignore[arg-type]

    stop_loss = None
    if sl_raw is not None:
        if not isinstance(sl_raw, Mapping):
            raise ProtectionSpecError(f"{where}.stop_loss must be a mapping")
        sl = dict(sl_raw)
        _reject_unknown(sl, {"style", "absolute", "pct_from_entry"}, f"{where}.stop_loss")
        if "style" not in sl:
            raise ProtectionSpecError(f"{where}.stop_loss.style is required")
        absolute = None if sl.get("absolute") is None else _decimal(sl["absolute"], f"{where}.stop_loss.absolute")
        pct = (
            None
            if sl.get("pct_from_entry") is None
            else _decimal(sl["pct_from_entry"], f"{where}.stop_loss.pct_from_entry")
        )
        stop_loss = StopLossSpec(style=str(sl["style"]), absolute=absolute, pct_from_entry=pct)  # type: ignore[arg-type]

    trailing = None
    if trail_raw is not None:
        if not isinstance(trail_raw, Mapping):
            raise ProtectionSpecError(f"{where}.trailing must be a mapping")
        tr = dict(trail_raw)
        _reject_unknown(
            tr,
            {
                "enabled",
                "activation",
                "trail_abs",
                "trail_pct",
                "activation_profit_abs",
                "mark",
            },
            f"{where}.trailing",
        )
        if "enabled" not in tr or not isinstance(tr["enabled"], bool):
            raise ProtectionSpecError(f"{where}.trailing.enabled must be an explicit boolean")
        if "activation" not in tr:
            raise ProtectionSpecError(f"{where}.trailing.activation is required")
        if "mark" not in tr:
            raise ProtectionSpecError(f"{where}.trailing.mark is required")
        trail_abs = (
            None if tr.get("trail_abs") is None else _decimal(tr["trail_abs"], f"{where}.trailing.trail_abs")
        )
        trail_pct = (
            None if tr.get("trail_pct") is None else _decimal(tr["trail_pct"], f"{where}.trailing.trail_pct")
        )
        activation_profit_abs = (
            None
            if tr.get("activation_profit_abs") is None
            else _decimal(tr["activation_profit_abs"], f"{where}.trailing.activation_profit_abs")
        )
        trailing = TrailingSpec(
            enabled=tr["enabled"],
            activation=str(tr["activation"]),  # type: ignore[arg-type]
            trail_abs=trail_abs,
            trail_pct=trail_pct,
            activation_profit_abs=activation_profit_abs,
            mark=str(tr["mark"]),  # type: ignore[arg-type]
        )

    return ProtectionSpec(
        mark=mark,  # type: ignore[arg-type]
        size_mode=size_mode,  # type: ignore[arg-type]
        take_profit=take_profit,
        stop_loss=stop_loss,
        trailing=trailing,
    )
