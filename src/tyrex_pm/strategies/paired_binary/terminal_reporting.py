"""Terminal run reporting for paired-binary FAILED / incomplete unwind paths."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig
from tyrex_pm.strategies.paired_binary.activation_flow import leg_inventory_qty
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

_EMERGENCY_UNWIND_FACTS = frozenset(
    {
        "paired_binary_emergency_unwind_started",
        "paired_binary_emergency_unwind_attempt",
        "paired_binary_emergency_unwind_done",
        "paired_binary_emergency_unwind_blocked",
        "paired_binary_emergency_unwind_retry",
    }
)
_SURVIVOR_PHASES = frozenset(
    {
        "BOTH_LEGS_ACTIVE",
        "ONLY_YES_ACTIVE",
        "ONLY_NO_ACTIVE",
        "STOP_PENDING_YES",
        "STOP_PENDING_NO",
        "TP_PENDING_YES",
        "TP_PENDING_NO",
        "TIMEOUT_PENDING",
        "EXITING_YES",
        "EXITING_NO",
        "EXITING_BOTH",
    }
)


def _fact_types(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r.get("fact_type")) for r in rows}


def _wallet_qty(coord, token_id: str) -> Decimal | None:
    wallet = getattr(coord, "wallet", None)
    if wallet is None:
        return None
    pos = wallet.positions.get(TokenId(str(token_id)))
    return pos.qty if pos is not None else Decimal("0")


def _infer_terminal_reason(state: PairedBinaryRuntimeState, rows: list[dict[str, Any]]) -> str:
    if state.unwind_block_reason:
        return str(state.unwind_block_reason)
    for fact_type in (
        "paired_binary_activation_rejected_loss_budget",
        "paired_binary_emergency_unwind_done",
        "paired_binary_manual_intervention_required",
    ):
        for row in reversed(rows):
            if row.get("fact_type") != fact_type:
                continue
            payload = row.get("payload") or {}
            reason = payload.get("reason")
            if reason:
                return str(reason)
    for row in reversed(rows):
        if row.get("fact_type") != "paired_binary_state_change":
            continue
        payload = row.get("payload") or {}
        reason = payload.get("reason")
        if reason:
            return str(reason)
    return state.phase.value.lower()


def _entry_committed(rows: list[dict[str, Any]]) -> bool:
    if "paired_binary_pair_entry_committed" in _fact_types(rows):
        return True
    for row in rows:
        if row.get("fact_type") != "paired_binary_state_change":
            continue
        payload = row.get("payload") or {}
        to_state = payload.get("state") or payload.get("to")
        if to_state in {"BOTH_ENTRY_PENDING", "BOTH_LEGS_FILLED", "BOTH_LEGS_ACTIVE"}:
            return True
    return False


def _both_legs_filled(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        if row.get("fact_type") != "paired_binary_state_change":
            continue
        payload = row.get("payload") or {}
        to_state = payload.get("state") or payload.get("to")
        if to_state == "BOTH_LEGS_FILLED":
            return True
    return False


def _activation_succeeded(rows: list[dict[str, Any]]) -> bool:
    if "paired_binary_monitor_started" in _fact_types(rows):
        return True
    for row in rows:
        if row.get("fact_type") != "paired_binary_state_change":
            continue
        payload = row.get("payload") or {}
        to_state = payload.get("state") or payload.get("to")
        if to_state == "BOTH_LEGS_ACTIVE":
            return True
    return False


def survivor_phase_reached(rows: list[dict[str, Any]]) -> bool:
    types = _fact_types(rows)
    if any(ft.startswith("survivor_") for ft in types):
        return True
    if "paired_binary_monitor_started" not in types:
        return False
    for row in rows:
        if row.get("fact_type") != "paired_binary_state_change":
            continue
        payload = row.get("payload") or {}
        to_state = payload.get("state") or payload.get("to")
        if to_state in _SURVIVOR_PHASES:
            return True
    return False


def activation_unwind_failure_pattern(rows: list[dict[str, Any]]) -> bool:
    loop = None
    for row in reversed(rows):
        if row.get("fact_type") != "health":
            continue
        payload = row.get("payload") or {}
        if payload.get("event") == "paired_binary_loop_stopped":
            loop = payload
            break
    if loop is None or str(loop.get("final_state", "")).upper() != "FAILED":
        return False
    if not _entry_committed(rows):
        return False
    if _activation_succeeded(rows):
        return False
    types = _fact_types(rows)
    if not types.intersection(_EMERGENCY_UNWIND_FACTS):
        return False
    if survivor_phase_reached(rows):
        return False
    return True


def _unwind_status(
    *,
    rows: list[dict[str, Any]],
    allocated_yes: Decimal,
    allocated_no: Decimal,
    manual_intervention: bool,
) -> str:
    types = _fact_types(rows)
    attempted = bool(types.intersection(_EMERGENCY_UNWIND_FACTS))
    if not attempted:
        return "not_attempted"
    flat = allocated_yes <= 0 and allocated_no <= 0
    if flat and "paired_binary_emergency_unwind_done" in types:
        return "completed_flat"
    if flat:
        return "completed_uncertain"
    if manual_intervention or "paired_binary_manual_intervention_required" in types:
        return "failed"
    if attempted:
        return "attempted"
    return "not_attempted"


def _flatness_verified(
    *,
    allocated_yes: Decimal,
    allocated_no: Decimal,
    wallet_yes: Decimal | None,
    wallet_no: Decimal | None,
) -> tuple[bool, str]:
    ledger_flat = allocated_yes <= 0 and allocated_no <= 0
    if wallet_yes is None and wallet_no is None:
        return ledger_flat, "allocation_ledger"
    wallet_flat = (wallet_yes or Decimal("0")) <= 0 and (wallet_no or Decimal("0")) <= 0
    if ledger_flat and wallet_flat:
        return True, "wallet_sync"
    if ledger_flat:
        return True, "allocation_ledger"
    if wallet_flat:
        return True, "wallet_sync"
    return False, "unknown"


def build_terminal_summary_payload(
    *,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    app: AppConfig,
    coord,
    fact_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    allocated_yes = leg_inventory_qty(coord, cfg, "yes")
    allocated_no = leg_inventory_qty(coord, cfg, "no")
    wallet_yes = _wallet_qty(coord, cfg.yes_token_id)
    wallet_no = _wallet_qty(coord, cfg.no_token_id)
    types = _fact_types(fact_rows)
    emergency_attempted = bool(types.intersection(_EMERGENCY_UNWIND_FACTS))
    emergency_done = "paired_binary_emergency_unwind_done" in types
    manual_intervention = (
        "paired_binary_manual_intervention_required" in types
        or state.phase == PairedBinaryPhase.FAILED
        and state.unwind_attempt_count > 0
        and (allocated_yes > 0 or allocated_no > 0)
    )
    flatness_verified, flatness_source = _flatness_verified(
        allocated_yes=allocated_yes,
        allocated_no=allocated_no,
        wallet_yes=wallet_yes,
        wallet_no=wallet_no,
    )
    open_exposure = allocated_yes > 0 or allocated_no > 0
    terminal_reason = _infer_terminal_reason(state, fact_rows)
    unwind_status = _unwind_status(
        rows=fact_rows,
        allocated_yes=allocated_yes,
        allocated_no=allocated_no,
        manual_intervention=manual_intervention,
    )
    pnl_unavailable_reason = None
    pnl_status = None
    if state.phase == PairedBinaryPhase.FAILED:
        if activation_unwind_failure_pattern(fact_rows) or emergency_attempted:
            pnl_unavailable_reason = "activation_unwind_failed_or_incomplete"
            pnl_status = "unavailable"
        elif open_exposure or not flatness_verified:
            pnl_unavailable_reason = "activation_unwind_failed_or_incomplete"
            pnl_status = "unavailable"

    return {
        "run_id": str(run_id),
        "market_id": cfg.market_id,
        "condition_id": cfg.condition_id,
        "event_end_ts": cfg.event_end_ts,
        "final_state": state.phase.value,
        "terminal_reason": terminal_reason,
        "phase": state.phase.value,
        "entry_committed": _entry_committed(fact_rows),
        "both_legs_filled": _both_legs_filled(fact_rows),
        "activation_succeeded": _activation_succeeded(fact_rows),
        "survivor_phase_reached": survivor_phase_reached(fact_rows),
        "emergency_unwind_attempted": emergency_attempted,
        "emergency_unwind_done": emergency_done,
        "unwind_attempt_count": state.unwind_attempt_count,
        "open_exposure_after_unwind": open_exposure,
        "wallet_qty_yes": str(wallet_yes) if wallet_yes is not None else None,
        "wallet_qty_no": str(wallet_no) if wallet_no is not None else None,
        "allocated_qty_yes": str(allocated_yes),
        "allocated_qty_no": str(allocated_no),
        "unwind_status": unwind_status,
        "flatness_verified": flatness_verified,
        "flatness_source": flatness_source,
        "manual_intervention_required": manual_intervention or open_exposure,
        "pnl_status": pnl_status or ("unavailable" if state.phase == PairedBinaryPhase.FAILED else None),
        "pnl_unavailable_reason": pnl_unavailable_reason,
    }


def finalize_terminal_reporting(
    *,
    sink,
    run_id: RunId,
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    app: AppConfig,
    coord,
    fact_rows: list[dict[str, Any]],
    yes_book: LegBook | None = None,
    no_book: LegBook | None = None,
) -> None:
    """Emit terminal summary + PnL-unavailable facts for FAILED terminal runs."""
    from tyrex_pm.strategies.paired_binary import facts as pb_facts

    if not state.is_terminal():
        return
    types = _fact_types(fact_rows)
    if "paired_binary_terminal_summary" in types:
        return

    summary = build_terminal_summary_payload(
        run_id=run_id,
        state=state,
        cfg=cfg,
        app=app,
        coord=coord,
        fact_rows=fact_rows,
    )
    yes = yes_book or LegBook(token_id=TokenId(cfg.yes_token_id), bid=None, ask=None, stale=False)
    no = no_book or LegBook(token_id=TokenId(cfg.no_token_id), bid=None, ask=None, stale=False)

    if state.phase == PairedBinaryPhase.FAILED:
        pb_facts.emit_terminal_summary(sink, run_id, state, yes, no, summary=summary)
        if "paired_binary_realized_pnl_unavailable" not in types and "paired_binary_realized_pnl" not in types:
            pb_facts.emit_failed_unwind_pnl_unavailable(
                sink,
                run_id,
                state,
                yes,
                no,
                summary=summary,
            )
