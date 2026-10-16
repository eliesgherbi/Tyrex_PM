"""Trial limits enforced by the framework immediately before dispatch authorization.

The remaining loss budget counts, without double counting (see ``SettlementLedger``):

* realized losses of completed episodes;
* worst-case loss of unresolved holdings (spent cost basis, reduced only by a proven
  payout);
* worst-case debit plus attributable fees of every order that could still fill
  (reserved, dispatched or dispatch-unknown);
* worst-case debit plus attributable fees of the proposed order.

Any episode in MANUAL_INTERVENTION blocks new entries outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .ledger import SettlementLedger


@dataclass(frozen=True)
class TrialLimits:
    max_debit_per_episode: Decimal
    max_episodes: int
    max_unresolved_count: int
    max_unresolved_cost: Decimal
    trial_loss_budget: Decimal

    def __post_init__(self) -> None:
        if self.max_debit_per_episode <= 0 or self.max_debit_per_episode > Decimal("5"):
            raise ValueError("max_debit_per_episode must be in (0, 5] USDC (tiny-live cap)")
        if self.max_episodes < 1 or self.max_unresolved_count < 1:
            raise ValueError("max_episodes and max_unresolved_count must be >= 1")
        if self.max_unresolved_cost <= 0 or self.trial_loss_budget <= 0:
            raise ValueError("unresolved cost and trial loss budget must be > 0")


@dataclass(frozen=True)
class BudgetCheck:
    allowed: bool
    reasons: tuple[str, ...]
    realized_losses: Decimal
    open_worst_case_loss: Decimal
    proposed_worst_case: Decimal
    loss_budget_used_after: Decimal
    loss_budget: Decimal
    unresolved_cost_after: Decimal
    episodes_after: int


def check_entry(
    ledger: SettlementLedger,
    limits: TrialLimits,
    *,
    window_id: str,
    proposed_debit: Decimal,
    proposed_fees: Decimal,
) -> BudgetCheck:
    totals = ledger.totals()
    reasons: list[str] = []
    proposed = proposed_debit + proposed_fees
    if totals.manual_count:
        reasons.append("SETTLEMENT_MANUAL_INTERVENTION_OPEN")
    if ledger.by_window(window_id) is not None:
        reasons.append("EPISODE_ALREADY_EXISTS_FOR_WINDOW")
    if proposed_debit <= 0:
        reasons.append("PROPOSED_DEBIT_NOT_POSITIVE")
    if proposed_debit > limits.max_debit_per_episode:
        reasons.append("MAX_DEBIT_PER_EPISODE")
    if totals.attempted_episodes + 1 > limits.max_episodes:
        reasons.append("MAX_EPISODES")
    if totals.open_count + 1 > limits.max_unresolved_count:
        reasons.append("MAX_UNRESOLVED_COUNT")
    unresolved_after = totals.spent_unresolved + totals.reserved_cash + proposed
    if unresolved_after > limits.max_unresolved_cost:
        reasons.append("MAX_UNRESOLVED_COST")
    used_after = totals.realized_losses + totals.open_worst_case_loss + proposed
    if used_after > limits.trial_loss_budget:
        reasons.append("TRIAL_LOSS_BUDGET")
    return BudgetCheck(
        allowed=not reasons,
        reasons=tuple(reasons),
        realized_losses=totals.realized_losses,
        open_worst_case_loss=totals.open_worst_case_loss,
        proposed_worst_case=proposed,
        loss_budget_used_after=used_after,
        loss_budget=limits.trial_loss_budget,
        unresolved_cost_after=unresolved_after,
        episodes_after=totals.attempted_episodes + 1,
    )
