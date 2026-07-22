"""N7 one-shot Scope A host — wraps generic N6LiveHost.

Operator authorization is the CLI ``--live`` invocation itself.
No envelopes, phrases, or nonces. Mutations default OFF until explicitly armed
for FakeTransport (tests) or operator live (non-CI).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.core.clock import Clock
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, new_intent_id
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.execution.polymarket.mutation_transport import MutationArmToken
from tyrex_pm.execution.polymarket.transport import PolymarketTransport
from tyrex_pm.runtime.live_config import LiveConfig, LiveScope
from tyrex_pm.runtime.n6_authorization import MutationAuthorization
from tyrex_pm.runtime.n6_live_host import N6LiveHost
from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_sealed import N7SealedConfig
from tyrex_pm.runtime.n7_sizing import (
    FeeInclusiveEntrySize,
    FeeInclusiveSizeSkip,
    size_fee_inclusive_entry,
)


def _ci_forbids_live() -> bool:
    return bool(
        os.environ.get("CI")
        or os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("TYREX_N7_FORBID_LIVE") == "1"
    )


@dataclass
class N7OneShotHost:
    sealed: N7SealedConfig
    clock: Clock
    transport: PolymarketTransport
    market: BinaryMarket
    persistence_path: Path | None = None
    min_valid_order_notional: Decimal = Decimal("1")
    fee_curve: FeeCurveParams = field(
        default_factory=lambda: FeeCurveParams(
            fee_rate=Decimal("0.07"), exponent=Decimal("1")
        )
    )
    inner: N6LiveHost | None = None
    entry_lineages: int = 0
    daily_entry_notional: Decimal = Decimal("0")
    daily_realized_loss: Decimal = Decimal("0")
    exit_attempts: int = 0
    exit_ladder_started_at: datetime | None = None
    terminated: bool = False
    mutations_force_off: bool = False
    operator_live_armed: bool = False
    bound_market_id: str | None = None
    window_id: str | None = None
    last_entry_sizing: dict[str, Any] | None = None
    facts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    last_abort: N7AbortCode | None = None

    def __post_init__(self) -> None:
        if self.sealed.live.scope is not LiveScope.A:
            raise ValueError("N7OneShotHost requires scope=A")
        ladder = self.sealed.timing.build_ladder(event_end=self.market.event_end)
        live = LiveConfig(
            enabled=False,
            mutations_enabled=False,
            scope=LiveScope.A,
            ack_timeout_ms=self.sealed.timing.ack_timeout_ms,
            max_order_notional=self.sealed.max_buy_collateral,
            order_style=self.sealed.order_style,
            hard_collateral_cap=self.sealed.max_buy_collateral,
        )
        self.inner = N6LiveHost(
            live=live,
            clock=self.clock,
            transport=self.transport,
            market=self.market,
            ladder=ladder,
            authorization=None,
            persistence_path=self.persistence_path,
            min_valid_order_notional=self.min_valid_order_notional,
        )
        self.window_id = self.market.market_id.value
        self.bound_market_id = self.market.market_id.value
        self._fact(
            "n7_host_started",
            {
                "config_fingerprint": self.sealed.fingerprint(),
                "market_id": self.market.market_id.value,
                "mutations_armed": False,
                "ceremony": "none_operator_cli_is_authorization",
            },
        )

    def _fact(self, fact_type: str, payload: dict[str, Any]) -> None:
        self.facts.append((fact_type, payload))
        if self.inner is not None:
            self.inner._fact(fact_type, payload)

    def _now(self) -> datetime:
        return self.clock.now_utc()

    def _enable_live_config(self) -> None:
        assert self.inner is not None
        self.inner.live = LiveConfig(
            enabled=True,
            mutations_enabled=True,
            scope=LiveScope.A,
            ack_timeout_ms=self.sealed.timing.ack_timeout_ms,
            max_order_notional=self.sealed.max_buy_collateral,
            order_style=self.sealed.order_style,
            hard_collateral_cap=self.sealed.max_buy_collateral,
        )

    def _mutations_ready(self) -> bool:
        if self.mutations_force_off:
            return False
        assert self.inner is not None and self.inner.oms is not None
        if self.operator_live_armed:
            return bool(self.inner.oms.mutations_enabled)
        return self.inner._mutations_armed()

    def disable_mutations(self, *, reason: str = "terminal") -> None:
        self.mutations_force_off = True
        self.operator_live_armed = False
        assert self.inner is not None
        self.inner.authorization = None
        self.inner.live = LiveConfig(
            enabled=False,
            mutations_enabled=False,
            scope=LiveScope.A,
            ack_timeout_ms=self.sealed.timing.ack_timeout_ms,
            max_order_notional=self.sealed.max_buy_collateral,
            order_style=self.sealed.order_style,
            hard_collateral_cap=self.sealed.max_buy_collateral,
        )
        if self.inner.oms is not None:
            self.inner.oms.mutations_enabled = False
        # Disarm SDK network if present
        disable = getattr(self.transport, "disable_network", None)
        if callable(disable):
            disable()
        self._fact("n7_mutations_disabled", {"reason": reason})

    def refuse_forbidden(self, action: str) -> dict[str, Any]:
        code_map = {
            "scope_b": N7AbortCode.SCOPE_B_REFUSED,
            "hold_to_resolution": N7AbortCode.HOLD_TO_RESOLUTION_REFUSED,
            "redeem": N7AbortCode.REDEEM_REFUSED,
            "cancel_all": N7AbortCode.CANCEL_ALL_REFUSED,
            "allowance_change": N7AbortCode.ALLOWANCE_MUTATION_REFUSED,
            "transfer": N7AbortCode.TRANSFER_REFUSED,
            "historical_as_strategy": N7AbortCode.HISTORICAL_AS_STRATEGY_REFUSED,
        }
        code = code_map.get(action, N7AbortCode.AUTHORIZATION_MISMATCH)
        self.last_abort = code
        out = {"refused": True, "action": action, "abort": code.value}
        self._fact("n7_forbidden_refused", out)
        return out

    def arm_fake(self) -> N7AbortCode | None:
        """Arm FakeTransport mutations for deterministic tests / rehearsal."""
        if self.mutations_force_off:
            return N7AbortCode.CRASH_MUTATIONS_DISABLED
        if not isinstance(self.transport, FakeTransport):
            self.last_abort = N7AbortCode.AUTHORIZATION_MISMATCH
            return self.last_abort
        self._enable_live_config()
        assert self.inner is not None
        self.inner.authorization = MutationAuthorization.for_fake_transport(
            reason="n7_fake_oneshot"
        )
        if self.inner.oms is not None:
            self.inner.oms.mutations_enabled = True
        self.operator_live_armed = False
        self._fact("n7_armed_fake", {})
        return None

    def arm_operator_live(self) -> N7AbortCode | None:
        """Arm real venue mutations. Operator CLI ``--live`` only; CI refused."""
        if self.mutations_force_off:
            return N7AbortCode.CRASH_MUTATIONS_DISABLED
        if _ci_forbids_live():
            self.last_abort = N7AbortCode.AUTHORIZATION_MISMATCH
            return self.last_abort
        if isinstance(self.transport, FakeTransport):
            self.last_abort = N7AbortCode.AUTHORIZATION_MISMATCH
            return self.last_abort
        enable = getattr(self.transport, "enable_network", None)
        if callable(enable):
            enable(
                MutationArmToken(
                    artifact_id=f"n7-operator-live-{uuid.uuid4()}",
                    allow_network=True,
                )
            )
        self._enable_live_config()
        assert self.inner is not None and self.inner.oms is not None
        # Bypass N6 fake-only MutationAuthorization; OMS flag is the gate.
        self.inner.authorization = None
        self.inner.oms.mutations_enabled = True
        self.operator_live_armed = True
        self._fact("n7_armed_operator_live", {"transport": type(self.transport).__name__})
        return None

    def refuse_second_window(self, other_market_id: str) -> dict[str, Any]:
        self.last_abort = N7AbortCode.SECOND_WINDOW_REFUSED
        out = {
            "refused": True,
            "abort": self.last_abort.value,
            "bound": self.bound_market_id,
            "requested": other_market_id,
        }
        self._fact("n7_second_window_refused", out)
        return out

    def size_entry(
        self, *, worst_price: Decimal, desired_share_notional: Decimal | None = None
    ) -> FeeInclusiveEntrySize | FeeInclusiveSizeSkip:
        return size_fee_inclusive_entry(
            worst_price=worst_price,
            collateral_cap=self.sealed.max_buy_collateral,
            fee_curve=self.fee_curve,
            min_valid_order_notional=self.min_valid_order_notional,
            desired_share_notional=desired_share_notional,
        )

    def try_enter(
        self,
        intent: EnterIntent,
        *,
        book: BookSnapshot,
        worst_price: Decimal | None = None,
    ) -> dict[str, Any]:
        assert self.inner is not None
        if self.terminated:
            self.last_abort = N7AbortCode.SECOND_WINDOW_REFUSED
            return {"status": "ABORT", "abort": self.last_abort.value}
        if self.bound_market_id != self.market.market_id.value:
            self.last_abort = N7AbortCode.MARKET_OUTSIDE_ENVELOPE
            return {"status": "ABORT", "abort": self.last_abort.value}
        if self.entry_lineages >= self.sealed.max_entry_lineages:
            self.last_abort = N7AbortCode.REENTRY_REFUSED
            return {"status": "ABORT", "abort": self.last_abort.value}

        ask = worst_price
        if ask is None:
            ba = book.best_ask
            ask = None if ba is None else ba.price
        if ask is None:
            self.last_abort = N7AbortCode.CRITICAL_FEED_STALE
            return {"status": "SKIP", "abort": self.last_abort.value, "reasons": ["no_ask"]}

        sized = self.size_entry(
            worst_price=ask, desired_share_notional=intent.target_notional
        )
        if isinstance(sized, FeeInclusiveSizeSkip):
            self.last_abort = N7AbortCode.VENUE_MINIMUM_ABOVE_CAP
            self.last_entry_sizing = sized.to_dict()
            return {
                "status": "SKIP",
                "abort": self.last_abort.value,
                "reasons": [sized.reason],
                "sizing": sized.to_dict(),
            }
        self.last_entry_sizing = sized.to_dict()
        if sized.max_fee_inclusive_debit > self.sealed.max_buy_collateral:
            self.last_abort = N7AbortCode.VENUE_MINIMUM_ABOVE_CAP
            return {
                "status": "SKIP",
                "abort": self.last_abort.value,
                "reasons": ["fee_inclusive_exceeds_cap"],
                "sizing": sized.to_dict(),
            }

        # Replace intent notional with fee-inclusive-safe share notional.
        from dataclasses import replace

        sized_intent = replace(intent, target_notional=sized.share_notional)

        if self.daily_entry_notional + sized.share_notional > self.sealed.max_daily_notional:
            self.last_abort = N7AbortCode.RISK_OR_DAILY_LIMIT_BREACH
            return {"status": "ABORT", "abort": self.last_abort.value}
        if self.daily_realized_loss >= self.sealed.max_daily_loss:
            self.last_abort = N7AbortCode.RISK_OR_DAILY_LIMIT_BREACH
            return {"status": "ABORT", "abort": self.last_abort.value}

        if not self._mutations_ready():
            self.last_abort = N7AbortCode.AUTHORIZATION_ABSENT
            return {
                "status": "ABORT",
                "abort": self.last_abort.value,
                "reasons": ["mutations_not_armed"],
            }

        result = self.inner.try_enter(sized_intent, book=book)
        result = dict(result)
        result["sizing"] = sized.to_dict()
        if result.get("status") in {
            "ACKNOWLEDGED",
            "DISPATCHED",
            "AMBIGUOUS",
            "REJECTED",
        }:
            self.entry_lineages += 1
            self.daily_entry_notional += sized.share_notional
        if result.get("status") == "SKIP" and "late_entry_skipped" in (
            result.get("reasons") or []
        ):
            self.last_abort = N7AbortCode.LATE_ENTRY
        if result.get("status") == "SKIP" and "kill_active" in (result.get("reasons") or []):
            self.last_abort = N7AbortCode.KILL_ACTIVE
        # Post-submit: if plan notional + fee would exceed (defense in depth)
        if result.get("limit_price") and result.get("status") == "ACKNOWLEDGED":
            from tyrex_pm.runtime.n7_sizing import conservative_entry_fee_for

            lim = Decimal(str(result["limit_price"]))
            # quantity implied from share notional
            qty = sized.quantity
            fee = conservative_entry_fee_for(
                quantity=qty, price=lim, fee_curve=self.fee_curve
            )
            if lim * qty + fee > self.sealed.max_buy_collateral + Decimal("0.000001"):
                self._fact(
                    "n7_fee_inclusive_breach_detected",
                    {"limit": str(lim), "qty": str(qty), "fee": str(fee)},
                )
        return result

    def try_exit(
        self,
        intent: ExitIntent | FlattenIntent,
        *,
        book: BookSnapshot,
        limit_price: Decimal,
        quantity: Decimal | None = None,
    ) -> dict[str, Any]:
        assert self.inner is not None
        # Exit fees never block inventory-reducing exits.
        return self.inner.try_exit(
            intent, book=book, limit_price=limit_price, quantity=quantity
        )

    def run_bounded_exit_ladder(
        self,
        *,
        book: BookSnapshot,
        limit_price: Decimal,
        strategy_stale: bool = False,
    ) -> dict[str, Any]:
        assert self.inner is not None
        assert self.inner.portfolio is not None
        if self.exit_ladder_started_at is None:
            self.exit_ladder_started_at = self._now()
        attempts: list[dict[str, Any]] = []
        budget = timedelta(milliseconds=self.sealed.timing.exit_retry_time_budget_ms)
        max_attempts = self.sealed.timing.exit_retry_max_attempts

        while True:
            if self.inner.portfolio.is_flat():
                self.last_abort = N7AbortCode.TERMINAL_FLAT
                out = {
                    "status": "FLAT",
                    "attempts": attempts,
                    "strategy_stale_ok": strategy_stale,
                }
                self._fact("n7_exit_ladder_flat", out)
                return out
            if self.inner.ladder.past_hard_stop(self._now()) or (
                self._now() >= self.inner.ladder.residual_operator_deadline_at
            ):
                self.last_abort = N7AbortCode.RESIDUAL_EXPOSURE_AT_DEADLINE
                out = {
                    "status": "RESIDUAL",
                    "abort": self.last_abort.value,
                    "attempts": attempts,
                    "flat": False,
                }
                self._fact("n7_exit_ladder_residual", out)
                return out
            if self.exit_attempts >= max_attempts:
                self.last_abort = N7AbortCode.EXIT_RETRY_BUDGET_EXHAUSTED
                return {
                    "status": "BUDGET_EXHAUSTED",
                    "abort": self.last_abort.value,
                    "attempts": attempts,
                }
            if self._now() - self.exit_ladder_started_at > budget:
                self.last_abort = N7AbortCode.EXIT_RETRY_BUDGET_EXHAUSTED
                return {
                    "status": "TIME_BUDGET_EXHAUSTED",
                    "abort": self.last_abort.value,
                    "attempts": attempts,
                }

            if self.inner._has_ambiguous_lineage():
                self.inner.resolve_ambiguous_via_recon()
                if self.inner._has_ambiguous_lineage():
                    self.last_abort = N7AbortCode.AMBIGUOUS_ACKNOWLEDGMENT
                    return {
                        "status": "AMBIGUOUS",
                        "abort": self.last_abort.value,
                        "attempts": attempts,
                    }

            if self.inner.inventory_unknown:
                self.last_abort = N7AbortCode.UNKNOWN_INVENTORY
                return {
                    "status": "UNKNOWN",
                    "abort": self.last_abort.value,
                    "attempts": attempts,
                }

            from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState

            if self.inner.lifecycle is not None and self.inner.lifecycle.state in {
                LifecycleState.EXIT_PENDING,
                LifecycleState.ENTRY_PENDING,
            }:
                self.exit_attempts += 1
                attempts.append(
                    {
                        "status": "WAIT_PENDING",
                        "lifecycle": self.inner.lifecycle.state.value,
                    }
                )
                if self.exit_attempts >= max_attempts:
                    self.last_abort = N7AbortCode.EXIT_RETRY_BUDGET_EXHAUSTED
                    return {
                        "status": "BUDGET_EXHAUSTED",
                        "abort": self.last_abort.value,
                        "attempts": attempts,
                    }
                continue

            intent = FlattenIntent(
                intent_id=new_intent_id(),
                strategy_id=self.inner.strategy_id,
                market_id=self.market.market_id,
                instrument_id=self.market.yes.instrument_id,
                created_at=self._now(),
                correlation_id=self.inner._correlation,
                causation_id=None,
                reason_code="n7_bounded_exit_ladder",
            )
            self.exit_attempts += 1
            result = self.try_exit(intent, book=book, limit_price=limit_price)
            attempts.append(result)
            if result.get("status") in {"ACKNOWLEDGED", "DISPATCHED", "AMBIGUOUS"}:
                return {
                    "status": "EXIT_SUBMITTED",
                    "result": result,
                    "attempts": attempts,
                    "strategy_stale_ok": strategy_stale,
                }
            if result.get("status") == "SKIP" and self.inner.portfolio.is_flat():
                self.last_abort = N7AbortCode.TERMINAL_FLAT
                return {"status": "FLAT", "attempts": attempts}

    def terminate(self, *, reason: str = "one_shot_complete") -> None:
        self.terminated = True
        self.disable_mutations(reason=reason)
        self._fact("n7_terminated", {"reason": reason})

    def handle_ctrl_c(self, *, book: BookSnapshot, limit_price: Decimal) -> dict[str, Any]:
        self.last_abort = N7AbortCode.CTRL_C_ABORT
        assert self.inner is not None
        self.inner.activate_kill()
        if self.inner.portfolio is not None and not self.inner.portfolio.is_flat():
            ladder = self.run_bounded_exit_ladder(book=book, limit_price=limit_price)
        else:
            ladder = {"status": "FLAT"}
        self.disable_mutations(reason="ctrl_c")
        return {"abort": self.last_abort.value, "ladder": ladder}

    def on_crash(self) -> dict[str, Any]:
        self.disable_mutations(reason="crash")
        self.last_abort = N7AbortCode.CRASH_MUTATIONS_DISABLED
        return {"abort": self.last_abort.value, "mutations_armed": False}

    def economics_report(self) -> dict[str, Any]:
        assert self.inner is not None and self.inner.portfolio is not None
        post = self.inner.post_trade_reconcile()
        return {
            "sizing": self.last_entry_sizing,
            "share_notional_authorized": (
                None
                if self.last_entry_sizing is None
                else self.last_entry_sizing.get("share_notional")
            ),
            "conservative_entry_fee_estimate": (
                None
                if self.last_entry_sizing is None
                else self.last_entry_sizing.get("conservative_entry_fee")
            ),
            "max_fee_inclusive_entry_debit": (
                None
                if self.last_entry_sizing is None
                else self.last_entry_sizing.get("max_fee_inclusive_debit")
            ),
            "post_trade": post,
            "remaining_inventory": post.get("residual"),
            "realized_pnl": post.get("realized_pnl"),
            "fees": post.get("fees"),
            "fee_label": post.get("fee_label"),
            "flat": post.get("flat"),
        }

    def status(self) -> dict[str, Any]:
        assert self.inner is not None
        return {
            "n7": self.sealed.to_dict(),
            "bound_market_id": self.bound_market_id,
            "entry_lineages": self.entry_lineages,
            "daily_entry_notional": str(self.daily_entry_notional),
            "terminated": self.terminated,
            "mutations_force_off": self.mutations_force_off,
            "operator_live_armed": self.operator_live_armed,
            "mutations_ready": self._mutations_ready(),
            "last_abort": None if self.last_abort is None else self.last_abort.value,
            "last_entry_sizing": self.last_entry_sizing,
            "inner": self.inner.status(),
        }
