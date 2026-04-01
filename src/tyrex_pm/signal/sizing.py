"""Simple copy sizing from guru `size_raw`."""

from __future__ import annotations

from tyrex_pm.core.types import GuruTradeSignal


class ProportionalSizingPolicy:
    """`quantity = max(0, (size_raw or 0) * scale)`."""

    def __init__(self, scale: float = 1.0) -> None:
        if scale < 0:
            raise ValueError("scale must be non-negative")
        self._scale = scale

    def size(self, sig: GuruTradeSignal) -> float:
        raw = float(sig.size_raw or 0.0)
        return max(0.0, raw * self._scale)
