"""Paired binary state machine + persistence (Phase 4.6)."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from tyrex_pm.core.ids import TokenId


class PairedBinaryPhase(str, Enum):
    IDLE = "IDLE"
    ENTRY_PLANNED = "ENTRY_PLANNED"
    YES_ENTRY_PENDING = "YES_ENTRY_PENDING"
    NO_ENTRY_PENDING = "NO_ENTRY_PENDING"
    BOTH_ENTRY_PENDING = "BOTH_ENTRY_PENDING"
    BOTH_LEGS_FILLED = "BOTH_LEGS_FILLED"
    ACTIVATION_PENDING_RECHECK = "ACTIVATION_PENDING_RECHECK"
    UNWIND_PENDING = "UNWIND_PENDING"
    BOTH_LEGS_ACTIVE = "BOTH_LEGS_ACTIVE"
    STOP_PENDING_YES = "STOP_PENDING_YES"
    STOP_PENDING_NO = "STOP_PENDING_NO"
    TP_PENDING_YES = "TP_PENDING_YES"
    TP_PENDING_NO = "TP_PENDING_NO"
    TIMEOUT_PENDING = "TIMEOUT_PENDING"
    ONLY_YES_ACTIVE = "ONLY_YES_ACTIVE"
    ONLY_NO_ACTIVE = "ONLY_NO_ACTIVE"
    EXITING_YES = "EXITING_YES"
    EXITING_NO = "EXITING_NO"
    EXITING_BOTH = "EXITING_BOTH"
    DONE = "DONE"
    FAILED = "FAILED"


TERMINAL_PHASES = frozenset({PairedBinaryPhase.DONE, PairedBinaryPhase.FAILED})

OPEN_EXPOSURE_PERSIST_PHASES = frozenset(
    {
        PairedBinaryPhase.BOTH_ENTRY_PENDING,
        PairedBinaryPhase.YES_ENTRY_PENDING,
        PairedBinaryPhase.NO_ENTRY_PENDING,
        PairedBinaryPhase.BOTH_LEGS_FILLED,
        PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        PairedBinaryPhase.ONLY_YES_ACTIVE,
        PairedBinaryPhase.ONLY_NO_ACTIVE,
        PairedBinaryPhase.EXITING_YES,
        PairedBinaryPhase.EXITING_NO,
        PairedBinaryPhase.EXITING_BOTH,
        PairedBinaryPhase.UNWIND_PENDING,
    }
)


@dataclass(frozen=True)
class PersistStateResult:
    success: bool
    path: Path
    tmp_path: Path | None = None
    attempts: int = 0
    error_type: str | None = None
    error_message: str | None = None
    severity: str = "warning"


def state_has_open_exposure(state: PairedBinaryRuntimeState) -> bool:
    return state.phase in OPEN_EXPOSURE_PERSIST_PHASES


def persist_failure_severity(state: PairedBinaryRuntimeState) -> str:
    return "error" if state_has_open_exposure(state) else "warning"


@dataclass
class LegRuntime:
    target_qty: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")
    allocation_final_qty: Decimal = Decimal("0")
    entry_vwap: Decimal | None = None
    entry_client_order_id: str | None = None
    leg_correlation_id: str | None = None
    triggered: bool = False
    exit_submitted: bool = False
    pending_trigger_type: str | None = None
    pending_trigger_reason: str | None = None
    last_exit_bid: Decimal | None = None
    exit_fill_price: Decimal | None = None
    entry_cash: Decimal | None = None
    entry_qty: Decimal | None = None
    entry_cash_source: str | None = None
    exit_cash: Decimal | None = None
    exit_qty: Decimal | None = None
    exit_cash_source: str | None = None


def leg_entry_avg_price(leg: LegRuntime) -> Decimal | None:
    if leg.entry_cash is not None and leg.entry_qty is not None and leg.entry_qty > 0:
        return leg.entry_cash / leg.entry_qty
    return leg.entry_vwap


def leg_exit_avg_price(leg: LegRuntime) -> Decimal | None:
    if leg.exit_cash is not None and leg.exit_qty is not None and leg.exit_qty > 0:
        return leg.exit_cash / leg.exit_qty
    return leg.exit_fill_price


@dataclass
class PairedBinaryRuntimeState:
    phase: PairedBinaryPhase = PairedBinaryPhase.IDLE
    pair_correlation_id: str | None = None
    market_id: str = ""
    owner_id: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    yes: LegRuntime = field(default_factory=LegRuntime)
    no: LegRuntime = field(default_factory=LegRuntime)
    effective_qty: Decimal = Decimal("0")
    yes_entry: Decimal | None = None
    no_entry: Decimal | None = None
    yes_target: Decimal | None = None
    no_target: Decimal | None = None
    pair_cost: Decimal | None = None
    loss_budget: Decimal | None = None
    profit_budget: Decimal | None = None
    desired_net_profit_per_pair: Decimal | None = None
    expected_pnl_total: Decimal | None = None
    yes_planned_stop: Decimal | None = None
    yes_trigger_stop: Decimal | None = None
    no_planned_stop: Decimal | None = None
    no_trigger_stop: Decimal | None = None
    yes_planned_target: Decimal | None = None
    no_planned_target: Decimal | None = None
    yes_exit: Decimal | None = None
    no_exit: Decimal | None = None
    pair_opened_ts: float | None = None
    activation_ts: float | None = None
    yes_activation_bid: Decimal | None = None
    no_activation_bid: Decimal | None = None
    pending_timeout_legs: list[str] | None = None
    entry_deadline_ts: float | None = None
    entry_price_source: str | None = None
    yes_entry_price_source: str | None = None
    no_entry_price_source: str | None = None
    activation_recheck_started_ts: float | None = None
    activation_recheck_attempts: int = 0
    unwind_block_reason: str | None = None
    unwind_attempt_count: int = 0
    pair_entry_saga_phase: str | None = None
    pair_entry_submit_deadline_ts: float | None = None
    pair_entry_resting_deadline_ts: float | None = None
    dedup_keys: set[str] = field(default_factory=set)
    survivor_leg_state: dict[str, Any] | None = None
    survival_emit_cache: dict[str, Any] = field(default_factory=dict)
    resolution_exit_reported: bool = False

    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES


def persistence_path(state_dir: Path, owner_id: str, market_id: str) -> Path:
    safe_market = market_id.replace("/", "_").replace("\\", "_")
    return state_dir / "paired_binary" / owner_id / f"{safe_market}.json"


def _dec_str(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _leg_from_dict(d: dict[str, Any] | None) -> LegRuntime:
    if not d:
        return LegRuntime()
    return LegRuntime(
        target_qty=_dec_str(d.get("target_qty")) or Decimal("0"),
        filled_qty=_dec_str(d.get("filled_qty")) or Decimal("0"),
        allocation_final_qty=_dec_str(d.get("allocation_final_qty")) or Decimal("0"),
        entry_vwap=_dec_str(d.get("entry_vwap")),
        entry_client_order_id=d.get("entry_client_order_id"),
        leg_correlation_id=d.get("leg_correlation_id"),
        triggered=bool(d.get("triggered", False)),
        exit_submitted=bool(d.get("exit_submitted", False)),
        pending_trigger_type=d.get("pending_trigger_type"),
        pending_trigger_reason=d.get("pending_trigger_reason"),
        last_exit_bid=_dec_str(d.get("last_exit_bid")),
        exit_fill_price=_dec_str(d.get("exit_fill_price")),
        entry_cash=_dec_str(d.get("entry_cash")),
        entry_qty=_dec_str(d.get("entry_qty")),
        entry_cash_source=d.get("entry_cash_source"),
        exit_cash=_dec_str(d.get("exit_cash")),
        exit_qty=_dec_str(d.get("exit_qty")),
        exit_cash_source=d.get("exit_cash_source"),
    )


def load_persisted_state(path: Path) -> PairedBinaryRuntimeState | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    phase_raw = str(raw.get("state") or raw.get("phase") or "IDLE")
    try:
        phase = PairedBinaryPhase(phase_raw)
    except ValueError:
        phase = PairedBinaryPhase.IDLE
    return PairedBinaryRuntimeState(
        phase=phase,
        pair_correlation_id=raw.get("pair_correlation_id"),
        market_id=str(raw.get("market_id", "")),
        owner_id=str(raw.get("owner_id", "")),
        yes_token_id=str(raw.get("yes_token_id", "")),
        no_token_id=str(raw.get("no_token_id", "")),
        yes=_leg_from_dict(raw.get("yes")),
        no=_leg_from_dict(raw.get("no")),
        effective_qty=_dec_str(raw.get("effective_qty")) or Decimal("0"),
        yes_entry=_dec_str(raw.get("yes_entry")),
        no_entry=_dec_str(raw.get("no_entry")),
        yes_target=_dec_str(raw.get("yes_target")),
        no_target=_dec_str(raw.get("no_target")),
        pair_cost=_dec_str(raw.get("pair_cost")),
        loss_budget=_dec_str(raw.get("loss_budget")),
        profit_budget=_dec_str(raw.get("profit_budget")),
        desired_net_profit_per_pair=_dec_str(raw.get("desired_net_profit_per_pair")),
        expected_pnl_total=_dec_str(raw.get("expected_pnl_total")),
        yes_planned_stop=_dec_str(raw.get("yes_planned_stop")),
        yes_trigger_stop=_dec_str(raw.get("yes_trigger_stop")),
        no_planned_stop=_dec_str(raw.get("no_planned_stop")),
        no_trigger_stop=_dec_str(raw.get("no_trigger_stop")),
        yes_planned_target=_dec_str(raw.get("yes_planned_target")),
        no_planned_target=_dec_str(raw.get("no_planned_target")),
        yes_exit=_dec_str(raw.get("yes_exit")),
        no_exit=_dec_str(raw.get("no_exit")),
        pair_opened_ts=raw.get("pair_opened_ts"),
        activation_ts=raw.get("activation_ts"),
        yes_activation_bid=_dec_str(raw.get("yes_activation_bid")),
        no_activation_bid=_dec_str(raw.get("no_activation_bid")),
        pending_timeout_legs=raw.get("pending_timeout_legs"),
        entry_deadline_ts=raw.get("entry_deadline_ts"),
        entry_price_source=raw.get("entry_price_source"),
        yes_entry_price_source=raw.get("yes_entry_price_source"),
        no_entry_price_source=raw.get("no_entry_price_source"),
        activation_recheck_started_ts=raw.get("activation_recheck_started_ts"),
        activation_recheck_attempts=int(raw.get("activation_recheck_attempts", 0) or 0),
        unwind_block_reason=raw.get("unwind_block_reason"),
        unwind_attempt_count=int(raw.get("unwind_attempt_count", 0) or 0),
        pair_entry_saga_phase=raw.get("pair_entry_saga_phase"),
        pair_entry_submit_deadline_ts=raw.get("pair_entry_submit_deadline_ts"),
        pair_entry_resting_deadline_ts=raw.get("pair_entry_resting_deadline_ts"),
        survivor_leg_state=raw.get("survivor_leg_state"),
    )


def save_persisted_state(
    path: Path,
    state: PairedBinaryRuntimeState,
    *,
    max_attempts: int = 5,
    retry_base_delay_s: float = 0.025,
) -> PersistStateResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "state": state.phase.value,
        "pair_correlation_id": state.pair_correlation_id,
        "market_id": state.market_id,
        "owner_id": state.owner_id,
        "yes_token_id": state.yes_token_id,
        "no_token_id": state.no_token_id,
        "yes": {
            "target_qty": str(state.yes.target_qty),
            "filled_qty": str(state.yes.filled_qty),
            "allocation_final_qty": str(state.yes.allocation_final_qty),
            "entry_vwap": str(state.yes.entry_vwap) if state.yes.entry_vwap else None,
            "entry_client_order_id": state.yes.entry_client_order_id,
            "leg_correlation_id": state.yes.leg_correlation_id,
            "triggered": state.yes.triggered,
            "exit_submitted": state.yes.exit_submitted,
            "pending_trigger_type": state.yes.pending_trigger_type,
            "pending_trigger_reason": state.yes.pending_trigger_reason,
            "last_exit_bid": str(state.yes.last_exit_bid) if state.yes.last_exit_bid else None,
            "exit_fill_price": str(state.yes.exit_fill_price) if state.yes.exit_fill_price else None,
            "entry_cash": str(state.yes.entry_cash) if state.yes.entry_cash is not None else None,
            "entry_qty": str(state.yes.entry_qty) if state.yes.entry_qty is not None else None,
            "entry_cash_source": state.yes.entry_cash_source,
            "exit_cash": str(state.yes.exit_cash) if state.yes.exit_cash is not None else None,
            "exit_qty": str(state.yes.exit_qty) if state.yes.exit_qty is not None else None,
            "exit_cash_source": state.yes.exit_cash_source,
        },
        "no": {
            "target_qty": str(state.no.target_qty),
            "filled_qty": str(state.no.filled_qty),
            "allocation_final_qty": str(state.no.allocation_final_qty),
            "entry_vwap": str(state.no.entry_vwap) if state.no.entry_vwap else None,
            "entry_client_order_id": state.no.entry_client_order_id,
            "leg_correlation_id": state.no.leg_correlation_id,
            "triggered": state.no.triggered,
            "exit_submitted": state.no.exit_submitted,
            "pending_trigger_type": state.no.pending_trigger_type,
            "pending_trigger_reason": state.no.pending_trigger_reason,
            "last_exit_bid": str(state.no.last_exit_bid) if state.no.last_exit_bid else None,
            "exit_fill_price": str(state.no.exit_fill_price) if state.no.exit_fill_price else None,
            "entry_cash": str(state.no.entry_cash) if state.no.entry_cash is not None else None,
            "entry_qty": str(state.no.entry_qty) if state.no.entry_qty is not None else None,
            "entry_cash_source": state.no.entry_cash_source,
            "exit_cash": str(state.no.exit_cash) if state.no.exit_cash is not None else None,
            "exit_qty": str(state.no.exit_qty) if state.no.exit_qty is not None else None,
            "exit_cash_source": state.no.exit_cash_source,
        },
        "effective_qty": str(state.effective_qty),
        "yes_entry": str(state.yes_entry) if state.yes_entry is not None else None,
        "no_entry": str(state.no_entry) if state.no_entry is not None else None,
        "yes_target": str(state.yes_target) if state.yes_target is not None else None,
        "no_target": str(state.no_target) if state.no_target is not None else None,
        "pair_cost": str(state.pair_cost) if state.pair_cost is not None else None,
        "loss_budget": str(state.loss_budget) if state.loss_budget is not None else None,
        "profit_budget": str(state.profit_budget) if state.profit_budget is not None else None,
        "desired_net_profit_per_pair": (
            str(state.desired_net_profit_per_pair)
            if state.desired_net_profit_per_pair is not None
            else None
        ),
        "expected_pnl_total": str(state.expected_pnl_total) if state.expected_pnl_total is not None else None,
        "yes_planned_stop": str(state.yes_planned_stop) if state.yes_planned_stop is not None else None,
        "yes_trigger_stop": str(state.yes_trigger_stop) if state.yes_trigger_stop is not None else None,
        "no_planned_stop": str(state.no_planned_stop) if state.no_planned_stop is not None else None,
        "no_trigger_stop": str(state.no_trigger_stop) if state.no_trigger_stop is not None else None,
        "yes_planned_target": str(state.yes_planned_target) if state.yes_planned_target is not None else None,
        "no_planned_target": str(state.no_planned_target) if state.no_planned_target is not None else None,
        "yes_exit": str(state.yes_exit) if state.yes_exit is not None else None,
        "no_exit": str(state.no_exit) if state.no_exit is not None else None,
        "pair_opened_ts": state.pair_opened_ts,
        "activation_ts": state.activation_ts,
        "yes_activation_bid": str(state.yes_activation_bid) if state.yes_activation_bid is not None else None,
        "no_activation_bid": str(state.no_activation_bid) if state.no_activation_bid is not None else None,
        "pending_timeout_legs": state.pending_timeout_legs,
        "entry_deadline_ts": state.entry_deadline_ts,
        "entry_price_source": state.entry_price_source,
        "yes_entry_price_source": state.yes_entry_price_source,
        "no_entry_price_source": state.no_entry_price_source,
        "activation_recheck_started_ts": state.activation_recheck_started_ts,
        "activation_recheck_attempts": state.activation_recheck_attempts,
        "unwind_block_reason": state.unwind_block_reason,
        "unwind_attempt_count": state.unwind_attempt_count,
        "pair_entry_saga_phase": state.pair_entry_saga_phase,
        "pair_entry_submit_deadline_ts": state.pair_entry_submit_deadline_ts,
        "pair_entry_resting_deadline_ts": state.pair_entry_resting_deadline_ts,
        "survivor_leg_state": state.survivor_leg_state,
    }
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    severity = persist_failure_severity(state)
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _safe_unlink(tmp)
        return PersistStateResult(
            success=False,
            path=path,
            tmp_path=tmp,
            attempts=0,
            error_type=type(exc).__name__,
            error_message=str(exc),
            severity=severity,
        )

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            os.replace(tmp, path)
            return PersistStateResult(success=True, path=path, tmp_path=tmp, attempts=attempt, severity=severity)
        except (PermissionError, OSError) as exc:
            winerr = getattr(exc, "winerror", None)
            if winerr not in (5, 32, None) and not isinstance(exc, PermissionError):
                last_error = exc
                break
            last_error = exc
            if attempt < max_attempts:
                time.sleep(retry_base_delay_s * attempt)

    _safe_unlink(tmp)
    err = last_error or RuntimeError("persist replace failed")
    return PersistStateResult(
        success=False,
        path=path,
        tmp_path=tmp,
        attempts=max_attempts,
        error_type=type(err).__name__,
        error_message=str(err),
        severity=severity,
    )


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            os.unlink(path)
    except OSError:
        pass


def yes_token(state: PairedBinaryRuntimeState) -> TokenId:
    return TokenId(state.yes_token_id)


def no_token(state: PairedBinaryRuntimeState) -> TokenId:
    return TokenId(state.no_token_id)
