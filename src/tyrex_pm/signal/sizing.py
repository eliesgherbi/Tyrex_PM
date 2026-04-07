"""Guru follow sizing: proportional base scale and optional conviction-weighted scale."""

from __future__ import annotations

from collections import deque
from typing import Any, Protocol, runtime_checkable

from tyrex_pm.core.types import GuruTradeSignal

_EPS = 1e-12


@runtime_checkable
class SizingPolicy(Protocol):
    def size(self, sig: GuruTradeSignal, *, branch: str) -> float:
        """Return follower quantity before worthiness / risk."""

    def record_accepted_entry_size(self, sig: GuruTradeSignal) -> None:
        """Update rolling guru entry stats after sizing an accepted BUY."""

    def entry_metrics_after_last_size(self) -> dict[str, Any]:
        """Diagnostics for the last ``size(..., branch='entry')`` call; empty if last call was exit."""


class ProportionalSizingPolicy:
    """`quantity = max(0, (size_raw or 0) * scale)` — C2 baseline when conviction is off."""

    def __init__(self, scale: float = 1.0) -> None:
        if scale < 0:
            raise ValueError("scale must be non-negative")
        self._scale = scale
        self._metrics: dict[str, Any] = {}

    def size(self, sig: GuruTradeSignal, *, branch: str = "entry") -> float:
        raw = float(sig.size_raw or 0.0)
        eff = self._scale
        self._metrics = {
            "base_scale": self._scale,
            "effective_scale": eff,
            "conviction_ratio": 1.0,
            "guru_size_raw": raw,
            "rolling_avg_guru_size": None,
            "branch": branch,
        }
        return max(0.0, raw * eff)

    def record_accepted_entry_size(self, sig: GuruTradeSignal) -> None:
        return None

    def entry_metrics_after_last_size(self) -> dict[str, Any]:
        return dict(self._metrics)


class ConvictionProportionalSizingPolicy:
    """
    ``effective_scale = base_scale * min(trade_size / avg, cap)`` on **entry**;
    **exit** uses ``base_scale`` only. Rolling avg uses **accepted BUY** entries with ``size_raw > 0`` only.
    Cold start: empty buffer ⇒ ratio **1.0** (``effective_scale = base_scale * min(1, cap)``).
    """

    def __init__(
        self,
        *,
        base_scale: float,
        conviction_cap: float,
        lookback_trades: int,
    ) -> None:
        if base_scale < 0:
            raise ValueError("base_scale must be non-negative")
        if conviction_cap <= 0:
            raise ValueError("conviction_cap must be positive")
        if lookback_trades < 1:
            raise ValueError("lookback_trades must be >= 1")
        self._base = base_scale
        self._cap = conviction_cap
        self._buf: deque[float] = deque(maxlen=lookback_trades)
        self._metrics: dict[str, Any] = {}

    def size(self, sig: GuruTradeSignal, *, branch: str) -> float:
        raw = float(sig.size_raw or 0.0)
        if branch == "exit":
            eff = self._base
            self._metrics = {
                "base_scale": self._base,
                "effective_scale": eff,
                "conviction_ratio": 1.0,
                "guru_size_raw": raw,
                "rolling_avg_guru_size": None,
                "branch": branch,
            }
            return max(0.0, raw * eff)

        trade_size = max(raw, _EPS)
        if len(self._buf) == 0:
            ratio = min(1.0, self._cap)
            roll_avg: float | None = None
        else:
            avg = max(sum(self._buf) / len(self._buf), _EPS)
            roll_avg = avg
            ratio = min(trade_size / avg, self._cap)
        eff = self._base * ratio
        self._metrics = {
            "base_scale": self._base,
            "effective_scale": eff,
            "conviction_ratio": ratio,
            "guru_size_raw": raw,
            "rolling_avg_guru_size": roll_avg,
            "branch": branch,
        }
        return max(0.0, raw * eff)

    def record_accepted_entry_size(self, sig: GuruTradeSignal) -> None:
        if sig.size_raw is None:
            return
        r = float(sig.size_raw)
        if r <= 0:
            return
        self._buf.append(r)

    def entry_metrics_after_last_size(self) -> dict[str, Any]:
        return dict(self._metrics)


def build_sizing_policy(
    *,
    copy_scale: float,
    conviction_sizing_enabled: bool,
    conviction_sizing_cap: float,
    conviction_sizing_lookback_trades: int,
) -> ProportionalSizingPolicy | ConvictionProportionalSizingPolicy:
    """Compose-time helper: conviction off → proportional only."""
    if not conviction_sizing_enabled:
        return ProportionalSizingPolicy(copy_scale)
    return ConvictionProportionalSizingPolicy(
        base_scale=copy_scale,
        conviction_cap=conviction_sizing_cap,
        lookback_trades=conviction_sizing_lookback_trades,
    )
