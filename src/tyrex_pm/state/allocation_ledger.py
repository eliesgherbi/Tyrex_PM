"""Per-strategy token allocation ledger (P4).

Tracks strategy-owned quantity attribution separate from venue ``WalletStore.positions``.
RiskEngine inventory checks remain authoritative; this ledger is for sizing and observability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import WalletPosition

_ALLOCATION_LEDGER_JSON_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec_str(val: Decimal) -> str:
    return format(val, "f")


def _parse_dec(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _entry_key(owner_id: str, token_id: str) -> str:
    return f"{owner_id}|{token_id}"


@dataclass
class AllocationEntry:
    owner_id: str
    token_id: str
    allocated_qty: Decimal = Decimal("0")
    reserved_exit_qty: Decimal = Decimal("0")
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitReservation:
    reservation_id: str
    owner_id: str
    token_id: str
    qty: Decimal
    original_qty: Decimal
    venue_order_id: str | None = None
    applied_fill_qty: Decimal = Decimal("0")
    applied_dedup_keys: list[str] = field(default_factory=list)


@dataclass
class AllocationClampResult:
    owner_id: str
    token_id: str
    allocated_before: Decimal
    allocated_after: Decimal
    venue_qty: Decimal


@dataclass
class AllocationMutation:
    event: str
    owner_id: str
    token_id: str
    delta_qty: Decimal
    allocated_before: Decimal
    allocated_after: Decimal
    correlation_id: str | None = None
    venue_qty: Decimal | None = None
    reservation_id: str | None = None
    source: str | None = None
    reason: str | None = None
    filled_qty: Decimal | None = None
    reserved_before: Decimal | None = None
    reserved_after: Decimal | None = None
    venue_order_id: str | None = None
    partial: bool = False


class AllocationLedger:
    """In-memory allocation ledger with JSON persistence (required on every run)."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._entries: dict[str, AllocationEntry] = {}
        self._reservations: dict[str, ExitReservation] = {}

    @property
    def path(self) -> Path | None:
        return self._path

    def get_allocated(self, owner_id: str, token_id: str | TokenId) -> Decimal:
        key = _entry_key(owner_id, str(token_id))
        entry = self._entries.get(key)
        return entry.allocated_qty if entry is not None else Decimal("0")

    def get_reserved(self, owner_id: str, token_id: str | TokenId) -> Decimal:
        key = _entry_key(owner_id, str(token_id))
        entry = self._entries.get(key)
        return entry.reserved_exit_qty if entry is not None else Decimal("0")

    def get_available_allocated(self, owner_id: str, token_id: str | TokenId) -> Decimal:
        allocated = self.get_allocated(owner_id, token_id)
        reserved = self.get_reserved(owner_id, token_id)
        avail = allocated - reserved
        return avail if avail > 0 else Decimal("0")

    def _touch_entry(self, owner_id: str, token_id: str) -> AllocationEntry:
        key = _entry_key(owner_id, token_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = AllocationEntry(owner_id=owner_id, token_id=token_id)
            self._entries[key] = entry
        entry.updated_at = _utc_now_iso()
        return entry

    def apply_buy(
        self,
        owner_id: str,
        token_id: str | TokenId,
        qty: Decimal,
        *,
        correlation_id: str | None = None,
    ) -> AllocationMutation:
        if qty <= 0:
            raise ValueError(f"apply_buy qty must be positive, got {qty!r}")
        tid = str(token_id)
        entry = self._touch_entry(owner_id, tid)
        before = entry.allocated_qty
        entry.allocated_qty = before + qty
        self._persist()
        return AllocationMutation(
            event="allocation_buy_applied",
            owner_id=owner_id,
            token_id=tid,
            delta_qty=qty,
            allocated_before=before,
            allocated_after=entry.allocated_qty,
            correlation_id=correlation_id,
        )

    def set_reservation_venue_order_id(self, reservation_id: str, venue_order_id: str) -> None:
        row = self._reservations.get(reservation_id)
        if row is None:
            return
        row.venue_order_id = str(venue_order_id)
        self._persist()

    def find_reservation_id_by_venue_order_id(self, venue_order_id: str) -> str | None:
        vid = str(venue_order_id)
        for rid, row in self._reservations.items():
            if row.venue_order_id == vid:
                return rid
        return None

    def apply_exit_fill(
        self,
        reservation_id: str,
        fill_qty: Decimal,
        *,
        source: str,
        dedup_key: str | None = None,
        correlation_id: str | None = None,
        venue_order_id: str | None = None,
    ) -> AllocationMutation | None:
        if fill_qty <= 0:
            return None
        row = self._reservations.get(reservation_id)
        if row is None:
            return None
        if dedup_key is not None and dedup_key in row.applied_dedup_keys:
            return None
        fill_qty = min(fill_qty, row.qty)
        if fill_qty <= 0:
            return None
        key = _entry_key(row.owner_id, row.token_id)
        entry = self._entries.get(key)
        if entry is None:
            return None
        allocated_before = entry.allocated_qty
        reserved_before = entry.reserved_exit_qty
        entry.allocated_qty = max(Decimal("0"), allocated_before - fill_qty)
        row.qty -= fill_qty
        row.applied_fill_qty += fill_qty
        entry.reserved_exit_qty = max(Decimal("0"), reserved_before - fill_qty)
        entry.updated_at = _utc_now_iso()
        if dedup_key is not None:
            row.applied_dedup_keys.append(dedup_key)
        partial = row.qty > 0
        if row.qty <= 0:
            self._reservations.pop(reservation_id, None)
        self._persist()
        return AllocationMutation(
            event="allocation_partial_fill_applied" if partial else "allocation_sell_applied",
            owner_id=row.owner_id,
            token_id=row.token_id,
            delta_qty=-fill_qty,
            allocated_before=allocated_before,
            allocated_after=entry.allocated_qty,
            correlation_id=correlation_id,
            reservation_id=reservation_id,
            source=source,
            filled_qty=fill_qty,
            reserved_before=reserved_before,
            reserved_after=entry.reserved_exit_qty,
            venue_order_id=venue_order_id or row.venue_order_id,
            partial=partial,
        )

    def apply_sell(
        self,
        owner_id: str,
        token_id: str | TokenId,
        qty: Decimal,
        *,
        correlation_id: str | None = None,
        reservation_id: str | None = None,
    ) -> AllocationMutation:
        if qty <= 0:
            raise ValueError(f"apply_sell qty must be positive, got {qty!r}")
        tid = str(token_id)
        entry = self._touch_entry(owner_id, tid)
        before = entry.allocated_qty
        entry.allocated_qty = max(Decimal("0"), before - qty)
        if reservation_id is not None:
            self.release_reservation(reservation_id, persist=False)
        self._persist()
        return AllocationMutation(
            event="allocation_sell_applied",
            owner_id=owner_id,
            token_id=tid,
            delta_qty=-qty,
            allocated_before=before,
            allocated_after=entry.allocated_qty,
            correlation_id=correlation_id,
            reservation_id=reservation_id,
        )

    def reserve_exit(
        self,
        owner_id: str,
        token_id: str | TokenId,
        qty: Decimal,
        reservation_id: str,
    ) -> AllocationMutation:
        if qty <= 0:
            raise ValueError(f"reserve_exit qty must be positive, got {qty!r}")
        if reservation_id in self._reservations:
            raise ValueError(f"reservation_id already exists: {reservation_id!r}")
        tid = str(token_id)
        entry = self._touch_entry(owner_id, tid)
        before = entry.reserved_exit_qty
        entry.reserved_exit_qty = before + qty
        self._reservations[reservation_id] = ExitReservation(
            reservation_id=reservation_id,
            owner_id=owner_id,
            token_id=tid,
            qty=qty,
            original_qty=qty,
        )
        self._persist()
        return AllocationMutation(
            event="allocation_reserved",
            owner_id=owner_id,
            token_id=tid,
            delta_qty=qty,
            allocated_before=entry.allocated_qty,
            allocated_after=entry.allocated_qty,
            reservation_id=reservation_id,
        )

    def release_reservation(
        self,
        reservation_id: str,
        *,
        persist: bool = True,
        reason: str | None = None,
        source: str | None = None,
    ) -> AllocationMutation | None:
        row = self._reservations.pop(reservation_id, None)
        if row is None:
            return None
        key = _entry_key(row.owner_id, row.token_id)
        entry = self._entries.get(key)
        if entry is None:
            if persist:
                self._persist()
            return AllocationMutation(
                event="allocation_released",
                owner_id=row.owner_id,
                token_id=row.token_id,
                delta_qty=-row.qty,
                allocated_before=Decimal("0"),
                allocated_after=Decimal("0"),
                reservation_id=reservation_id,
                reason=reason,
                source=source,
                reserved_before=row.qty,
                reserved_after=Decimal("0"),
                venue_order_id=row.venue_order_id,
            )
        before = entry.reserved_exit_qty
        entry.reserved_exit_qty = max(Decimal("0"), before - row.qty)
        entry.updated_at = _utc_now_iso()
        if persist:
            self._persist()
        return AllocationMutation(
            event="allocation_released",
            owner_id=row.owner_id,
            token_id=row.token_id,
            delta_qty=-row.qty,
            allocated_before=entry.allocated_qty,
            allocated_after=entry.allocated_qty,
            reservation_id=reservation_id,
            reason=reason,
            source=source,
            reserved_before=before,
            reserved_after=entry.reserved_exit_qty,
            venue_order_id=row.venue_order_id,
        )

    def clamp_to_venue_positions(
        self,
        wallet_positions: dict[TokenId, WalletPosition],
    ) -> list[AllocationClampResult]:
        venue_by_token: dict[str, Decimal] = {
            str(tid): max(Decimal("0"), pos.qty) for tid, pos in wallet_positions.items()
        }
        results: list[AllocationClampResult] = []
        changed = False
        for entry in list(self._entries.values()):
            venue_qty = venue_by_token.get(entry.token_id, Decimal("0"))
            if entry.allocated_qty <= venue_qty:
                continue
            before = entry.allocated_qty
            entry.allocated_qty = venue_qty
            if entry.reserved_exit_qty > entry.allocated_qty:
                entry.reserved_exit_qty = entry.allocated_qty
            entry.updated_at = _utc_now_iso()
            changed = True
            results.append(
                AllocationClampResult(
                    owner_id=entry.owner_id,
                    token_id=entry.token_id,
                    allocated_before=before,
                    allocated_after=entry.allocated_qty,
                    venue_qty=venue_qty,
                )
            )
        if changed:
            self._persist()
        return results

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": _ALLOCATION_LEDGER_JSON_VERSION,
            "entries": [
                {
                    "owner_id": e.owner_id,
                    "token_id": e.token_id,
                    "allocated_qty": _dec_str(e.allocated_qty),
                    "reserved_exit_qty": _dec_str(e.reserved_exit_qty),
                    "updated_at": e.updated_at,
                    "metadata": dict(e.metadata),
                }
                for e in sorted(
                    self._entries.values(),
                    key=lambda x: (x.owner_id, x.token_id),
                )
            ],
            "reservations": [
                {
                    "reservation_id": r.reservation_id,
                    "owner_id": r.owner_id,
                    "token_id": r.token_id,
                    "qty": _dec_str(r.qty),
                    "original_qty": _dec_str(r.original_qty),
                    "venue_order_id": r.venue_order_id,
                    "applied_fill_qty": _dec_str(r.applied_fill_qty),
                    "applied_dedup_keys": list(r.applied_dedup_keys),
                }
                for r in sorted(
                    self._reservations.values(),
                    key=lambda x: x.reservation_id,
                )
            ],
        }

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")


def load_allocation_ledger(path: Path) -> AllocationLedger:
    ledger = AllocationLedger(path=path)
    if not path.is_file():
        return ledger
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ledger
    if not isinstance(data, dict):
        return ledger
    entries_raw = data.get("entries")
    if isinstance(entries_raw, list):
        for row in entries_raw:
            if not isinstance(row, dict):
                continue
            owner_id = str(row.get("owner_id", "")).strip()
            token_id = str(row.get("token_id", "")).strip()
            if not owner_id or not token_id:
                continue
            key = _entry_key(owner_id, token_id)
            ledger._entries[key] = AllocationEntry(
                owner_id=owner_id,
                token_id=token_id,
                allocated_qty=_parse_dec(row.get("allocated_qty", "0")),
                reserved_exit_qty=_parse_dec(row.get("reserved_exit_qty", "0")),
                updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
                metadata=dict(row.get("metadata") or {}),
            )
    reservations_raw = data.get("reservations")
    if isinstance(reservations_raw, list):
        for row in reservations_raw:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("reservation_id", "")).strip()
            owner_id = str(row.get("owner_id", "")).strip()
            token_id = str(row.get("token_id", "")).strip()
            if not rid or not owner_id or not token_id:
                continue
            ledger._reservations[rid] = ExitReservation(
                reservation_id=rid,
                owner_id=owner_id,
                token_id=token_id,
                qty=_parse_dec(row.get("qty", "0")),
                original_qty=_parse_dec(row.get("original_qty", row.get("qty", "0"))),
                venue_order_id=str(row["venue_order_id"]) if row.get("venue_order_id") else None,
                applied_fill_qty=_parse_dec(row.get("applied_fill_qty", "0")),
                applied_dedup_keys=[
                    str(x) for x in (row.get("applied_dedup_keys") or []) if str(x).strip()
                ],
            )
    return ledger


def save_allocation_ledger(path: Path, ledger: AllocationLedger) -> None:
    ledger._path = path
    ledger._persist()
