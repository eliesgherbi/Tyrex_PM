"""One-shot R7 approval artifact — bound to market, caps, code identity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


class ApprovalError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class ApprovalArtifact:
    artifact_id: str
    created_at: str
    expires_at: str
    commit_identity: str
    config_fingerprint: str
    r7a_report_hash: str
    market_id: str
    condition_id: str
    instrument_token_id: str
    outcome_side: str
    side: str  # BUY
    max_buy_notional: str
    limit_price: str
    quantity: str
    order_type: str  # FAK | FOK | GTC
    tick_size: str
    min_order_size: str
    max_limit_price: str
    entry_deadline: str
    flatten_deadline: str
    max_hold_s: float
    flatten_before_close_s: float
    risk_fingerprint: str
    user_stream_ready: bool
    reconciliation_clean: bool
    consumed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "commit_identity": self.commit_identity,
            "config_fingerprint": self.config_fingerprint,
            "r7a_report_hash": self.r7a_report_hash,
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "instrument_token_id": self.instrument_token_id,
            "outcome_side": self.outcome_side,
            "side": self.side,
            "max_buy_notional": self.max_buy_notional,
            "limit_price": self.limit_price,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "max_limit_price": self.max_limit_price,
            "entry_deadline": self.entry_deadline,
            "flatten_deadline": self.flatten_deadline,
            "max_hold_s": self.max_hold_s,
            "flatten_before_close_s": self.flatten_before_close_s,
            "risk_fingerprint": self.risk_fingerprint,
            "user_stream_ready": self.user_stream_ready,
            "reconciliation_clean": self.reconciliation_clean,
            "consumed": self.consumed,
        }

    def content_hash(self) -> str:
        payload = {k: v for k, v in self.to_dict().items() if k != "consumed"}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ApprovalArtifact:
        return cls(
            artifact_id=str(data["artifact_id"]),
            created_at=str(data["created_at"]),
            expires_at=str(data["expires_at"]),
            commit_identity=str(data["commit_identity"]),
            config_fingerprint=str(data["config_fingerprint"]),
            r7a_report_hash=str(data["r7a_report_hash"]),
            market_id=str(data["market_id"]),
            condition_id=str(data["condition_id"]),
            instrument_token_id=str(data["instrument_token_id"]),
            outcome_side=str(data["outcome_side"]),
            side=str(data["side"]),
            max_buy_notional=str(data["max_buy_notional"]),
            limit_price=str(data["limit_price"]),
            quantity=str(data["quantity"]),
            order_type=str(data["order_type"]),
            tick_size=str(data["tick_size"]),
            min_order_size=str(data["min_order_size"]),
            max_limit_price=str(data["max_limit_price"]),
            entry_deadline=str(data["entry_deadline"]),
            flatten_deadline=str(data["flatten_deadline"]),
            max_hold_s=float(data["max_hold_s"]),
            flatten_before_close_s=float(data["flatten_before_close_s"]),
            risk_fingerprint=str(data["risk_fingerprint"]),
            user_stream_ready=bool(data["user_stream_ready"]),
            reconciliation_clean=bool(data["reconciliation_clean"]),
            consumed=bool(data.get("consumed", False)),
        )


def build_approval_artifact(
    *,
    commit_identity: str,
    config_fingerprint: str,
    r7a_report_hash: str,
    market_id: str,
    condition_id: str,
    instrument_token_id: str,
    outcome_side: str,
    max_buy_notional: Decimal,
    limit_price: Decimal,
    quantity: Decimal,
    order_type: str,
    tick_size: str,
    min_order_size: str,
    max_limit_price: Decimal,
    entry_deadline: datetime,
    flatten_deadline: datetime,
    max_hold_s: float,
    flatten_before_close_s: float,
    risk_fingerprint: str,
    user_stream_ready: bool,
    reconciliation_clean: bool,
    ttl: timedelta = timedelta(hours=1),
) -> ApprovalArtifact:
    if max_buy_notional > Decimal("5.00"):
        raise ApprovalError("max_buy_notional exceeds $5 envelope")
    if limit_price * quantity > max_buy_notional + Decimal("0.0000001"):
        raise ApprovalError("price*qty exceeds max_buy_notional")
    if order_type not in {"FAK", "FOK", "GTC", "GTD"}:
        raise ApprovalError(f"unsupported order_type: {order_type}")
    now = _utc_now()
    entry_dl = entry_deadline.astimezone(timezone.utc)
    expires = min(now + ttl, entry_dl)
    return ApprovalArtifact(
        artifact_id=str(uuid4()),
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
        commit_identity=commit_identity,
        config_fingerprint=config_fingerprint,
        r7a_report_hash=r7a_report_hash,
        market_id=market_id,
        condition_id=condition_id,
        instrument_token_id=instrument_token_id,
        outcome_side=outcome_side,
        side="BUY",
        max_buy_notional=str(max_buy_notional),
        limit_price=str(limit_price),
        quantity=str(quantity),
        order_type=order_type,
        tick_size=tick_size,
        min_order_size=min_order_size,
        max_limit_price=str(max_limit_price),
        entry_deadline=entry_deadline.astimezone(timezone.utc).isoformat(),
        flatten_deadline=flatten_deadline.astimezone(timezone.utc).isoformat(),
        max_hold_s=max_hold_s,
        flatten_before_close_s=flatten_before_close_s,
        risk_fingerprint=risk_fingerprint,
        user_stream_ready=user_stream_ready,
        reconciliation_clean=reconciliation_clean,
        consumed=False,
    )


def validate_approval(
    artifact: ApprovalArtifact,
    *,
    commit_identity: str,
    config_fingerprint: str,
    market_id: str,
    instrument_token_id: str,
    quantity: Decimal,
    limit_price: Decimal,
    max_buy_notional: Decimal,
    user_stream_ready: bool,
    reconciliation_clean: bool,
    now: datetime | None = None,
) -> None:
    """Raise ApprovalError if artifact is not valid for the proposed action."""
    when = now or _utc_now()
    if artifact.consumed:
        raise ApprovalError("ARTIFACT_CONSUMED")
    if _parse_ts(artifact.expires_at) <= when:
        raise ApprovalError("ARTIFACT_EXPIRED")
    if _parse_ts(artifact.entry_deadline) <= when:
        raise ApprovalError("ENTRY_DEADLINE_PASSED")
    if artifact.commit_identity != commit_identity:
        raise ApprovalError("CODE_IDENTITY_MISMATCH")
    if artifact.config_fingerprint != config_fingerprint:
        raise ApprovalError("CONFIG_FINGERPRINT_MISMATCH")
    if artifact.market_id != market_id:
        raise ApprovalError("MARKET_MISMATCH")
    if artifact.instrument_token_id != instrument_token_id:
        raise ApprovalError("TOKEN_MISMATCH")
    if Decimal(artifact.quantity) != quantity:
        raise ApprovalError("QUANTITY_MISMATCH")
    if Decimal(artifact.limit_price) != limit_price:
        raise ApprovalError("PRICE_MISMATCH")
    if Decimal(artifact.max_buy_notional) != max_buy_notional:
        raise ApprovalError("NOTIONAL_MISMATCH")
    if Decimal(artifact.max_buy_notional) > Decimal("5.00"):
        raise ApprovalError("NOTIONAL_EXCEEDS_ENVELOPE")
    if not artifact.user_stream_ready or not user_stream_ready:
        raise ApprovalError("USER_STREAM_NOT_READY")
    if not artifact.reconciliation_clean or not reconciliation_clean:
        raise ApprovalError("RECONCILIATION_NOT_CLEAN")


def write_approval(path: Path, artifact: ApprovalArtifact) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifact.to_dict(), indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return artifact.content_hash()


def read_approval(path: Path) -> ApprovalArtifact:
    return ApprovalArtifact.from_dict(json.loads(path.read_text(encoding="utf-8")))


def mark_consumed(path: Path) -> ApprovalArtifact:
    art = read_approval(path)
    data = art.to_dict()
    data["consumed"] = True
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return ApprovalArtifact.from_dict(data)
