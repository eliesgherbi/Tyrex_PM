"""Live entry quantity reconciliation for paired binary (Phase 4.6 fix).

Reconciles entry completion from fill/position evidence only — never from resting
submit ack or ledger credits that lack fill proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.runtime.allocation_runtime import maybe_repair_allocation_from_evidence
from tyrex_pm.runtime.config import AppConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.state import fill_state
from tyrex_pm.state.entry_fill_lifecycle import EntryFillStatus, resolve_leg_fill_snapshot


@dataclass(frozen=True)
class LegEntryQtyReconcile:
    leg: str
    token_id: TokenId
    confirmed_qty: Decimal
    ledger_qty: Decimal
    venue_qty: Decimal
    effective_qty: Decimal
    source: str
    repaired: bool
    finality_final: bool
    fill_status: str
    leg_correlation_id: str | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class PairEntryQtyReconcile:
    yes: LegEntryQtyReconcile
    no: LegEntryQtyReconcile
    pair_correlation_id: str
    effective_pair_qty: Decimal


_FILL_ACTIVE_STATUSES = frozenset(
    {
        EntryFillStatus.PARTIALLY_FILLED,
        EntryFillStatus.MATCHED,
        EntryFillStatus.CONFIRMED,
    }
)


def find_allocation_final_buy_qty(
    coord: RuntimeCoordinator, token_id: TokenId
) -> tuple[Decimal, bool, str, dict[str, Any]]:
    """Return (qty, is_final, source, evidence) from user-WS trade ledger."""
    wallet = coord.wallet
    for rec in reversed(wallet.trade_fill_records):
        if rec.token_id != token_id or rec.side != Side.BUY:
            continue
        status = str(rec.status).upper()
        evidence = {
            "trade_status": status,
            "trade_size": str(rec.size),
            "trade_price": str(rec.price),
            "trade_ts": rec.ts_utc.isoformat(),
        }
        if fill_state.is_allocation_final(status):
            return rec.size, True, rec.source or "user_ws", evidence
    for rec in reversed(wallet.trade_fill_records):
        if rec.token_id != token_id or rec.side != Side.BUY:
            continue
        status = str(rec.status).upper()
        if fill_state.is_execution_evidence(status):
            return (
                rec.size,
                False,
                rec.source or "user_ws",
                {
                    "trade_status": status,
                    "trade_size": str(rec.size),
                    "trade_price": str(rec.price),
                    "trade_ts": rec.ts_utc.isoformat(),
                },
            )
    return Decimal("0"), False, "none", {}


def venue_position_qty(coord: RuntimeCoordinator, token_id: TokenId) -> Decimal:
    pos = coord.wallet.positions.get(token_id)
    if pos is None:
        return Decimal("0")
    return max(Decimal("0"), pos.qty)


def reconcile_leg_entry_qty(
    coord: RuntimeCoordinator,
    app: AppConfig,
    *,
    owner_id: str,
    token_id: TokenId,
    leg: str,
    pair_correlation_id: str,
    leg_correlation_id: str | None = None,
    client_order_id: str | None = None,
    run_id: RunId | None = None,
    repair: bool = True,
) -> LegEntryQtyReconcile:
    ledger = coord.allocation_ledger
    snap = resolve_leg_fill_snapshot(
        coord,
        token_id=token_id,
        owner_id=owner_id,
        client_order_id=client_order_id,
        include_venue_position=False,
    )
    confirmed_qty, finality_final, conf_source, _ = find_allocation_final_buy_qty(coord, token_id)
    ledger_qty = Decimal("0")
    if ledger is not None:
        ledger_qty = ledger.get_available_allocated(owner_id, token_id)
    venue_qty = venue_position_qty(coord, token_id)
    repaired = False
    effective = Decimal("0")
    source = "none"

    if confirmed_qty > 0 and finality_final:
        effective = confirmed_qty
        source = conf_source
        if repair and ledger is not None and ledger_qty < confirmed_qty:
            mut = maybe_repair_allocation_from_evidence(
                coord,
                app,
                owner_id=owner_id,
                token_id=token_id,
                target_qty=confirmed_qty,
                source=conf_source,
                correlation_id=leg_correlation_id or f"{pair_correlation_id}:{leg}:finality",
                run_id=str(run_id) if run_id is not None else "",
                reason="finality_confirmed",
            )
            if mut is not None:
                repaired = True
                ledger_qty = ledger.get_available_allocated(owner_id, token_id)
    elif snap.filled_qty > 0 and snap.status in _FILL_ACTIVE_STATUSES:
        effective = snap.filled_qty
        source = snap.source
        if repair and ledger is not None and ledger_qty < effective:
            mut = maybe_repair_allocation_from_evidence(
                coord,
                app,
                owner_id=owner_id,
                token_id=token_id,
                target_qty=effective,
                source=snap.source,
                correlation_id=leg_correlation_id or f"{pair_correlation_id}:{leg}:fill",
                run_id=str(run_id) if run_id is not None else "",
                reason="fill_evidence",
            )
            if mut is not None:
                repaired = True
                ledger_qty = ledger.get_available_allocated(owner_id, token_id)
    elif venue_qty > 0 and ledger_qty > 0:
        # Shadow instant fill or aligned repair: owned in both wallet and ledger.
        effective = min(ledger_qty, venue_qty)
        source = "venue_and_ledger_aligned"
        if repair and ledger is not None and ledger_qty < effective:
            mut = maybe_repair_allocation_from_evidence(
                coord,
                app,
                owner_id=owner_id,
                token_id=token_id,
                target_qty=effective,
                source="wallet_position",
                correlation_id=leg_correlation_id or f"{pair_correlation_id}:{leg}:venue",
                run_id=str(run_id) if run_id is not None else "",
                reason="venue_position_fallback",
            )
            if mut is not None:
                repaired = True
                ledger_qty = ledger.get_available_allocated(owner_id, token_id)
    elif venue_qty > 0 and snap.status in _FILL_ACTIVE_STATUSES:
        effective = min(snap.filled_qty, venue_qty) if snap.filled_qty > 0 else venue_qty
        source = "venue_position"
        if repair and ledger is not None and ledger_qty < effective:
            mut = maybe_repair_allocation_from_evidence(
                coord,
                app,
                owner_id=owner_id,
                token_id=token_id,
                target_qty=effective,
                source="wallet_position",
                correlation_id=leg_correlation_id or f"{pair_correlation_id}:{leg}:venue",
                run_id=str(run_id) if run_id is not None else "",
                reason="venue_position_fallback",
            )
            if mut is not None:
                repaired = True
                ledger_qty = ledger.get_available_allocated(owner_id, token_id)

    return LegEntryQtyReconcile(
        leg=leg,
        token_id=token_id,
        confirmed_qty=confirmed_qty if finality_final else Decimal("0"),
        ledger_qty=ledger_qty,
        venue_qty=venue_qty,
        effective_qty=effective,
        source=source,
        repaired=repaired,
        finality_final=finality_final,
        fill_status=snap.status.value,
        leg_correlation_id=leg_correlation_id,
        client_order_id=client_order_id,
    )


def reconcile_pair_entry_qty(
    coord: RuntimeCoordinator,
    app: AppConfig,
    *,
    owner_id: str,
    yes_token_id: str,
    no_token_id: str,
    pair_correlation_id: str,
    yes_leg_correlation_id: str | None = None,
    no_leg_correlation_id: str | None = None,
    yes_client_order_id: str | None = None,
    no_client_order_id: str | None = None,
    run_id: RunId | None = None,
    repair: bool = True,
) -> PairEntryQtyReconcile:
    yes = reconcile_leg_entry_qty(
        coord,
        app,
        owner_id=owner_id,
        token_id=TokenId(yes_token_id),
        leg="yes",
        pair_correlation_id=pair_correlation_id,
        leg_correlation_id=yes_leg_correlation_id,
        client_order_id=yes_client_order_id,
        run_id=run_id,
        repair=repair,
    )
    no = reconcile_leg_entry_qty(
        coord,
        app,
        owner_id=owner_id,
        token_id=TokenId(no_token_id),
        leg="no",
        pair_correlation_id=pair_correlation_id,
        leg_correlation_id=no_leg_correlation_id,
        client_order_id=no_client_order_id,
        run_id=run_id,
        repair=repair,
    )
    return PairEntryQtyReconcile(
        yes=yes,
        no=no,
        pair_correlation_id=pair_correlation_id,
        effective_pair_qty=min(yes.effective_qty, no.effective_qty),
    )
