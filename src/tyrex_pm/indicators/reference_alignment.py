"""Log-basis and Chainlink-aligned Binance estimate (N3 production form).

Accepted formula (N3 plan + initiative README):

    b_t = ln(C_t / B_t)

    Ĉ_t = B_t · e^{b_latest}

Sign convention: positive b means Chainlink above Binance (C > B).

Legacy linear ``compute_basis_bps`` remains for F2 gate compatibility and is
**not** identical to ln·1e4. Callers must not mix formulas silently.

Thresholds (max skew / freshness / drift) are not applied here — OPEN.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.numerics import as_decimal
from tyrex_pm.indicators.reference_basis import BasisValidity


@dataclass(frozen=True, kw_only=True)
class LogBasisResult:
    """Instantaneous log-basis sample."""

    chainlink: Decimal
    binance: Decimal
    basis_ln: Decimal | None
    basis_ln_x_1e4: Decimal | None  # diagnostic approx; not legacy linear bps
    validity: BasisValidity
    ready: bool
    reason_code: str | None
    formula_id: str = "ln_C_over_B"
    sign_convention: str = "positive_when_chainlink_above_binance"


def compute_log_basis(
    *,
    chainlink: Decimal | str | int | None,
    binance: Decimal | str | int | None,
) -> LogBasisResult:
    if chainlink is None or binance is None:
        return LogBasisResult(
            chainlink=Decimal("0")
            if chainlink is None
            else as_decimal(chainlink, field_name="chainlink"),
            binance=Decimal("0")
            if binance is None
            else as_decimal(binance, field_name="binance"),
            basis_ln=None,
            basis_ln_x_1e4=None,
            validity=BasisValidity.MISSING,
            ready=False,
            reason_code="missing_reference",
        )
    c = as_decimal(chainlink, field_name="chainlink")
    b = as_decimal(binance, field_name="binance")
    if c <= 0 or b <= 0:
        return LogBasisResult(
            chainlink=c,
            binance=b,
            basis_ln=None,
            basis_ln_x_1e4=None,
            validity=BasisValidity.INVALID,
            ready=False,
            reason_code="invalid_reference_price",
        )
    basis = Decimal(str(math.log(float(c / b))))
    return LogBasisResult(
        chainlink=c,
        binance=b,
        basis_ln=basis,
        basis_ln_x_1e4=basis * Decimal("10000"),
        validity=BasisValidity.VALID,
        ready=True,
        reason_code=None,
    )


def aligned_chainlink_estimate(
    *,
    binance: Decimal | str | int,
    basis_ln_latest: Decimal | str | int,
) -> Decimal:
    """Ĉ = B · exp(b_latest)."""
    b = as_decimal(binance, field_name="binance")
    bl = as_decimal(basis_ln_latest, field_name="basis_ln_latest")
    if b <= 0:
        raise ValueError("binance must be positive")
    return as_decimal(str(float(b) * math.exp(float(bl))), field_name="c_hat")


class AlignmentInitState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    READY = "READY"
    EWMA_NOT_CONFIGURED = "EWMA_NOT_CONFIGURED"


@dataclass(frozen=True, kw_only=True)
class AlignedReferenceSnapshot:
    """Dual-reference alignment view (immutable)."""

    chainlink_raw: Decimal
    binance_raw: Decimal
    basis_ln_instant: Decimal | None
    basis_ln_smoothed: Decimal | None
    c_hat: Decimal | None
    pairing_policy_id: str
    source_skew_ms: int | None
    init_state: AlignmentInitState
    trading_identity: str
    clock_status: str | None
    blocker_reasons: tuple[str, ...]
    formula_id: str = "ln_C_over_B"
    ewma_half_life_s: float | None = None  # None => OPEN / not configured


@dataclass
class BasisEwmaState:
    """Continuous EWMA of log-basis across market windows.

    Does **not** reset on five-minute window rollover. Half-life must be supplied
    explicitly; ``None`` means EWMA not configured (OPEN) — instantaneous only.
    """

    half_life_s: float | None = None
    _value: float | None = None
    _last_source_ts: datetime | None = None
    _samples: int = 0

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def value(self) -> Decimal | None:
        if self._value is None:
            return None
        return Decimal(str(self._value))

    def reset(self) -> None:
        """Explicit reset only — not called on window change."""
        self._value = None
        self._last_source_ts = None
        self._samples = 0

    def update(
        self,
        *,
        basis_ln: Decimal | float,
        source_ts: datetime,
    ) -> AlignmentInitState:
        source_ts = require_utc(source_ts, field_name="source_ts")
        if self.half_life_s is None:
            self._samples += 1
            self._value = float(basis_ln)
            self._last_source_ts = source_ts
            return AlignmentInitState.EWMA_NOT_CONFIGURED
        if self.half_life_s <= 0:
            raise ValueError("half_life_s must be positive when configured")
        x = float(basis_ln)
        if self._value is None or self._last_source_ts is None:
            self._value = x
            self._last_source_ts = source_ts
            self._samples = 1
            return AlignmentInitState.INSUFFICIENT_HISTORY
        dt_s = (source_ts - self._last_source_ts).total_seconds()
        if dt_s < 0:
            return (
                AlignmentInitState.READY
                if self._samples >= 2
                else AlignmentInitState.INSUFFICIENT_HISTORY
            )
        lam = math.exp(-math.log(2) / self.half_life_s * dt_s)
        self._value = lam * self._value + (1.0 - lam) * x
        self._last_source_ts = source_ts
        self._samples += 1
        if self._samples < 2:
            return AlignmentInitState.INSUFFICIENT_HISTORY
        return AlignmentInitState.READY


def build_aligned_reference(
    *,
    chainlink: Decimal | str | int,
    binance: Decimal | str | int,
    pairing_policy_id: str,
    source_skew_ms: int | None,
    trading_identity: str,
    clock_status: str | None,
    ewma: BasisEwmaState | None = None,
    source_ts: datetime | None = None,
    extra_blockers: tuple[str, ...] = (),
) -> AlignedReferenceSnapshot:
    """Pure assembly of instantaneous + optional smoothed basis."""
    log = compute_log_basis(chainlink=chainlink, binance=binance)
    blockers = list(extra_blockers)
    if not log.ready or log.basis_ln is None:
        blockers.append(log.reason_code or "basis_not_ready")
        return AlignedReferenceSnapshot(
            chainlink_raw=log.chainlink,
            binance_raw=log.binance,
            basis_ln_instant=None,
            basis_ln_smoothed=None,
            c_hat=None,
            pairing_policy_id=pairing_policy_id,
            source_skew_ms=source_skew_ms,
            init_state=AlignmentInitState.UNINITIALIZED,
            trading_identity=trading_identity,
            clock_status=clock_status,
            blocker_reasons=tuple(dict.fromkeys(blockers)),
        )

    smoothed: Decimal | None = None
    init = AlignmentInitState.READY
    half: float | None = None
    if ewma is not None:
        half = ewma.half_life_s
        if source_ts is None:
            blockers.append("ewma_source_ts_missing")
            init = AlignmentInitState.INSUFFICIENT_HISTORY
        else:
            init = ewma.update(basis_ln=log.basis_ln, source_ts=source_ts)
            smoothed = ewma.value
            if init is AlignmentInitState.EWMA_NOT_CONFIGURED:
                blockers.append("threshold_not_configured")
    else:
        init = AlignmentInitState.EWMA_NOT_CONFIGURED
        blockers.append("threshold_not_configured")

    b_for_hat = smoothed if smoothed is not None else log.basis_ln
    c_hat = aligned_chainlink_estimate(binance=log.binance, basis_ln_latest=b_for_hat)
    return AlignedReferenceSnapshot(
        chainlink_raw=log.chainlink,
        binance_raw=log.binance,
        basis_ln_instant=log.basis_ln,
        basis_ln_smoothed=smoothed,
        c_hat=c_hat,
        pairing_policy_id=pairing_policy_id,
        source_skew_ms=source_skew_ms,
        init_state=init,
        trading_identity=trading_identity,
        clock_status=clock_status,
        blocker_reasons=tuple(dict.fromkeys(blockers)),
        ewma_half_life_s=half,
    )
