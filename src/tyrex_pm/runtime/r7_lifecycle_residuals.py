"""Account-wide lifecycle residual registry (R7D.2).

Persistent multi-record state keyed by:
  ``condition_id | token_id | originating_run_id``

Cleanup policy remains NONE — no sell/redeem/merge/split/transfer/approve/on-chain.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.execution.polymarket.settlement import (
    DEFAULT_MIN_TRADABLE,
    FlatClassification,
    classify_flatness,
)
from tyrex_pm.runtime.r7_lifecycle_dust import (
    INCIDENT_DUST_CONDITION,
    INCIDENT_DUST_QTY,
    INCIDENT_DUST_TOKEN,
    default_incident_dust_record,
    read_lifecycle_dust,
)
from tyrex_pm.runtime.r7_paths import (
    DEFAULT_LIFECYCLE_DUST_PATH,
    DEFAULT_LIFECYCLE_RESIDUALS_PATH,
    ensure_r7_state_dir,
)

REGISTRY_SCHEMA = "r7_lifecycle_residuals_v1"
CLEANUP_POLICY_NONE = "NONE"


def residual_identity_key(
    condition_id: str, token_id: str, originating_run_id: str
) -> str:
    return f"{condition_id}|{token_id}|{originating_run_id}"


@dataclass
class LifecycleResidualRecord:
    condition_id: str
    token_id: str
    originating_run_id: str
    market_slug: str
    acquired_quantity: str
    exited_quantity: str
    residual_quantity: str
    min_tradable: str
    classification: str
    provenance: str
    created_at: str
    updated_at: str
    last_reconciliation_source: str
    tradable: bool
    cleanup_policy: str = CLEANUP_POLICY_NONE
    buy_order_id: str | None = None
    closed: bool = False
    closed_at: str | None = None
    historical_provenance_retained: bool = True

    @property
    def identity_key(self) -> str:
        return residual_identity_key(
            self.condition_id, self.token_id, self.originating_run_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_key": self.identity_key,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "token_suffix": self.token_id[-8:],
            "condition_suffix": self.condition_id[-8:],
            "originating_run_id": self.originating_run_id,
            "market_slug": self.market_slug,
            "acquired_quantity": self.acquired_quantity,
            "exited_quantity": self.exited_quantity,
            "residual_quantity": self.residual_quantity,
            "min_tradable": self.min_tradable,
            "classification": self.classification,
            "provenance": self.provenance,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_reconciliation_source": self.last_reconciliation_source,
            "tradable": self.tradable,
            "cleanup_policy": self.cleanup_policy,
            "buy_order_id": self.buy_order_id,
            "closed": self.closed,
            "closed_at": self.closed_at,
            "historical_provenance_retained": self.historical_provenance_retained,
            "in_acknowledgment_set": False,
            "prohibitions": {
                "sell_auto": True,
                "redeem": True,
                "merge_split": True,
                "transfer": True,
                "approve": True,
                "on_chain": True,
                "append_to_acknowledgment_set": True,
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LifecycleResidualRecord:
        return cls(
            condition_id=str(data["condition_id"]),
            token_id=str(data["token_id"]),
            originating_run_id=str(data["originating_run_id"]),
            market_slug=str(data.get("market_slug") or ""),
            acquired_quantity=str(data.get("acquired_quantity") or "0"),
            exited_quantity=str(data.get("exited_quantity") or "0"),
            residual_quantity=str(data.get("residual_quantity") or data.get("quantity") or "0"),
            min_tradable=str(data.get("min_tradable") or str(DEFAULT_MIN_TRADABLE)),
            classification=str(data.get("classification") or ""),
            provenance=str(data.get("provenance") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or data.get("created_at") or ""),
            last_reconciliation_source=str(
                data.get("last_reconciliation_source") or "unknown"
            ),
            tradable=bool(data.get("tradable", False)),
            cleanup_policy=str(data.get("cleanup_policy") or CLEANUP_POLICY_NONE),
            buy_order_id=None if data.get("buy_order_id") is None else str(data["buy_order_id"]),
            closed=bool(data.get("closed", False)),
            closed_at=None if data.get("closed_at") is None else str(data["closed_at"]),
            historical_provenance_retained=bool(
                data.get("historical_provenance_retained", True)
            ),
        )


@dataclass
class LifecycleResidualRegistry:
    schema_version: str = REGISTRY_SCHEMA
    residuals: dict[str, LifecycleResidualRecord] = field(default_factory=dict)
    migrated_from_lifecycle_dust: bool = False

    def open_residuals(self) -> list[LifecycleResidualRecord]:
        return [r for r in self.residuals.values() if not r.closed]

    def token_ids(self, *, include_closed: bool = False) -> list[str]:
        out: list[str] = []
        for r in self.residuals.values():
            if r.closed and not include_closed:
                continue
            out.append(r.token_id)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cleanup_policy_default": CLEANUP_POLICY_NONE,
            "migrated_from_lifecycle_dust": self.migrated_from_lifecycle_dust,
            "note": (
                "Multi-record residual registry. Not disposable reports. "
                "No automatic cleanup authorized."
            ),
            "residuals": {
                k: v.to_dict()
                for k, v in sorted(self.residuals.items(), key=lambda kv: kv[0])
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LifecycleResidualRegistry:
        raw = data.get("residuals") or {}
        residuals: dict[str, LifecycleResidualRecord] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, Mapping):
                    rec = LifecycleResidualRecord.from_dict(v)
                    residuals[str(k)] = rec
        elif isinstance(raw, list):
            for v in raw:
                if isinstance(v, Mapping):
                    rec = LifecycleResidualRecord.from_dict(v)
                    residuals[rec.identity_key] = rec
        return cls(
            schema_version=str(data.get("schema_version") or REGISTRY_SCHEMA),
            residuals=residuals,
            migrated_from_lifecycle_dust=bool(
                data.get("migrated_from_lifecycle_dust", False)
            ),
        )


def incident_residual_record(
    *, now: str | None = None
) -> LifecycleResidualRecord:
    ts = now or datetime.now(timezone.utc).isoformat()
    flat = classify_flatness(
        conditional_balance=INCIDENT_DUST_QTY,
        balance_known=True,
        min_tradable=DEFAULT_MIN_TRADABLE,
    )
    return LifecycleResidualRecord(
        condition_id=INCIDENT_DUST_CONDITION,
        token_id=INCIDENT_DUST_TOKEN,
        originating_run_id="d632b631-166f-4e35-8db8-fe69a3f86795",
        market_slug="btc-updown-5m-1784303100",
        acquired_quantity="9.470587",
        exited_quantity="9.47",
        residual_quantity=str(INCIDENT_DUST_QTY),
        min_tradable=str(DEFAULT_MIN_TRADABLE),
        classification=str(flat["classification"]),
        provenance="r7b_buy_0x68efa63a_plus_manual_ui_sell",
        created_at=ts,
        updated_at=ts,
        last_reconciliation_source="r7c_incident_recon",
        tradable=False,
        cleanup_policy=CLEANUP_POLICY_NONE,
        buy_order_id="0x68efa63a23abb0ab55042204683f48f4303ed2db3e9d955317bc41add43e71db",
        closed=False,
    )


def upsert_residual(
    registry: LifecycleResidualRegistry, record: LifecycleResidualRecord
) -> LifecycleResidualRegistry:
    """Insert or update by identity key — never overwrite a different identity."""
    key = record.identity_key
    existing = registry.residuals.get(key)
    if existing is not None and existing.identity_key != key:
        raise ValueError("RESIDUAL_IDENTITY_COLLISION")
    # Preserve created_at on update
    if existing is not None:
        record = LifecycleResidualRecord(
            **{
                **record.__dict__,
                "created_at": existing.created_at or record.created_at,
            }
        )
    registry.residuals[key] = record
    return registry


def close_residual_if_zero(
    registry: LifecycleResidualRegistry,
    *,
    condition_id: str,
    token_id: str,
    originating_run_id: str,
    source: str,
) -> LifecycleResidualRegistry:
    key = residual_identity_key(condition_id, token_id, originating_run_id)
    rec = registry.residuals.get(key)
    if rec is None:
        return registry
    if Decimal(rec.residual_quantity) != 0:
        return registry
    now = datetime.now(timezone.utc).isoformat()
    registry.residuals[key] = LifecycleResidualRecord(
        **{
            **rec.__dict__,
            "closed": True,
            "closed_at": now,
            "updated_at": now,
            "last_reconciliation_source": source,
            "historical_provenance_retained": True,
            "classification": FlatClassification.FLAT.value,
        }
    )
    return registry


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".residuals_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_residual_registry(
    registry: LifecycleResidualRegistry,
    *,
    repo_root: Path | None = None,
    path: Path | None = None,
) -> Path:
    ensure_r7_state_dir(repo_root)
    dest = path or ((repo_root or Path.cwd()) / DEFAULT_LIFECYCLE_RESIDUALS_PATH)
    _atomic_write(dest, json.dumps(registry.to_dict(), indent=2) + "\n")
    return dest


def read_residual_registry(
    path: Path | None = None, *, repo_root: Path | None = None
) -> LifecycleResidualRegistry | None:
    dest = path or ((repo_root or Path.cwd()) / DEFAULT_LIFECYCLE_RESIDUALS_PATH)
    if not dest.exists():
        return None
    return LifecycleResidualRegistry.from_dict(
        json.loads(dest.read_text(encoding="utf-8"))
    )


def migrate_dust_to_registry(
    *,
    repo_root: Path | None = None,
    force_incident: bool = True,
) -> LifecycleResidualRegistry:
    """Load registry or migrate legacy lifecycle_dust.json → multi-record registry."""
    root = repo_root or Path.cwd()
    ensure_r7_state_dir(root)
    existing = read_residual_registry(repo_root=root)
    if existing is not None and existing.residuals:
        return existing

    registry = existing or LifecycleResidualRegistry()
    dust = read_lifecycle_dust(repo_root=root)
    if dust:
        # Map scalar dust → residual record (preserve identity fields)
        now = datetime.now(timezone.utc).isoformat()
        qty = str(dust.get("quantity") or INCIDENT_DUST_QTY)
        flat = classify_flatness(
            conditional_balance=Decimal(qty),
            balance_known=True,
            min_tradable=Decimal(str(dust.get("min_tradable") or DEFAULT_MIN_TRADABLE)),
        )
        rec = LifecycleResidualRecord(
            condition_id=str(dust.get("condition_id") or INCIDENT_DUST_CONDITION),
            token_id=str(dust.get("token_id") or INCIDENT_DUST_TOKEN),
            originating_run_id=str(
                dust.get("originating_run_id")
                or "d632b631-166f-4e35-8db8-fe69a3f86795"
            ),
            market_slug=str(dust.get("market_slug") or ""),
            acquired_quantity=str(dust.get("acquired_quantity") or "9.470587"),
            exited_quantity=str(dust.get("exited_quantity") or "9.47"),
            residual_quantity=qty,
            min_tradable=str(dust.get("min_tradable") or DEFAULT_MIN_TRADABLE),
            classification=str(dust.get("classification") or flat["classification"]),
            provenance=str(dust.get("provenance") or "migrated_from_lifecycle_dust"),
            created_at=str(dust.get("created_at") or now),
            updated_at=now,
            last_reconciliation_source="migrate_lifecycle_dust",
            tradable=False,
            cleanup_policy=CLEANUP_POLICY_NONE,
            buy_order_id=None if dust.get("buy_order_id") is None else str(dust["buy_order_id"]),
        )
        upsert_residual(registry, rec)
        registry.migrated_from_lifecycle_dust = True
    elif force_incident:
        upsert_residual(registry, incident_residual_record())
        registry.migrated_from_lifecycle_dust = True

    write_residual_registry(registry, repo_root=root)
    return registry


def residual_token_ids(registry: LifecycleResidualRegistry | None) -> list[str]:
    if registry is None:
        return []
    return registry.token_ids(include_closed=False)


def evaluate_residuals_for_entry(
    registry: LifecycleResidualRegistry | None,
    *,
    selected_token_id: str | None = None,
) -> dict[str, Any]:
    """Entry gate: unknown/tradable residual blocks; known non-tradable dust OK.

    Selected-market flatness is evaluated against the selected token separately;
    this only reports registry-side blockers.
    """
    if registry is None:
        return {
            "ok": True,
            "blockers": [],
            "open_count": 0,
            "visible": [],
            "selected_token_known_dust": False,
        }
    blockers: list[str] = []
    visible = []
    selected_dust = False
    for rec in registry.open_residuals():
        visible.append(
            {
                "identity_key": rec.identity_key,
                "token_suffix": rec.token_id[-8:],
                "classification": rec.classification,
                "tradable": rec.tradable,
                "residual_quantity": rec.residual_quantity,
                "cleanup_policy": rec.cleanup_policy,
            }
        )
        if selected_token_id and rec.token_id == selected_token_id:
            selected_dust = True
        if rec.tradable:
            blockers.append("TRADABLE_RESIDUAL_EXPOSURE")
        if rec.classification in {
            "RESIDUAL_EXPOSURE",
            "MANUAL_INTERVENTION",
            "UNKNOWN",
        } or rec.classification.startswith("UNKNOWN"):
            blockers.append("UNKNOWN_OR_UNSAFE_RESIDUAL")
        if Decimal(rec.residual_quantity) >= Decimal(rec.min_tradable):
            # Should have been classified tradable; fail closed
            if not rec.tradable:
                blockers.append("RESIDUAL_QTY_GE_MIN_TRADABLE_MISCLASSIFIED")
    # Dedup blockers
    blockers = list(dict.fromkeys(blockers))
    return {
        "ok": not blockers,
        "blockers": blockers,
        "open_count": len(registry.open_residuals()),
        "visible": visible,
        "selected_token_known_dust": selected_dust,
        "cleanup_authorized": False,
    }


# Back-compat helpers used by older call sites
def ensure_incident_in_registry(*, repo_root: Path | None = None) -> LifecycleResidualRegistry:
    return migrate_dust_to_registry(repo_root=repo_root, force_incident=True)
