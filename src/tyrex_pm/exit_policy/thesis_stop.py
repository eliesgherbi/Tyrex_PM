"""Thesis-stop exit evaluator (A0.7)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.quant.binary_fair_value import MODEL_STATUS_READY, FairValueSnapshot
from tyrex_pm.quant.edge import LEG_DOWN, LEG_UP
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import ZGapExitConfig

EXIT_REASON_THESIS_STOP = "thesis_stop"


@dataclass
class ThesisStopTracker:
    """Tracks continuous confirmation for thesis stop."""

    first_cross_ts: float | None = None
    triggered: bool = False

    def reset(self) -> None:
        self.first_cross_ts = None

    def observe(
        self,
        *,
        crossed: bool,
        now_ts: float,
        confirm_s: float,
    ) -> bool:
        if self.triggered:
            return False
        if not crossed:
            self.first_cross_ts = None
            return False
        if self.first_cross_ts is None:
            self.first_cross_ts = now_ts
            return False
        if now_ts - self.first_cross_ts >= confirm_s:
            self.triggered = True
            return True
        return False


@dataclass(frozen=True)
class ThesisStopDecision:
    should_exit: bool
    held_leg: str | None
    current_z: Decimal | None
    z_stop: Decimal
    confirmation_duration_s: float | None
    reason: str | None = None


def _model_ready(fair: FairValueSnapshot, vol: VolatilitySnapshot) -> bool:
    if not vol.ready:
        return False
    if fair.model_status != MODEL_STATUS_READY:
        return False
    if fair.z is None:
        return False
    if fair.sigma is None or fair.sigma <= 0:
        return False
    return True


def evaluate_thesis_stop(
    *,
    held_leg: str | None,
    fair: FairValueSnapshot,
    vol: VolatilitySnapshot,
    exit_cfg: ZGapExitConfig,
    tracker: ThesisStopTracker,
    now_ts: float,
    feeds_fresh: bool,
) -> ThesisStopDecision:
    z_stop = exit_cfg.z_stop
    if held_leg not in {LEG_UP, LEG_DOWN}:
        return ThesisStopDecision(
            should_exit=False,
            held_leg=held_leg,
            current_z=fair.z,
            z_stop=z_stop,
            confirmation_duration_s=None,
        )
    if not feeds_fresh or not _model_ready(fair, vol):
        tracker.reset()
        return ThesisStopDecision(
            should_exit=False,
            held_leg=held_leg,
            current_z=fair.z,
            z_stop=z_stop,
            confirmation_duration_s=None,
            reason="model_not_ready",
        )

    z = Decimal(str(fair.z))
    crossed = False
    if held_leg == LEG_UP:
        crossed = z <= -z_stop
    elif held_leg == LEG_DOWN:
        crossed = z >= z_stop

    confirm_dur: float | None = None
    if tracker.first_cross_ts is not None:
        confirm_dur = max(0.0, now_ts - tracker.first_cross_ts)

    should = tracker.observe(crossed=crossed, now_ts=now_ts, confirm_s=exit_cfg.stop_confirm_s)
    if should:
        confirm_dur = max(confirm_dur or 0.0, exit_cfg.stop_confirm_s)

    return ThesisStopDecision(
        should_exit=should,
        held_leg=held_leg,
        current_z=z,
        z_stop=z_stop,
        confirmation_duration_s=confirm_dur,
        reason=EXIT_REASON_THESIS_STOP if should else None,
    )
