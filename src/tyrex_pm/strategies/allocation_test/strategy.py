"""Toy strategy validating P4 allocation ownership (Owner A buy / Owner B block / Owner A sell)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.runtime.allocation_ids import (
    ALLOCATION_TEST_INTENT_SOURCE,
)
from tyrex_pm.runtime.allocation_runtime import clamp_planned_to_allocated
from tyrex_pm.runtime.config import (
    AllocationTestStrategyConfig,
    SELL_TEST_PRICING_AUTO,
    SELL_TEST_PRICING_FIXED,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.exit_lifecycle import inventory_snapshot, oms_status_is_matched, parse_taking_amount
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.reporting.sinks.jsonl import JsonlSink


# --- State machine phases ---
PHASE_INIT = "INIT"
PHASE_OWNER_A_BUY_SUBMITTED = "OWNER_A_BUY_SUBMITTED"
PHASE_OWNER_A_ALLOCATION_VISIBLE = "OWNER_A_ALLOCATION_VISIBLE"
PHASE_OWNER_B_UNAUTHORIZED_SELL_ATTEMPTED = "OWNER_B_UNAUTHORIZED_SELL_ATTEMPTED"
PHASE_OWNER_B_SELL_BLOCKED = "OWNER_B_SELL_BLOCKED"
PHASE_OWNER_A_SELL_SUBMITTED = "OWNER_A_SELL_SUBMITTED"
PHASE_OWNER_A_SELL_COMPLETED = "OWNER_A_SELL_COMPLETED"
PHASE_DONE = "DONE"

PHASE_BUY_DENIED = "BUY_DENIED"
PHASE_BUY_OMS_REJECT = "BUY_OMS_REJECT"
PHASE_ALLOCATION_NOT_APPLIED = "ALLOCATION_NOT_APPLIED"
PHASE_OWNER_B_SELL_NOT_BLOCKED = "OWNER_B_SELL_NOT_BLOCKED"
PHASE_OWNER_A_SELL_DENIED = "OWNER_A_SELL_DENIED"
PHASE_OWNER_A_SELL_OMS_REJECT = "OWNER_A_SELL_OMS_REJECT"
PHASE_TIMEOUT_ALLOCATION_VISIBLE = "TIMEOUT_ALLOCATION_VISIBLE"
PHASE_TIMEOUT_POSITION_VISIBLE = "TIMEOUT_POSITION_VISIBLE"
PHASE_TIMEOUT_OWNER_A_EXIT = "TIMEOUT_OWNER_A_EXIT"
PHASE_OWNER_A_SELL_PRICING_FAILED = "OWNER_A_SELL_PRICING_FAILED"
PHASE_LEDGER_MISMATCH = "LEDGER_MISMATCH"

_TERMINAL_FAILURE_PHASES = frozenset(
    {
        PHASE_BUY_DENIED,
        PHASE_BUY_OMS_REJECT,
        PHASE_ALLOCATION_NOT_APPLIED,
        PHASE_OWNER_B_SELL_NOT_BLOCKED,
        PHASE_OWNER_A_SELL_DENIED,
        PHASE_OWNER_A_SELL_OMS_REJECT,
        PHASE_TIMEOUT_ALLOCATION_VISIBLE,
        PHASE_TIMEOUT_POSITION_VISIBLE,
        PHASE_TIMEOUT_OWNER_A_EXIT,
        PHASE_OWNER_A_SELL_PRICING_FAILED,
        PHASE_LEDGER_MISMATCH,
    }
)

PHASE_OWNER_A_BUY = "owner_a_buy"
PHASE_OWNER_A_SELL = "owner_a_sell"


class AllocationTestStrategy:
    """Validate that venue wallet inventory != per-owner allocation."""

    def __init__(self, cfg: AllocationTestStrategyConfig) -> None:
        self._cfg = cfg
        self._phase = PHASE_INIT
        self._owner_a_buy_size: Decimal | None = None
        self._owner_a_correlation_id = f"allocation_test:{cfg.token_id}:A"
        self._buy_work_issued = False
        self._buy_submit_succeeded = False
        self._sell_work_issued = False
        self._sell_submit_succeeded = False
        self._sell_outcome: str | None = None
        self._resolved_sell_price: Decimal | None = None
        self._sell_pricing_evidence: dict[str, object] | None = None

    @property
    def cfg(self) -> AllocationTestStrategyConfig:
        return self._cfg

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def owner_a_correlation_id(self) -> str:
        return self._owner_a_correlation_id

    @property
    def buy_submit_succeeded(self) -> bool:
        return self._buy_submit_succeeded

    @property
    def sell_outcome(self) -> str | None:
        return self._sell_outcome

    @property
    def effective_sell_limit_price(self) -> Decimal | None:
        if self._resolved_sell_price is not None:
            return self._resolved_sell_price
        sell_cfg = self._cfg.owner_a_sell
        if sell_cfg.pricing_mode == SELL_TEST_PRICING_FIXED:
            return sell_cfg.limit_price or self._cfg.buy.limit_price
        return sell_cfg.limit_price or self._cfg.buy.limit_price

    def set_resolved_sell_price(
        self,
        price: Decimal,
        *,
        evidence: dict[str, object] | None = None,
    ) -> None:
        if price <= 0:
            raise ValueError(f"resolved sell price must be positive, got {price!r}")
        self._resolved_sell_price = price
        self._sell_pricing_evidence = evidence

    def emit_sell_pricing_failed(self, sink: JsonlSink, run_id: str, *, error: str | None = None) -> None:
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                run_id,
                {
                    "event": "allocation_test_sell_pricing_failed",
                    "token_id": self._cfg.token_id,
                    "pricing_mode": self._cfg.owner_a_sell.pricing_mode,
                    "error": error or "no_marketable_price",
                },
            )
        )
        self._mark_failure(PHASE_OWNER_A_SELL_PRICING_FAILED)

    def is_done(self) -> bool:
        return self._phase == PHASE_DONE or self._phase in _TERMINAL_FAILURE_PHASES

    def is_terminal_failure(self) -> bool:
        return self._phase in _TERMINAL_FAILURE_PHASES

    def _mark_failure(self, phase: str) -> None:
        self._phase = phase

    def mark_timeout_allocation_visible(self) -> None:
        self._mark_failure(PHASE_TIMEOUT_ALLOCATION_VISIBLE)

    def mark_timeout_position_visible(self) -> None:
        self._mark_failure(PHASE_TIMEOUT_POSITION_VISIBLE)

    def mark_timeout_owner_a_exit(self) -> None:
        self._mark_failure(PHASE_TIMEOUT_OWNER_A_EXIT)

    def mark_allocation_not_applied(self) -> None:
        self._mark_failure(PHASE_ALLOCATION_NOT_APPLIED)

    def mark_ledger_mismatch(self) -> None:
        self._mark_failure(PHASE_LEDGER_MISMATCH)

    def owner_a_buy_work_units(self) -> list[IntentWorkUnit]:
        if not self._cfg.enabled or not self._cfg.buy.enabled:
            return []
        if self._cfg.run_once and (self._buy_submit_succeeded or self._buy_work_issued):
            return []
        price = self._cfg.buy.limit_price
        if price is None or price <= 0:
            return []
        size = self._cfg.buy.notional_usd / price
        intent = EnterIntent(
            token_id=TokenId(self._cfg.token_id),
            side=Side.BUY,
            size=size,
            limit_price=price,
            order_style=self._cfg.buy.order_style,
        )
        self._buy_work_issued = True
        self._phase = PHASE_OWNER_A_BUY_SUBMITTED
        return [
            IntentWorkUnit(
                intent=intent,
                correlation_id=self._owner_a_correlation_id,
                intent_fact_extensions={
                    "source": ALLOCATION_TEST_INTENT_SOURCE,
                    "allocation_owner_id": self._cfg.owner_a_id,
                    "allocation_test_phase": PHASE_OWNER_A_BUY,
                },
            )
        ]

    def notify_buy_not_submitted(self) -> None:
        if self._phase == PHASE_OWNER_A_BUY_SUBMITTED:
            self._mark_failure(PHASE_BUY_DENIED)

    def notify_buy_oms_reject(self) -> None:
        if self._phase == PHASE_OWNER_A_BUY_SUBMITTED:
            self._mark_failure(PHASE_BUY_OMS_REJECT)

    def notify_buy_submitted(
        self,
        ap: ApprovedIntent,
        *,
        match_evidence: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(ap.intent, EnterIntent) or ap.intent.side != Side.BUY:
            return
        self._buy_submit_succeeded = True
        taking = parse_taking_amount(match_evidence or {})
        self._owner_a_buy_size = taking if taking is not None and taking > 0 else ap.intent.size
        if self._phase == PHASE_OWNER_A_BUY_SUBMITTED:
            pass  # wait for allocation visibility check in loop

    def check_owner_a_allocation_visible(self, coord: RuntimeCoordinator) -> bool:
        if coord.allocation_ledger is None:
            return False
        tid = TokenId(self._cfg.token_id)
        return coord.allocation_ledger.get_allocated(self._cfg.owner_a_id, tid) > 0

    def wallet_position_qty(self, coord: RuntimeCoordinator) -> Decimal:
        tid = TokenId(self._cfg.token_id)
        snap = inventory_snapshot(coord, tid)
        return Decimal(snap["wallet_position_qty"])

    def check_owner_b_prerequisites_visible(self, coord: RuntimeCoordinator) -> bool:
        """Owner A allocation > 0 and venue wallet position > 0 (live proof for B block)."""
        if not self.check_owner_a_allocation_visible(coord):
            return False
        return self.wallet_position_qty(coord) > 0

    def mark_owner_a_allocation_visible(self) -> None:
        if self._phase == PHASE_OWNER_A_BUY_SUBMITTED and self._buy_submit_succeeded:
            self._phase = PHASE_OWNER_A_ALLOCATION_VISIBLE

    def emit_timeout_position_visible(self, sink: JsonlSink, run_id: str) -> None:
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                run_id,
                {
                    "event": "allocation_test_timeout_position_visible",
                    "token_id": self._cfg.token_id,
                    "owner_a_id": self._cfg.owner_a_id,
                    "timeout_s": self._cfg.timeouts.position_visible_s,
                },
            )
        )
        self.mark_timeout_position_visible()

    def _planned_owner_b_size(self) -> Decimal:
        mode = self._cfg.owner_b_unauthorized_sell.size_mode
        if mode == "match_owner_a_buy":
            if self._owner_a_buy_size is not None and self._owner_a_buy_size > 0:
                return self._owner_a_buy_size
            price = self._cfg.buy.limit_price
            if price is not None and price > 0:
                return self._cfg.buy.notional_usd / price
            return Decimal("0")
        return self._cfg.owner_b_unauthorized_sell.fixed_size

    def attempt_owner_b_unauthorized_sell(
        self,
        coord: RuntimeCoordinator,
        sink: JsonlSink,
        run_id: str,
    ) -> bool:
        """Strategy-side guard: block Owner B when allocated qty is zero.

        Returns True when B was blocked as expected (no pipeline call).
        """
        if not self._cfg.owner_b_unauthorized_sell.enabled:
            self._phase = PHASE_OWNER_B_SELL_BLOCKED
            return True
        if self._phase != PHASE_OWNER_A_ALLOCATION_VISIBLE:
            return False

        tid = TokenId(self._cfg.token_id)
        planned = self._planned_owner_b_size()
        snap = inventory_snapshot(coord, tid)
        wallet_qty = Decimal(snap["wallet_position_qty"])
        allocated = clamp_planned_to_allocated(
            coord,
            owner_id=self._cfg.owner_b_id,
            token_id=tid,
            planned=planned,
        )

        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                run_id,
                {
                    "event": "allocation_test_unauthorized_sell_attempt",
                    "owner_id": self._cfg.owner_b_id,
                    "token_id": str(tid),
                    "planned_size": str(planned),
                    "allocated_available": str(
                        coord.allocation_ledger.get_available_allocated(self._cfg.owner_b_id, tid)
                        if coord.allocation_ledger is not None
                        else Decimal("0")
                    ),
                    "wallet_position_qty": str(wallet_qty),
                },
            )
        )
        self._phase = PHASE_OWNER_B_UNAUTHORIZED_SELL_ATTEMPTED

        if allocated <= 0:
            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    run_id,
                    {
                        "event": "allocation_test_unauthorized_sell_blocked",
                        "owner_id": self._cfg.owner_b_id,
                        "token_id": str(tid),
                        "planned_size": str(planned),
                        "allocated_available": "0",
                        "wallet_position_qty": str(wallet_qty),
                        "reason": "insufficient_allocation",
                    },
                )
            )
            self._phase = PHASE_OWNER_B_SELL_BLOCKED
            return True

        self._mark_failure(PHASE_OWNER_B_SELL_NOT_BLOCKED)
        return False

    def build_owner_a_sell_work_unit(
        self,
        coord: RuntimeCoordinator,
    ) -> IntentWorkUnit | None:
        if not self._cfg.enabled or not self._cfg.owner_a_sell.enabled:
            return None
        if self._phase != PHASE_OWNER_B_SELL_BLOCKED:
            return None
        if self._cfg.run_once and self._sell_work_issued:
            return None

        tid = TokenId(self._cfg.token_id)
        planned = self._owner_a_buy_size
        if planned is None or planned <= 0:
            price = self._cfg.buy.limit_price
            if price is not None and price > 0:
                planned = self._cfg.buy.notional_usd / price
            else:
                return None

        allocated_clamped = clamp_planned_to_allocated(
            coord,
            owner_id=self._cfg.owner_a_id,
            token_id=tid,
            planned=planned,
        )
        snap = inventory_snapshot(coord, tid)
        venue_avail = Decimal(snap["available_to_sell"])
        sell_size = min(planned, allocated_clamped, venue_avail)
        if sell_size <= 0:
            return None

        sell_price = self.effective_sell_limit_price
        if sell_price is None or sell_price <= 0:
            return None

        exit_int = ExitIntent(
            token_id=tid,
            side=Side.SELL,
            size=sell_size,
            limit_price=sell_price,
            order_style=self._cfg.owner_a_sell.order_style,
        )
        self._sell_work_issued = True
        self._phase = PHASE_OWNER_A_SELL_SUBMITTED
        ext: dict[str, object] = {
            "source": ALLOCATION_TEST_INTENT_SOURCE,
            "allocation_owner_id": self._cfg.owner_a_id,
            "allocation_test_phase": PHASE_OWNER_A_SELL,
            "parent_correlation_id": self._owner_a_correlation_id,
            "allocation_test_sell_pricing_mode": self._cfg.owner_a_sell.pricing_mode,
        }
        if self._sell_pricing_evidence is not None:
            ext["allocation_test_sell_pricing"] = self._sell_pricing_evidence
        return IntentWorkUnit(
            intent=exit_int,
            correlation_id=self._owner_a_correlation_id,
            intent_fact_extensions=ext,
        )

    def notify_sell_denied(self) -> None:
        if self._phase == PHASE_OWNER_A_SELL_SUBMITTED:
            self._mark_failure(PHASE_OWNER_A_SELL_DENIED)

    def notify_sell_oms_reject(self) -> None:
        if self._phase == PHASE_OWNER_A_SELL_SUBMITTED:
            self._mark_failure(PHASE_OWNER_A_SELL_OMS_REJECT)

    def notify_sell_submitted(
        self,
        match_evidence: dict[str, Any],
        *,
        shadow_instant_fill: bool = False,
    ) -> None:
        if self._phase != PHASE_OWNER_A_SELL_SUBMITTED:
            return
        self._sell_submit_succeeded = True
        if shadow_instant_fill or oms_status_is_matched(match_evidence):
            self._sell_outcome = "matched"
        else:
            self._sell_outcome = "live_resting"
        self._phase = PHASE_OWNER_A_SELL_COMPLETED
        self._phase = PHASE_DONE

    def notify_sell_completed(self) -> None:
        """Backward-compatible alias; prefer notify_sell_submitted with match evidence."""
        self.notify_sell_submitted({}, shadow_instant_fill=True)

    def verify_final_ledger(self, coord: RuntimeCoordinator) -> bool:
        """Validate ledger state matches sell outcome (matched → 0, live → reserved)."""
        if coord.allocation_ledger is None:
            return True
        tid = TokenId(self._cfg.token_id)
        ledger = coord.allocation_ledger
        owner = self._cfg.owner_a_id
        allocated = ledger.get_allocated(owner, tid)
        reserved = ledger.get_reserved(owner, tid)
        available = ledger.get_available_allocated(owner, tid)
        if self._sell_outcome == "live_resting":
            if allocated <= 0 or reserved <= 0 or available != Decimal("0"):
                self.mark_ledger_mismatch()
                return False
            return True
        if self._sell_outcome == "matched" and allocated != Decimal("0"):
            self.mark_ledger_mismatch()
            return False
        if self._sell_outcome is None and allocated != Decimal("0"):
            self.mark_ledger_mismatch()
            return False
        return True

    def ledger_snapshot(self, coord: RuntimeCoordinator) -> dict[str, str]:
        if coord.allocation_ledger is None:
            return {}
        tid = TokenId(self._cfg.token_id)
        owner = self._cfg.owner_a_id
        return {
            "allocated_qty": str(coord.allocation_ledger.get_allocated(owner, tid)),
            "reserved_exit_qty": str(coord.allocation_ledger.get_reserved(owner, tid)),
            "available_allocated": str(coord.allocation_ledger.get_available_allocated(owner, tid)),
        }
