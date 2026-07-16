"""Phase 1 M6 — survival kill switches (loss/attempt counters)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.config import SurvivalKillSwitchConfig

ACTION_DENY_ENTRY = "deny_entry"
ACTION_PAUSE_STRATEGY = "pause_strategy"
ACTION_FORCE_FLATTEN_PAIR = "force_flatten_pair"
ACTION_HARD_STOP = "hard_stop"

_SEVERITY = {
    ACTION_HARD_STOP: 4,
    ACTION_FORCE_FLATTEN_PAIR: 3,
    ACTION_PAUSE_STRATEGY: 2,
    ACTION_DENY_ENTRY: 1,
}


@dataclass(frozen=True)
class KillSwitchDecision:
    triggered: bool
    switch_name: str | None
    action: str | None
    current_value: Decimal | int | None
    threshold: Decimal | int | None
    reason: str | None = None


@dataclass
class _KillSwitchState:
    version: int = 1
    daily_date: str = ""
    daily_loss: Decimal = Decimal("0")
    failed_lifecycle_count: int = 0
    consecutive_no_entry: int = 0
    bad_market_quality_streak: int = 0
    manual_intervention_count: int = 0
    last_pair_loss: Decimal | None = None
    hard_stop_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "daily_date": self.daily_date,
            "daily_loss": str(self.daily_loss),
            "failed_lifecycle_count": self.failed_lifecycle_count,
            "consecutive_no_entry": self.consecutive_no_entry,
            "bad_market_quality_streak": self.bad_market_quality_streak,
            "manual_intervention_count": self.manual_intervention_count,
            "last_pair_loss": str(self.last_pair_loss) if self.last_pair_loss is not None else None,
            "hard_stop_active": self.hard_stop_active,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> _KillSwitchState:
        ll = raw.get("last_pair_loss")
        return cls(
            version=int(raw.get("version", 1)),
            daily_date=str(raw.get("daily_date", "")),
            daily_loss=Decimal(str(raw.get("daily_loss", "0"))),
            failed_lifecycle_count=int(raw.get("failed_lifecycle_count", 0)),
            consecutive_no_entry=int(raw.get("consecutive_no_entry", 0)),
            bad_market_quality_streak=int(raw.get("bad_market_quality_streak", 0)),
            manual_intervention_count=int(raw.get("manual_intervention_count", 0)),
            last_pair_loss=Decimal(str(ll)) if ll not in (None, "") else None,
            hard_stop_active=bool(raw.get("hard_stop_active", False)),
        )


class KillSwitchManager:
    def __init__(
        self,
        cfg: SurvivalKillSwitchConfig,
        *,
        state_path: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._state_path = state_path
        self._state = _KillSwitchState()
        if state_path and state_path.is_file():
            self._state = _KillSwitchState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))

    @property
    def state(self) -> _KillSwitchState:
        return self._state

    def _persist(self) -> None:
        if not self._cfg.persist_daily or self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._state.to_dict(), indent=2), encoding="utf-8")

    def reset_daily_if_needed(self, now_ts: float | None = None) -> None:
        now = now_ts if now_ts is not None else time.time()
        today = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        if self._state.daily_date != today:
            self._state.daily_date = today
            self._state.daily_loss = Decimal("0")
            if not self._state.hard_stop_active:
                self._state.failed_lifecycle_count = 0
                self._state.consecutive_no_entry = 0
                self._state.bad_market_quality_streak = 0
            self._persist()

    def record_lifecycle_terminal(self, phase: str, pnl: Decimal | None) -> None:
        if str(phase).upper() == "FAILED":
            self._state.failed_lifecycle_count += 1
        if pnl is not None and pnl < 0:
            loss = abs(pnl)
            self._state.daily_loss += loss
            self._state.last_pair_loss = pnl
        elif pnl is not None:
            self._state.last_pair_loss = pnl
        self._persist()

    def record_no_entry_run(self) -> None:
        self._state.consecutive_no_entry += 1
        self._persist()

    def record_quality_reject(self) -> None:
        self._state.bad_market_quality_streak += 1
        self._persist()

    def record_quality_pass(self) -> None:
        if self._state.bad_market_quality_streak > 0:
            self._state.bad_market_quality_streak = 0
            self._persist()

    def record_manual_intervention(self) -> None:
        self._state.manual_intervention_count += 1
        self._persist()

    def record_entry_success(self) -> None:
        if self._state.consecutive_no_entry > 0:
            self._state.consecutive_no_entry = 0
            self._persist()

    def check(self, *, owner_id: str, pair_id: str | None = None) -> KillSwitchDecision:
        if not self._cfg.enabled:
            return KillSwitchDecision(False, None, None, None, None)

        if self._state.hard_stop_active:
            return KillSwitchDecision(
                True,
                "hard_stop_latched",
                ACTION_HARD_STOP,
                self._state.manual_intervention_count,
                self._cfg.max_manual_intervention_count,
                reason="hard_stop_active",
            )

        candidates: list[KillSwitchDecision] = []

        if self._state.manual_intervention_count >= self._cfg.max_manual_intervention_count:
            self._state.hard_stop_active = True
            candidates.append(
                KillSwitchDecision(
                    True,
                    "max_manual_intervention_count",
                    ACTION_HARD_STOP,
                    self._state.manual_intervention_count,
                    self._cfg.max_manual_intervention_count,
                    reason="manual_intervention_threshold",
                )
            )

        if self._state.failed_lifecycle_count >= self._cfg.max_failed_lifecycle_count:
            candidates.append(
                KillSwitchDecision(
                    True,
                    "max_failed_lifecycle_count",
                    ACTION_PAUSE_STRATEGY,
                    self._state.failed_lifecycle_count,
                    self._cfg.max_failed_lifecycle_count,
                )
            )

        if self._state.daily_loss >= self._cfg.daily_max_loss_usd:
            candidates.append(
                KillSwitchDecision(
                    True,
                    "daily_max_loss_usd",
                    ACTION_DENY_ENTRY,
                    self._state.daily_loss,
                    self._cfg.daily_max_loss_usd,
                )
            )

        if self._state.last_pair_loss is not None and self._state.last_pair_loss <= -self._cfg.per_pair_max_loss_usd:
            candidates.append(
                KillSwitchDecision(
                    True,
                    "per_pair_max_loss_usd",
                    ACTION_FORCE_FLATTEN_PAIR,
                    abs(self._state.last_pair_loss),
                    self._cfg.per_pair_max_loss_usd,
                )
            )
            candidates.append(
                KillSwitchDecision(
                    True,
                    "per_pair_max_loss_usd",
                    ACTION_DENY_ENTRY,
                    abs(self._state.last_pair_loss),
                    self._cfg.per_pair_max_loss_usd,
                )
            )

        if self._state.consecutive_no_entry >= self._cfg.max_consecutive_no_entry:
            candidates.append(
                KillSwitchDecision(
                    True,
                    "max_consecutive_no_entry",
                    ACTION_PAUSE_STRATEGY,
                    self._state.consecutive_no_entry,
                    self._cfg.max_consecutive_no_entry,
                )
            )

        if self._state.bad_market_quality_streak >= self._cfg.max_bad_market_quality_streak:
            candidates.append(
                KillSwitchDecision(
                    True,
                    "max_bad_market_quality_streak",
                    ACTION_DENY_ENTRY,
                    self._state.bad_market_quality_streak,
                    self._cfg.max_bad_market_quality_streak,
                )
            )

        if not candidates:
            return KillSwitchDecision(False, None, None, None, None)

        best = max(candidates, key=lambda d: _SEVERITY.get(d.action or "", 0))
        self._persist()
        return best

    def triggered_decisions(self, *, owner_id: str, pair_id: str | None = None) -> list[KillSwitchDecision]:
        """Return all currently triggered decisions (for tests / diagnostics)."""
        primary = self.check(owner_id=owner_id, pair_id=pair_id)
        if not primary.triggered:
            return []
        out = [primary]
        if primary.switch_name == "per_pair_max_loss_usd":
            out.append(
                KillSwitchDecision(
                    True,
                    "per_pair_max_loss_usd",
                    ACTION_DENY_ENTRY,
                    primary.current_value,
                    primary.threshold,
                )
            )
        return out

    def blocks_entry(self, decision: KillSwitchDecision) -> bool:
        if not decision.triggered:
            return False
        return decision.action in {
            ACTION_DENY_ENTRY,
            ACTION_PAUSE_STRATEGY,
            ACTION_HARD_STOP,
            ACTION_FORCE_FLATTEN_PAIR,
        }


def kill_switch_state_path(state_dir: Path, owner_id: str) -> Path:
    return state_dir / "kill_switches" / f"{owner_id}.json"


def manager_from_app(app, *, state_dir: Path | None = None) -> KillSwitchManager | None:
    survival = getattr(app, "survival", None)
    if survival is None or not survival.enabled or not survival.kill_switches.enabled:
        return None
    path = None
    if state_dir is not None and survival.kill_switches.persist_daily:
        owner = ""
        if app.paired_binary is not None:
            owner = app.paired_binary.owner_id
        if owner:
            path = kill_switch_state_path(state_dir, owner)
    return KillSwitchManager(survival.kill_switches, state_path=path)
