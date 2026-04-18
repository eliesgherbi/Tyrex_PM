from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.signals.base import GuruCopySignal
from tyrex_pm.runtime.config import StrategyConfig


@dataclass
class FilterResult:
    ok: bool
    reason: str | None


def apply_filters(sig: GuruCopySignal, cfg: StrategyConfig) -> FilterResult:
    tid = sig.trade.token_id
    if cfg.filters.token_allowlist and str(tid) not in cfg.filters.token_allowlist:
        return FilterResult(False, rc.TOKEN_NOT_ALLOWLISTED)
    n = sig.trade.notional_usd
    if n is not None and n < cfg.filters.min_notional_usd:
        return FilterResult(False, rc.GURU_BELOW_MIN_NOTIONAL)
    if cfg.filters.significance_min_notional_usd > 0 and n is not None:
        if n < cfg.filters.significance_min_notional_usd:
            return FilterResult(False, rc.GURU_SIGNIFICANCE_REJECT)
    if cfg.sizing.conviction.enabled:
        sc = sig.trade.conviction_score
        if sc is None:
            return FilterResult(False, rc.GURU_LOW_CONVICTION)
        if sc < cfg.filters.min_conviction_score:
            return FilterResult(False, rc.GURU_LOW_CONVICTION)
    return FilterResult(True, None)
