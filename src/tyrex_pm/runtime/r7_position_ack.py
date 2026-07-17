"""Typed acknowledgment of exact R7A.1 resolved positions (reporting/risk only)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from tyrex_pm.runtime.r7_position_inventory import (
    InventoryReport,
    InventoryRow,
    PositionCategory,
)

SCHEMA_VERSION = "r7_position_ack_v1"
POLICY_ID = "ACK_RESOLVED_REDEEMABLE_UNTOUCHED_R7A1"

ACKNOWLEDGMENT_TEXT = (
    "I acknowledge the four resolved positions and authorize them to remain "
    "untouched during R7. I do not authorize redemption or any other action on "
    "them. They may be excluded from selected-market flatness, but must remain "
    "visible in account-wide reconciliation."
)


class AckError(RuntimeError):
    pass


@dataclass(frozen=True)
class PositionFingerprint:
    condition_id: str
    token_id: str
    outcome: str
    quantity: str
    resolved: bool
    redeemable: bool
    category: str
    slug: str | None = None

    def digest(self) -> str:
        payload = {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "outcome": self.outcome,
            "quantity": str(Decimal(self.quantity)),
            "resolved": self.resolved,
            "redeemable": self.redeemable,
            "category": self.category,
            "slug": self.slug,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "outcome": self.outcome,
            "quantity": self.quantity,
            "resolved": self.resolved,
            "redeemable": self.redeemable,
            "category": self.category,
            "slug": self.slug,
            "fingerprint": self.digest(),
            # Redacted view helpers (full IDs kept for exact matching)
            "condition_id_prefix": self.condition_id[:18] + "…" if self.condition_id else None,
            "token_id_prefix": self.token_id[:18] + "…" if self.token_id else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PositionFingerprint:
        return cls(
            condition_id=str(data["condition_id"]),
            token_id=str(data["token_id"]),
            outcome=str(data["outcome"]),
            quantity=str(data["quantity"]),
            resolved=bool(data["resolved"]),
            redeemable=bool(data["redeemable"]),
            category=str(data["category"]),
            slug=None if data.get("slug") is None else str(data["slug"]),
        )


@dataclass
class PositionAcknowledgment:
    schema_version: str
    acknowledgment_id: str
    created_at: str
    policy_id: str
    acknowledgment_text_hash: str
    expected_position_count: int
    required_category: str
    positions: list[PositionFingerprint]
    permissions: dict[str, bool]
    prohibitions: dict[str, bool]
    commit_identity: str
    consumed: bool = False

    def content_hash(self) -> str:
        payload = self.to_dict()
        payload.pop("consumed", None)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def set_fingerprint(self) -> str:
        return hashlib.sha256(
            "".join(sorted(p.digest() for p in self.positions)).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "acknowledgment_id": self.acknowledgment_id,
            "created_at": self.created_at,
            "policy_id": self.policy_id,
            "acknowledgment_text_hash": self.acknowledgment_text_hash,
            "acknowledgment_text": ACKNOWLEDGMENT_TEXT,
            "expected_position_count": self.expected_position_count,
            "required_category": self.required_category,
            "position_set_fingerprint": self.set_fingerprint(),
            "positions": [p.to_dict() for p in self.positions],
            "permissions": dict(self.permissions),
            "prohibitions": dict(self.prohibitions),
            "commit_identity": self.commit_identity,
            "consumed": self.consumed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PositionAcknowledgment:
        return cls(
            schema_version=str(data["schema_version"]),
            acknowledgment_id=str(data["acknowledgment_id"]),
            created_at=str(data["created_at"]),
            policy_id=str(data["policy_id"]),
            acknowledgment_text_hash=str(data["acknowledgment_text_hash"]),
            expected_position_count=int(data["expected_position_count"]),
            required_category=str(data["required_category"]),
            positions=[PositionFingerprint.from_dict(p) for p in data["positions"]],
            permissions=dict(data["permissions"]),
            prohibitions=dict(data["prohibitions"]),
            commit_identity=str(data["commit_identity"]),
            consumed=bool(data.get("consumed", False)),
        )


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_condition_id(row: Mapping[str, Any]) -> str:
    return str(row.get("conditionId") or row.get("condition_id") or "")


def _row_token_id(row: Mapping[str, Any]) -> str:
    return str(row.get("asset") or row.get("token_id") or "")


def identity_key(condition_id: str, token_id: str) -> str:
    """Stable authoritative identity — not order-dependent."""
    return f"{condition_id.lower()}|{token_id}"


def row_ack_fields_complete(row: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Fail-closed completeness for acknowledgment matching."""
    missing: list[str] = []
    if not _row_condition_id(row):
        missing.append("condition_id")
    if not _row_token_id(row):
        missing.append("token_id")
    if "size" not in row:
        missing.append("size")
    if "redeemable" not in row:
        missing.append("redeemable")
    return (len(missing) == 0), missing


def fingerprint_from_raw(row: Mapping[str, Any]) -> PositionFingerprint:
    size = Decimal(str(row.get("size") or "0"))
    redeemable = bool(row.get("redeemable"))
    cur = row.get("curPrice")
    resolved = redeemable or (cur is not None and Decimal(str(cur)) == 0)
    return PositionFingerprint(
        condition_id=_row_condition_id(row),
        token_id=_row_token_id(row),
        outcome=str(row.get("outcome") or ""),
        quantity=str(size),
        resolved=resolved,
        redeemable=redeemable,
        category=PositionCategory.RESOLVED_REDEEMABLE_POSITION.value,
        slug=(
            None
            if row.get("slug") is None and row.get("eventSlug") is None
            else str(row.get("slug") or row.get("eventSlug"))
        ),
    )


def build_acknowledgment(
    *,
    raw_positions: Sequence[Mapping[str, Any]],
    commit_identity: str,
    acknowledgment_text: str = ACKNOWLEDGMENT_TEXT,
) -> PositionAcknowledgment:
    """Build ack for exactly four RESOLVED_REDEEMABLE rows from raw Data API rows."""
    if acknowledgment_text != ACKNOWLEDGMENT_TEXT:
        raise AckError("ACKNOWLEDGMENT_TEXT_MISMATCH")
    fps: list[PositionFingerprint] = []
    for row in raw_positions:
        if Decimal(str(row.get("size") or "0")) == 0:
            continue
        fp = fingerprint_from_raw(row)
        if not fp.resolved or not fp.redeemable:
            raise AckError("POSITION_NOT_RESOLVED_REDEEMABLE")
        if not fp.condition_id or not fp.token_id:
            raise AckError("POSITION_IDENTITY_INCOMPLETE")
        fps.append(fp)
    if len(fps) != 4:
        raise AckError(f"EXPECTED_FOUR_POSITIONS_GOT_{len(fps)}")
    return PositionAcknowledgment(
        schema_version=SCHEMA_VERSION,
        acknowledgment_id=str(uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        policy_id=POLICY_ID,
        acknowledgment_text_hash=_text_hash(acknowledgment_text),
        expected_position_count=4,
        required_category=PositionCategory.RESOLVED_REDEEMABLE_POSITION.value,
        positions=fps,
        permissions={
            "selected_market_flatness_exclusion": True,
            "account_wide_visibility_required": True,
        },
        prohibitions={
            "sell": True,
            "redeem": True,
            "merge_split": True,
            "transfer": True,
            "approve": True,
            "any_on_chain_action": True,
        },
        commit_identity=commit_identity,
        consumed=False,
    )


def write_acknowledgment(path: Path, ack: PositionAcknowledgment) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ack.to_dict(), indent=2) + "\n", encoding="utf-8")
    return ack.content_hash()


def read_acknowledgment(path: Path) -> PositionAcknowledgment:
    return PositionAcknowledgment.from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


@dataclass
class AckValidationResult:
    ok: bool
    blockers: list[str] = field(default_factory=list)
    matched: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blockers": list(self.blockers),
            "matched": self.matched,
            "notes": list(self.notes),
        }


def validate_acknowledgment_against_inventory(
    ack: PositionAcknowledgment,
    *,
    raw_positions: Sequence[Mapping[str, Any]],
    selected_token_ids: Sequence[str] = (),
    selected_condition_id: str | None = None,
    open_order_count: int = 0,
    secondary_raw_positions: Sequence[Mapping[str, Any]] | None = None,
    ignore_selected_market_tokens: Sequence[str] = (),
) -> AckValidationResult:
    """Validate that live inventory still matches the acknowledged set exactly.

    Fail closed on incomplete rows — never silently drop or reinterpret acks.
    Matching is identity-stable (condition_id + token_id); row order irrelevant.
    """
    result = AckValidationResult(ok=True)
    if ack.acknowledgment_text_hash != _text_hash(ACKNOWLEDGMENT_TEXT):
        result.ok = False
        result.blockers.append("ACKNOWLEDGMENT_TEXT_MISMATCH")
    if ack.expected_position_count != 4 or len(ack.positions) != 4:
        result.ok = False
        result.blockers.append("ACKNOWLEDGED_POSITION_SET_CHANGED")

    # Completeness gate — incomplete inventory cannot validate entry
    complete_rows: list[Mapping[str, Any]] = []
    for r in raw_positions:
        if Decimal(str(r.get("size") or "0")) == 0:
            continue
        ok_row, missing = row_ack_fields_complete(r)
        if not ok_row:
            result.ok = False
            result.blockers.append("ACK_INVENTORY_ROW_INCOMPLETE")
            result.notes.append(f"incomplete_fields={missing}")
            continue
        complete_rows.append(r)

    if "ACK_INVENTORY_ROW_INCOMPLETE" in result.blockers:
        result.blockers = list(dict.fromkeys(result.blockers))
        return result

    ignore_sel = {str(t) for t in ignore_selected_market_tokens}
    selected = set(selected_token_ids)
    ack_by_id = {identity_key(p.condition_id, p.token_id): p for p in ack.positions}

    live_fps: list[PositionFingerprint] = []
    seen_identity: set[str] = set()
    for r in complete_rows:
        fp = fingerprint_from_raw(r)
        ik = identity_key(fp.condition_id, fp.token_id)
        # Exclude lifecycle dust only when it is NOT an acknowledged identity
        if fp.token_id in ignore_sel and ik not in ack_by_id:
            result.notes.append("selected_market_lifecycle_dust_excluded_from_ack_set")
            continue
        if ik in seen_identity:
            result.ok = False
            result.blockers.append("ACK_DUPLICATE_IDENTITY")
            continue
        seen_identity.add(ik)
        live_fps.append(fp)

    live_by_id = {identity_key(p.condition_id, p.token_id): p for p in live_fps}

    if set(ack_by_id) != set(live_by_id):
        result.ok = False
        missing = set(ack_by_id) - set(live_by_id)
        extra = set(live_by_id) - set(ack_by_id)
        if missing:
            result.blockers.append("ACKNOWLEDGED_POSITION_SET_CHANGED")
            result.notes.append("never_drop_acknowledged_position")
        if extra:
            result.blockers.append("UNACKNOWLEDGED_POSITION_PRESENT")

    for ik, ack_fp in ack_by_id.items():
        live_fp = live_by_id.get(ik)
        if live_fp is None:
            continue
        if Decimal(live_fp.quantity) != Decimal(ack_fp.quantity):
            result.ok = False
            result.blockers.append("ACKNOWLEDGED_POSITION_SET_CHANGED")
        if live_fp.digest() != ack_fp.digest():
            # outcome/slug/category drift
            if not live_fp.redeemable or not live_fp.resolved:
                result.ok = False
                result.blockers.append("ACKNOWLEDGED_POSITION_BECAME_TRADABLE")
            elif live_fp.digest() != ack_fp.digest():
                result.ok = False
                result.blockers.append("ACKNOWLEDGED_POSITION_SET_CHANGED")
        if live_fp.token_id in selected or (
            selected_condition_id and live_fp.condition_id == selected_condition_id
        ):
            result.ok = False
            result.blockers.append("SELECTED_MARKET_POSITION_NONZERO")
        result.matched += 1

    for ik, live_fp in live_by_id.items():
        if ik not in ack_by_id:
            result.ok = False
            result.blockers.append("UNACKNOWLEDGED_POSITION_PRESENT")

    if secondary_raw_positions is not None:
        sec = validate_acknowledgment_against_inventory(
            ack,
            raw_positions=secondary_raw_positions,
            selected_token_ids=selected_token_ids,
            selected_condition_id=selected_condition_id,
            open_order_count=0,
            secondary_raw_positions=None,
            ignore_selected_market_tokens=ignore_selected_market_tokens,
        )
        if sec.ok != result.ok or set(sec.blockers) != set(result.blockers):
            # Disagreement between sources — fail closed for entry
            result.ok = False
            result.blockers.append("ACK_SOURCE_DISAGREEMENT")
            result.notes.append("primary_vs_secondary_inventory_disagree")

    if open_order_count > 0:
        result.ok = False
        result.blockers.append("UNKNOWN_EXTERNAL_ORDER")

    result.blockers = list(dict.fromkeys(result.blockers))
    if result.ok and result.matched != 4:
        result.ok = False
        result.blockers.append("ACKNOWLEDGED_POSITION_SET_CHANGED")
    if result.ok:
        result.notes.append("four_acknowledged_resolved_positions_match")
    return result


def ack_targets_forbidden(token_id: str, ack: PositionAcknowledgment) -> bool:
    """True if token is acknowledged — must not be an execution target."""
    return any(p.token_id == token_id for p in ack.positions)


def inventory_rows_marked_acknowledged(
    inventory: InventoryReport,
    ack: PositionAcknowledgment,
) -> list[dict[str, Any]]:
    """Account-wide visibility: mark acknowledged rows in report form."""
    digests = {p.digest() for p in ack.positions}
    # Inventory rows lack full IDs; report uses ack positions for visibility
    out = []
    for p in ack.positions:
        out.append(
            {
                "acknowledged": True,
                "fingerprint": p.digest(),
                "category": p.category,
                "outcome": p.outcome,
                "quantity": p.quantity,
                "slug": p.slug,
                "condition_id_prefix": p.condition_id[:18] + "…",
                "token_id_prefix": p.token_id[:18] + "…",
                "excluded_from_selected_market_flatness": True,
                "cleanup_target": False,
                "sell_forbidden": True,
                "redeem_forbidden": True,
            }
        )
    _ = digests
    _ = inventory
    return out
