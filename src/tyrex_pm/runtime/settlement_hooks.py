"""Hold-to-resolution hooks for the trading runtime (``lifecycle.mode: hold_to_resolution``).

The trading runtime calls these at fixed points; legacy profiles never construct them.

* ``before_entry`` — immediately before an entry reaches the dispatch path: verified
  current constraints (fees from provider metadata matching the strategy's curve, tick,
  minimum size), then the trial budget over the durable settlement ledger (pending,
  uncertain, unresolved and realized, plus the proposed order), then a durable
  ``EPISODE_RESERVED`` record. Any failure blocks the entry with stable reasons.
* ``after_entry`` — records the authoritative dispatch outcome: filled (reservation
  becomes spent capital once), no fill / no dispatch (released) or unknown (kept).
* ``commit_hold`` — requires confirmed inventory, no unresolved mutation and no live
  resting order (cancelled first by the caller, proof required), then
  ``HOLD_REQUESTED`` and ``HOLD_COMMITTED``. Only a committed holding is exempt from
  mandatory flatten, the manual-deadline stop and the shutdown sale.
* ``committed_holds`` — (condition, token) pairs that startup reconciliation must treat
  as intentional settlement exposure, never as positions to close.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tyrex_pm.execution.session_state import ExecutionPhase
from tyrex_pm.execution.settlement.constraints import (
    FeeScheduleSource,
    gamma_fee_schedule,
    verify_constraints,
)
from tyrex_pm.execution.settlement.governor import check_entry
from tyrex_pm.execution.settlement.ledger import EpisodeState, SettlementLedger

S = EpisodeState


@dataclass
class EntryCheck:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class SettlementHooks:
    config: Any  # TradingRunConfig with lifecycle_mode == hold_to_resolution
    ledger: SettlementLedger
    fee_schedule_source: FeeScheduleSource = gamma_fee_schedule

    @classmethod
    def from_config(cls, config: Any, **kwargs: Any) -> SettlementHooks | None:
        settlement = getattr(config, "settlement", None)
        if getattr(config, "lifecycle_mode", None) != "hold_to_resolution" or settlement is None:
            return None
        return cls(config=config, ledger=SettlementLedger(settlement.ledger_path), **kwargs)

    def episode_id(self, window_id: str) -> str:
        return f"{self.config.run_name}:{window_id}"

    # -- startup ---------------------------------------------------------------------

    def committed_holds(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (ep.condition_id, ep.token_id)
            for ep in self.ledger.episodes.values()
            if ep.state
            in (
                S.FILLED,
                S.HOLD_REQUESTED,
                S.HOLD_COMMITTED,
                S.RESOLUTION_UNCERTAIN,
                S.PAYOUT_PROVEN,
                S.REDEMPTION_PENDING_OPERATOR,
            )
        )

    def startup_blockers(self) -> tuple[str, ...]:
        """Episodes a crash left between reservation and a known outcome block entries."""
        stuck = [ep.episode_id for ep in self.ledger.needs_reconciliation()]
        return ("SETTLEMENT_EPISODE_NEEDS_RECONCILIATION",) if stuck else ()

    # -- entry -----------------------------------------------------------------------

    async def before_entry(
        self,
        *,
        window_id: str,
        window_slug: str,
        market_id: str,
        condition_id: str,
        token_id: str,
        outcome_index: int,
        target_notional: Decimal,
        tick_size: Decimal | None,
        min_order_size: Decimal | None,
        strategy_fee_curve: Any,
    ) -> EntryCheck:
        reasons: list[str] = list(self.startup_blockers())
        try:
            schedule = await self.fee_schedule_source(window_slug, condition_id)
        except Exception as exc:  # noqa: BLE001 - unknown fees block, never default
            schedule = None
            reasons.append(f"FEE_SCHEDULE_READ_FAILED:{type(exc).__name__}")
        constraints = verify_constraints(
            fee_schedule=schedule,
            strategy_fee_curve=strategy_fee_curve,
            tick_size=tick_size,
            min_order_size=min_order_size,
            fee_source="gamma_fee_schedule",
        )
        reasons.extend(constraints.reasons)
        cap = self.config.risk.maximum_total_debit
        # The planner spends desired / (1 + fee_reserve_rate): the cap already holds the
        # fee reserve, and taker buy fees are charged in shares, so the worst-case cash
        # debit of this order is the capped desired notional.
        proposed = min(target_notional, cap)
        budget = check_entry(
            self.ledger,
            self.config.settlement.trial,
            window_id=window_id,
            proposed_debit=proposed,
            proposed_fees=Decimal(0),
        )
        reasons.extend(budget.reasons)
        evidence = {
            "constraints": {
                "ok": constraints.ok,
                "tick_size": str(tick_size),
                "min_order_size": str(min_order_size),
                "fee_curve": None
                if constraints.fee_curve is None
                else [str(constraints.fee_curve.fee_rate), str(constraints.fee_curve.exponent)],
            },
            "budget": {
                "loss_budget": str(budget.loss_budget),
                "used_after": str(budget.loss_budget_used_after),
                "realized_losses": str(budget.realized_losses),
                "open_worst_case": str(budget.open_worst_case_loss),
                "proposed": str(budget.proposed_worst_case),
                "unresolved_cost_after": str(budget.unresolved_cost_after),
            },
        }
        if reasons:
            return EntryCheck(False, tuple(dict.fromkeys(reasons)), evidence)
        self.ledger.record(
            "EPISODE_RESERVED",
            self.episode_id(window_id),
            window_id=window_id,
            market_id=market_id,
            condition_id=condition_id,
            token_id=token_id,
            outcome_index=outcome_index,
            max_debit=str(proposed),
            max_fees="0",
        )
        return EntryCheck(True, (), evidence)

    def after_entry(self, *, window_id: str, state: Any, cost_basis: Decimal) -> str:
        episode = self.episode_id(window_id)
        if episode not in self.ledger.episodes:
            return "NO_EPISODE"
        phase = None if state is None else state.phase
        shares = Decimal(0) if state is None else state.confirmed_position_shares
        if shares > 0:
            self.ledger.record("FILL_CONFIRMED", episode, net_shares=str(shares),
                               cost_basis=str(cost_basis))
            return "FILL_CONFIRMED"
        if phase is ExecutionPhase.COMPLETED_NO_DISPATCH:
            self.ledger.record("DISPATCH_NOT_SENT", episode)
            return "DISPATCH_NOT_SENT"
        if phase is ExecutionPhase.COMPLETED_NO_FILL:
            self.ledger.record("DISPATCH_NO_FILL", episode)
            return "DISPATCH_NO_FILL"
        self.ledger.record("DISPATCH_UNKNOWN", episode,
                           phase=None if phase is None else phase.value)
        return "DISPATCH_UNKNOWN"

    # -- hold ------------------------------------------------------------------------

    async def commit_hold(
        self,
        *,
        window_id: str,
        state: Any,
        cancel_conflicting: Callable[[], Awaitable[Any]],
    ) -> tuple[bool, str]:
        episode = self.episode_id(window_id)
        ep = self.ledger.episodes.get(episode)
        if ep is None:
            return False, "NO_EPISODE"
        if ep.state is S.HOLD_COMMITTED:
            return True, "ALREADY_COMMITTED"
        if ep.state is not S.FILLED or state is None or state.confirmed_position_shares <= 0:
            return False, "NO_CONFIRMED_INVENTORY"
        if state.confirmed_position_shares != ep.net_shares:
            return False, "INVENTORY_DIFFERS_FROM_LEDGER"
        if state.resting_limit_venue_ids or state.working_maker_entry:
            state = await cancel_conflicting()
        if state is None or state.resting_limit_venue_ids or state.working_maker_entry:
            return False, "CONFLICTING_ORDER_NOT_PROVEN_CANCELLED"
        if state.has_unresolved_mutation_attempt:
            return False, "UNRESOLVED_MUTATION"
        quantity = str(state.confirmed_position_shares)
        self.ledger.record("HOLD_REQUESTED", episode, quantity=quantity)
        self.ledger.record("HOLD_COMMITTED", episode, quantity=quantity)
        return True, "HOLD_COMMITTED"

    def is_committed(self, window_id: str | None) -> bool:
        if window_id is None:
            return False
        ep = self.ledger.episodes.get(self.episode_id(window_id))
        return ep is not None and ep.state is S.HOLD_COMMITTED
