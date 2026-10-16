from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from tyrex_pm.data_warehouse.config import strict
from tyrex_pm.data_warehouse.contracts import exact

ADAPTERS = ("scripted_v1", "strategy_closed_loop_v1")


@dataclass(frozen=True)
class FeePolicy:
    version: str = "explicit_scenario_v1"
    mode: str = "scenario"
    rate: str = "0.07"
    exponent: int = 1
    quantum: str = "0.00001"
    buy_fee_unit: str = "shares"
    maker_rate: str = "0"
    provenance: str = "SCENARIO_ASSUMPTION_NOT_VERIFIED_HISTORICAL_FEES"


@dataclass(frozen=True)
class Scenario:
    schema_version: int = 1
    adapter: str = "scripted_v1"
    initial_cash: str = "100"
    feed_delay_ms: int = 0
    decision_delay_ms: int = 0
    order_delay_ms: int = 300
    cancel_delay_ms: int = 300
    quality_predicate: str = "valid_books_at_actions"
    maker_model: str = "no_fill"
    maker_queue_multiplier: str = "2"
    participation: str = "0.1"
    gap_policy: str = "invalidate_path"
    tie_policy: str = "observations_before_actions"
    seed: int = 0
    settlement_release_delay_ms: int = 60000
    fee_policy: FeePolicy = field(default_factory=FeePolicy)
    intents_file: str = ""
    label_policy_file: str = ""
    # Closed-loop strategy replay only (adapter strategy_closed_loop_v1).
    name: str = ""
    feedback_delay_ms: int = 0
    adverse_price_ticks: int = 0
    timer_interval_ms: int = 1000
    # Time from decision (after decision_delay_ms) to the pre-send final gate, i.e. the
    # local preparation/signing time. The gate reads the strategy-visible book at that
    # instant. Must not exceed order_delay_ms (gate happens before venue arrival).
    final_gate_delay_ms: int = 0
    # Share precision of an amount-denominated (spend) BUY fill [shares]. Assumption.
    amount_buy_share_quantum: str = "0.01"
    # The planner's spend is rounded down to this collateral precision before it is sent
    # (venue amount precision) [USDC]. Assumption, to verify against the SDK rounding.
    amount_buy_spend_quantum: str = "0.01"

    def validate(self) -> None:
        if self.schema_version != 1 or self.adapter not in ADAPTERS:
            raise ValueError("unsupported replay adapter/version")
        if self.adapter == "scripted_v1" and (
            self.feedback_delay_ms or self.adverse_price_ticks
        ):
            raise ValueError("feedback delay and adverse ticks apply to closed-loop replay only")
        if self.adverse_price_ticks < 0 or self.timer_interval_ms <= 0:
            raise ValueError("adverse_price_ticks must be >= 0 and timer_interval_ms > 0")
        if self.final_gate_delay_ms < 0 or self.final_gate_delay_ms > self.order_delay_ms:
            raise ValueError("final_gate_delay_ms must be in [0, order_delay_ms]")
        if exact(self.amount_buy_share_quantum) <= 0 or exact(self.amount_buy_spend_quantum) <= 0:
            raise ValueError("amount quanta must be positive")
        if self.quality_predicate not in ("valid_books_at_actions", "strategy_ready"):
            raise ValueError("unknown quality predicate")
        if self.maker_model not in ("no_fill", "queue_trade_assumption_v1"):
            raise ValueError("unsupported maker policy")
        if self.gap_policy != "invalidate_path" or self.tie_policy != "observations_before_actions":
            raise ValueError("unsupported gap/tie policy")
        if not 0 < exact(self.participation) <= 1 or exact(self.initial_cash) <= 0:
            raise ValueError("invalid cash or participation")
        if exact(self.maker_queue_multiplier) < 1:
            raise ValueError("queue multiplier must be conservative (>=1)")
        if any(
            value < 0
            for value in (
                self.feed_delay_ms,
                self.decision_delay_ms,
                self.order_delay_ms,
                self.cancel_delay_ms,
                self.settlement_release_delay_ms,
                self.feedback_delay_ms,
            )
        ):
            raise ValueError("negative delay")
        fees = self.fee_policy
        if fees.mode not in ("scenario", "recorded"):
            raise ValueError("fee mode must be scenario or recorded")
        if fees.version != "explicit_scenario_v1" or fees.buy_fee_unit not in ("shares", "cash"):
            raise ValueError("unsupported fee semantics")
        if fees.exponent < 0 or not 0 <= exact(fees.rate) <= 1 or exact(fees.maker_rate) != 0:
            raise ValueError("invalid fee curve or unsupported maker charge")
        if exact(fees.quantum) <= 0 or not fees.provenance:
            raise ValueError("fee rounding and provenance required")


def load_scenario(path: Path) -> Scenario:
    scenario = strict(Scenario, yaml.safe_load(path.read_text(encoding="utf-8")))
    scenario.validate()
    return scenario


@dataclass(frozen=True)
class Intent:
    order_id: str
    at_ns: int
    instrument_id: str
    action: str
    quantity: str = "0"
    limit_price: str = "0"
    lifetime: str = "FAK"
    post_only: bool = False
    # Amount-denominated BUY (venue market BUY): spend this much collateral walking asks
    # up to limit_price. Empty means share-denominated ``quantity``.
    spend_amount: str = ""

    def validate(self) -> None:
        if self.action not in ("BUY", "SELL", "CANCEL"):
            raise ValueError("unknown intent action")
        if self.at_ns < 0 or not self.order_id:
            raise ValueError("intent requires stable identity and nonnegative timestamp")
        if self.spend_amount:
            if self.action != "BUY" or self.lifetime != "FAK" or self.post_only:
                raise ValueError("spend_amount applies to taker FAK BUY orders only")
            if exact(self.spend_amount) <= 0:
                raise ValueError("spend_amount must be positive")
        if self.action != "CANCEL":
            if not Decimal(0) < exact(self.limit_price) < Decimal(1):
                raise ValueError("invalid order quantity or probability price")
            if not self.spend_amount and exact(self.quantity) <= 0:
                raise ValueError("invalid order quantity or probability price")
            if self.lifetime not in ("FAK", "FOK", "GTC"):
                raise ValueError("unsupported order lifetime")
            if self.post_only and self.lifetime != "GTC":
                raise ValueError("post-only requires GTC")
