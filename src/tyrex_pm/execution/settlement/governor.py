"""Trial and run limits enforced by the framework immediately before dispatch authorization.

The remaining loss budget counts, without double counting (see ``SettlementLedger``):

* realized losses of completed (and early-sold) episodes;
* worst-case loss of unresolved holdings (spent cost basis, reduced only by a proven
  payout; losses already locked in by partial sales stay counted);
* worst-case debit plus attributable fees of every order that could still fill
  (reserved, dispatched or dispatch-unknown);
* worst-case debit plus attributable fees of the proposed order.

Any episode in MANUAL_INTERVENTION blocks new entries outright.

``RunLimits`` (continuous operation) adds account-wide limits that survive restarts
because they are computed from the durable ledger, never from process memory:

* daily loss: realized losses completed on the current UTC day, plus *all* open
  worst-case loss (open risk is never erased by midnight), plus the proposal;
* open worst-case loss and reserved debit caps;
* unredeemed payout caps (operator redemption: receivables are not cash);
* available collateral (account read, receivables excluded) after reservations;
* consecutive losing completions (pause until an operator acknowledges).

Window budgets (``WindowBudget``) bound episodes, attempts and POSTs per window from
the ledger, so a restart cannot reset retry or re-entry counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
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
class RunLimits:
    """Account-wide limits for continuous operation. Every value must be explicit."""

    daily_loss_limit: Decimal
    max_open_worst_case_loss: Decimal
    max_reserved_debit: Decimal
    max_unredeemed_count: int
    max_unredeemed_cost: Decimal
    # 0 disables the consecutive-loss pause.
    max_consecutive_losses: int
    # Accounting day boundary (fixed, not configurable): UTC midnight.
    accounting_timezone: str = "UTC"

    def __post_init__(self) -> None:
        if min(self.daily_loss_limit, self.max_open_worst_case_loss) <= 0:
            raise ValueError("daily_loss_limit and max_open_worst_case_loss must be > 0")
        if self.max_reserved_debit <= 0 or self.max_unredeemed_cost <= 0:
            raise ValueError("max_reserved_debit and max_unredeemed_cost must be > 0")
        if self.max_unredeemed_count < 1 or self.max_consecutive_losses < 0:
            raise ValueError("max_unredeemed_count >= 1 and max_consecutive_losses >= 0")
        if self.accounting_timezone != "UTC":
            raise ValueError("risk accounting days are UTC days")


@dataclass(frozen=True)
class WindowBudget:
    """Per-window ceilings derived from the strategy's retry/re-entry policy."""

    max_episodes: int = 1
    max_attempts: int = 1
    max_posts: int = 1
    max_attempts_per_episode: int = 1
    max_posts_per_episode: int = 1
    max_prepare_failures_per_episode: int = 1


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
    daily_loss_used_after: Decimal | None = None


def check_entry(
    ledger: SettlementLedger,
    limits: TrialLimits,
    *,
    window_id: str,
    proposed_debit: Decimal,
    proposed_fees: Decimal,
    rearm_episode: str | None = None,
    window_budget: WindowBudget | None = None,
    run_limits: RunLimits | None = None,
    available_collateral: Decimal | None = None,
    today: date | None = None,
) -> BudgetCheck:
    """Budget check for a new episode, or for ``rearm_episode``'s next attempt."""
    totals = ledger.totals()
    reasons: list[str] = []
    proposed = proposed_debit + proposed_fees
    budget = window_budget or WindowBudget()
    if totals.manual_count:
        reasons.append("SETTLEMENT_MANUAL_INTERVENTION_OPEN")
    usage = ledger.window_usage(window_id)
    if rearm_episode is None:
        if usage.open_episode is not None:
            reasons.append("OPEN_EPISODE_EXISTS_FOR_WINDOW")
        if usage.episodes + 1 > budget.max_episodes:
            reasons.append(
                "EPISODE_ALREADY_EXISTS_FOR_WINDOW" if budget.max_episodes == 1
                else "WINDOW_EPISODE_CAP"
            )
    else:
        episode = ledger.episodes.get(rearm_episode)
        if episode is None or episode.window_id != window_id:
            reasons.append("REARM_EPISODE_UNKNOWN")
        else:
            if episode.attempts + 1 > budget.max_attempts_per_episode:
                reasons.append("EPISODE_ATTEMPT_CAP")
            if episode.posts >= budget.max_posts_per_episode:
                reasons.append("EPISODE_POST_CAP")
            if episode.prepare_failures >= budget.max_prepare_failures_per_episode:
                reasons.append("EPISODE_PREPARE_FAILURE_CAP")
    if usage.attempts + 1 > budget.max_attempts and (rearm_episode or usage.episodes):
        reasons.append("WINDOW_ATTEMPT_CAP")
    if usage.posts >= budget.max_posts and (rearm_episode or usage.episodes):
        reasons.append("WINDOW_POST_CAP")
    if proposed_debit <= 0:
        reasons.append("PROPOSED_DEBIT_NOT_POSITIVE")
    if proposed_debit > limits.max_debit_per_episode:
        reasons.append("MAX_DEBIT_PER_EPISODE")
    episodes_after = totals.attempted_episodes + (0 if rearm_episode else 1)
    if rearm_episode is None and episodes_after > limits.max_episodes:
        reasons.append("MAX_EPISODES")
    if totals.open_count + 1 > limits.max_unresolved_count:
        reasons.append("MAX_UNRESOLVED_COUNT")
    unresolved_after = totals.spent_unresolved + totals.reserved_cash + proposed
    if unresolved_after > limits.max_unresolved_cost:
        reasons.append("MAX_UNRESOLVED_COST")
    used_after = totals.realized_losses + totals.open_worst_case_loss + proposed
    if used_after > limits.trial_loss_budget:
        reasons.append("TRIAL_LOSS_BUDGET")
    daily_after: Decimal | None = None
    if run_limits is not None:
        day = today or datetime.now(timezone.utc).date()
        daily_after = ledger.realized_losses_on(day) + totals.open_worst_case_loss + proposed
        if daily_after > run_limits.daily_loss_limit:
            reasons.append("DAILY_LOSS_LIMIT")
        if totals.open_worst_case_loss + proposed > run_limits.max_open_worst_case_loss:
            reasons.append("MAX_OPEN_WORST_CASE_LOSS")
        if totals.reserved_cash + proposed > run_limits.max_reserved_debit:
            reasons.append("MAX_RESERVED_DEBIT")
        if totals.unredeemed_count >= run_limits.max_unredeemed_count:
            reasons.append("MAX_UNREDEEMED_COUNT")
        if totals.unredeemed_cost > run_limits.max_unredeemed_cost:
            reasons.append("MAX_UNREDEEMED_COST")
        if (
            run_limits.max_consecutive_losses
            and ledger.consecutive_losses() >= run_limits.max_consecutive_losses
        ):
            reasons.append("CONSECUTIVE_LOSS_PAUSE")
        if available_collateral is None or (
            available_collateral - totals.reserved_cash < proposed
        ):
            reasons.append("AVAILABLE_COLLATERAL_INSUFFICIENT")
    return BudgetCheck(
        allowed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        realized_losses=totals.realized_losses,
        open_worst_case_loss=totals.open_worst_case_loss,
        proposed_worst_case=proposed,
        loss_budget_used_after=used_after,
        loss_budget=limits.trial_loss_budget,
        unresolved_cost_after=unresolved_after,
        episodes_after=episodes_after,
        daily_loss_used_after=daily_after,
    )
