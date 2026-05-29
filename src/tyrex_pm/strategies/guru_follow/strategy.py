from __future__ import annotations

from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import Intent
from tyrex_pm.signals.base import GuruCopySignal
from tyrex_pm.runtime.config import StrategyConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.strategies.guru_follow import exits, filters, sizing
from tyrex_pm.strategies.guru_follow.scheduled_exit_demo import ScheduledExitDemoState


class GuruFollowStrategy:
    def __init__(self, cfg: StrategyConfig) -> None:
        self._cfg = cfg
        self.scheduled_exit_demo = ScheduledExitDemoState(cfg.exits)

    def on_guru_signal(
        self,
        sig: GuruCopySignal,
        coord: RuntimeCoordinator,
    ) -> tuple[list[Intent], str | None, dict[str, Any] | None]:
        fr = filters.apply_filters(sig, self._cfg)
        if not fr.ok:
            return [], fr.reason, None
        if sig.trade.side == Side.SELL:
            intent, skip, side_meta = exits.maybe_exit_intent(sig, self._cfg, coord)
            if skip:
                return [], skip, side_meta
            assert intent is not None
            return [intent], None, side_meta
        ent = sizing.build_enter_intent(sig, self._cfg)
        if ent is None:
            if self._cfg.sizing.static_enabled and self._cfg.sizing.static_amount_usd <= 0:
                return [], rc.GURU_STATIC_AMOUNT_INVALID, None
            return [], rc.GURU_PRICE_REQUIRED, None
        meta: dict[str, str] = {
            "sizing_mode": "static" if self._cfg.sizing.static_enabled else "proportional",
        }
        return [ent], None, meta
