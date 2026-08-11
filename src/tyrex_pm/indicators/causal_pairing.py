"""Causal Chainlink/Binance pairing — live policy only.

Policy ID: LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK

For each Chainlink tick C with source_ts = t_C, select the latest Binance tick
with source_ts ≤ t_C. Never pair with a future Binance tick. Symmetric-nearest
matching is forbidden on this path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ingress import IngressMeta
from tyrex_pm.core.numerics import as_decimal

PAIRING_POLICY_ID = "LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK"


class TradingReferenceIdentity(str, Enum):
    """Keep direct Binance and RTDS Binance distinct — never silent switch."""

    BINANCE_SPOT = "binance_spot"
    RTDS_BINANCE = "rtds_binance"
    UNKNOWN = "unknown"


@dataclass(frozen=True, kw_only=True)
class PriceTickView:
    value: Decimal
    source_ts: datetime
    receive_wall_raw_utc: datetime
    receive_wall_corrected_utc: datetime | None = None
    receive_monotonic_ns: int = 0
    identity: TradingReferenceIdentity = TradingReferenceIdentity.BINANCE_SPOT
    ingress: IngressMeta | None = None
    event_id: str | None = None
    raw_fingerprint: str | None = None
    late_or_out_of_order: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ts", require_utc(self.source_ts, field_name="source_ts"))
        object.__setattr__(
            self,
            "receive_wall_raw_utc",
            require_utc(self.receive_wall_raw_utc, field_name="receive_wall_raw_utc"),
        )
        if self.receive_wall_corrected_utc is not None:
            object.__setattr__(
                self,
                "receive_wall_corrected_utc",
                require_utc(
                    self.receive_wall_corrected_utc,
                    field_name="receive_wall_corrected_utc",
                ),
            )
        object.__setattr__(self, "value", as_decimal(self.value, field_name="value"))


@dataclass(frozen=True, kw_only=True)
class CausalPairResult:
    policy_id: str
    chainlink: PriceTickView
    binance: PriceTickView | None
    source_skew_ms: int | None
    paired: bool
    blocker_reasons: tuple[str, ...] = ()
    notes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "notes", dict(self.notes))
        object.__setattr__(self, "blocker_reasons", tuple(self.blocker_reasons))


def select_latest_binance_at_or_before(
    *,
    chainlink: PriceTickView,
    binance_ticks: Sequence[PriceTickView],
    primary_identity: TradingReferenceIdentity = TradingReferenceIdentity.BINANCE_SPOT,
    max_skew_ms: int | None = None,
) -> CausalPairResult:
    """Select latest Binance with source_ts <= Chainlink source_ts.

    ``max_skew_ms`` remains OPEN when None — no silent default gate.
    When set, pairs exceeding skew are reported with ``source_skew_exceeded``
    but the numeric pair is still returned for audit; readiness uses blockers.
    """
    cl_ts = require_utc(chainlink.source_ts, field_name="chainlink.source_ts")
    eligible = [t for t in binance_ticks if t.identity is primary_identity and t.source_ts <= cl_ts]
    blockers: list[str] = []
    notes: dict[str, Any] = {
        "policy_id": PAIRING_POLICY_ID,
        "primary_identity": primary_identity.value,
        "max_skew_ms": max_skew_ms,
        "max_skew_status": "OPEN" if max_skew_ms is None else "configured",
    }

    # Prove look-ahead rejection: nearest future tick is never chosen.
    future = [t for t in binance_ticks if t.identity is primary_identity and t.source_ts > cl_ts]
    if future:
        nearest_future = min(future, key=lambda t: t.source_ts)
        notes["rejected_future_binance_source_ts"] = nearest_future.source_ts.isoformat()
        notes["rejected_future_binance_value"] = str(nearest_future.value)

    if not eligible:
        blockers.append("no_causal_binance_pair")
        return CausalPairResult(
            policy_id=PAIRING_POLICY_ID,
            chainlink=chainlink,
            binance=None,
            source_skew_ms=None,
            paired=False,
            blocker_reasons=tuple(blockers),
            notes=notes,
        )

    chosen = max(eligible, key=lambda t: (t.source_ts, t.receive_monotonic_ns))
    skew_ms = int((cl_ts - chosen.source_ts).total_seconds() * 1000.0)
    if max_skew_ms is not None and skew_ms > max_skew_ms:
        blockers.append("source_skew_exceeded")
        notes["skew_gate"] = "exceeded"
    elif max_skew_ms is None:
        notes["skew_gate"] = "OPEN_not_applied"

    if chosen.late_or_out_of_order or (chosen.ingress and chosen.ingress.late_or_out_of_order):
        blockers.append("binance_late_or_out_of_order")
    if chainlink.late_or_out_of_order or (
        chainlink.ingress and chainlink.ingress.late_or_out_of_order
    ):
        blockers.append("chainlink_late_or_out_of_order")

    for tick, label in ((chainlink, "chainlink"), (chosen, "binance")):
        status = None if tick.ingress is None else tick.ingress.clock_status
        if status == "UNSYNCHRONIZED":
            blockers.append(f"{label}_clock_unsynchronized")
        elif status == "DEGRADED":
            blockers.append(f"{label}_clock_degraded")

    return CausalPairResult(
        policy_id=PAIRING_POLICY_ID,
        chainlink=chainlink,
        binance=chosen,
        source_skew_ms=skew_ms,
        paired=True,
        blocker_reasons=tuple(dict.fromkeys(blockers)),
        notes=notes,
    )
