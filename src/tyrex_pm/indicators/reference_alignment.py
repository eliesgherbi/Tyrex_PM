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


class AlignmentMode(str, Enum):
    """How C_hat was produced for this evaluation."""

    RECONSTRUCTION = "RECONSTRUCTION"  # same causal pair → C_hat ≡ C algebraically
    BETWEEN_TICKS = "BETWEEN_TICKS"  # current B_t × prior accepted basis estimate
    UNAVAILABLE = "UNAVAILABLE"


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


@dataclass
class AcceptedBasisEstimate:
    """Latest causally accepted basis estimate for between-tick alignment.

    Updated only when a new Chainlink tick is paired; never from future Binance.
    """

    basis_ln: Decimal | None = None
    as_of_chainlink_source_ts: datetime | None = None
    as_of_binance_source_ts: datetime | None = None
    chainlink_value: Decimal | None = None
    binance_value: Decimal | None = None
    source_skew_ms: int | None = None
    samples: int = 0

    def accept(
        self,
        *,
        basis_ln: Decimal,
        chainlink_source_ts: datetime,
        binance_source_ts: datetime,
        chainlink_value: Decimal,
        binance_value: Decimal,
        source_skew_ms: int | None,
    ) -> None:
        chainlink_source_ts = require_utc(
            chainlink_source_ts, field_name="chainlink_source_ts"
        )
        binance_source_ts = require_utc(
            binance_source_ts, field_name="binance_source_ts"
        )
        if (
            self.as_of_chainlink_source_ts is not None
            and chainlink_source_ts < self.as_of_chainlink_source_ts
        ):
            # Ignore out-of-order Chainlink for estimate advancement.
            return
        self.basis_ln = as_decimal(basis_ln, field_name="basis_ln")
        self.as_of_chainlink_source_ts = chainlink_source_ts
        self.as_of_binance_source_ts = binance_source_ts
        self.chainlink_value = as_decimal(chainlink_value, field_name="chainlink_value")
        self.binance_value = as_decimal(binance_value, field_name="binance_value")
        self.source_skew_ms = source_skew_ms
        self.samples += 1


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


def evaluate_dynamic_alignment(
    *,
    current_binance: Decimal | str | int,
    binance_source_ts: datetime,
    trading_identity: str,
    pairing_policy_id: str,
    accepted: AcceptedBasisEstimate,
    ewma: BasisEwmaState | None = None,
    current_chainlink: Decimal | str | int | None = None,
    chainlink_source_ts: datetime | None = None,
    pairing_source_skew_ms: int | None = None,
    clock_status: str | None = None,
    evaluated_at: datetime | None = None,
    update_accepted_from_current_pair: bool = False,
):
    """Build dynamic alignment for one evaluation without freezing into sealed K.

    Between Chainlink ticks: use ``accepted`` basis estimate with current B_t.
    When ``update_accepted_from_current_pair`` and a current Chainlink pair is
    supplied, instantaneous basis updates the accepted estimate (causal only).

    Returns ``DynamicAlignedReference``.
    """
    from tyrex_pm.domain.polymarket.sealed_reference import DynamicAlignedReference

    b = as_decimal(current_binance, field_name="current_binance")
    binance_source_ts = require_utc(binance_source_ts, field_name="binance_source_ts")
    blockers: list[str] = []
    half = None if ewma is None else ewma.half_life_s
    if ewma is None or ewma.half_life_s is None:
        blockers.append("threshold_not_configured")

    instant: Decimal | None = None
    mode = AlignmentMode.UNAVAILABLE
    includes_current = False
    estimate = accepted.basis_ln
    estimate_as_of = accepted.as_of_chainlink_source_ts
    cl_raw = None if accepted.chainlink_value is None else accepted.chainlink_value
    cl_ts = accepted.as_of_chainlink_source_ts
    skew = pairing_source_skew_ms if pairing_source_skew_ms is not None else accepted.source_skew_ms

    if current_chainlink is not None and chainlink_source_ts is not None:
        chainlink_source_ts = require_utc(
            chainlink_source_ts, field_name="chainlink_source_ts"
        )
        # Causal guard: Binance used for this pair must not be after Chainlink.
        if binance_source_ts > chainlink_source_ts:
            blockers.append("no_causal_binance_pair")
        else:
            log = compute_log_basis(chainlink=current_chainlink, binance=b)
            if log.ready and log.basis_ln is not None:
                instant = log.basis_ln
                cl_raw = log.chainlink
                cl_ts = chainlink_source_ts
                includes_current = True
                if update_accepted_from_current_pair:
                    if ewma is not None:
                        ewma.update(basis_ln=log.basis_ln, source_ts=chainlink_source_ts)
                        estimate = ewma.value if ewma.value is not None else log.basis_ln
                    else:
                        estimate = log.basis_ln
                    accepted.accept(
                        basis_ln=estimate,
                        chainlink_source_ts=chainlink_source_ts,
                        binance_source_ts=binance_source_ts,
                        chainlink_value=log.chainlink,
                        binance_value=log.binance,
                        source_skew_ms=int(
                            (chainlink_source_ts - binance_source_ts).total_seconds()
                            * 1000.0
                        ),
                    )
                    estimate_as_of = chainlink_source_ts
                    skew = accepted.source_skew_ms
                mode = AlignmentMode.RECONSTRUCTION
            else:
                blockers.append(log.reason_code or "basis_not_ready")

    if mode is AlignmentMode.UNAVAILABLE and estimate is not None:
        mode = AlignmentMode.BETWEEN_TICKS
        includes_current = False

    c_hat = None
    if estimate is not None and b > 0:
        c_hat = aligned_chainlink_estimate(binance=b, basis_ln_latest=estimate)
        if mode is AlignmentMode.RECONSTRUCTION and instant is not None and cl_raw is not None:
            # Algebraic identity check (float noise tolerant).
            if abs(float(c_hat) - float(cl_raw)) > max(1e-4, float(cl_raw) * 1e-9):
                blockers.append("reconstruction_identity_unexpected")

    if estimate is None:
        blockers.append("basis_estimate_unavailable")
        mode = AlignmentMode.UNAVAILABLE

    init = AlignmentInitState.READY
    if estimate is None:
        init = AlignmentInitState.UNINITIALIZED
    elif half is None:
        init = AlignmentInitState.EWMA_NOT_CONFIGURED

    return DynamicAlignedReference(
        binance_raw=b,
        binance_source_ts=binance_source_ts,
        trading_identity=trading_identity,
        chainlink_raw=cl_raw,
        chainlink_source_ts=cl_ts,
        instantaneous_basis_ln=instant,
        basis_estimate_used_ln=estimate,
        basis_estimate_as_of_ts=estimate_as_of,
        basis_estimate_includes_current_chainlink=includes_current,
        c_hat=c_hat,
        alignment_mode=mode,
        pairing_policy_id=pairing_policy_id,
        pairing_source_skew_ms=skew,
        init_state=init,
        clock_status=clock_status,
        blocker_reasons=tuple(dict.fromkeys(blockers)),
        ewma_half_life_s=half,
        evaluated_at=evaluated_at,
        provenance={
            "accepted_samples": accepted.samples,
            "formula_id": "ln_C_over_B",
            "c_hat_formula": "B_t * exp(basis_estimate_used)",
        },
    )
