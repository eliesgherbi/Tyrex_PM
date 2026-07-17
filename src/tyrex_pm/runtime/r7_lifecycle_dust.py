"""Known R7B lifecycle residual dust — separate from user acknowledgment set."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.settlement import (
    DEFAULT_MIN_TRADABLE,
    FlatClassification,
    classify_flatness,
)
from tyrex_pm.runtime.r7_paths import DEFAULT_LIFECYCLE_DUST_PATH, ensure_r7_state_dir


# Incident residual (R7B BUY + manual UI SELL)
INCIDENT_DUST_TOKEN = (
    "1038082852808592687103030316298741805143710778541582307413776216436336466979"
)
INCIDENT_DUST_CONDITION = (
    "0x32204a5cffff255df6155b69105aded512770c4597ccb4bef721dfa0ab526401"
)
INCIDENT_DUST_QTY = Decimal("0.000587")


@dataclass(frozen=True)
class LifecycleDustRecord:
    schema_version: str
    token_id: str
    condition_id: str
    quantity: str
    classification: str
    provenance: str
    market_slug: str
    min_tradable: str
    buy_order_id: str | None
    created_at: str
    prohibitions: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "token_id": self.token_id,
            "token_suffix": self.token_id[-8:],
            "condition_id": self.condition_id,
            "quantity": self.quantity,
            "classification": self.classification,
            "provenance": self.provenance,
            "market_slug": self.market_slug,
            "min_tradable": self.min_tradable,
            "buy_order_id": self.buy_order_id,
            "created_at": self.created_at,
            "prohibitions": dict(self.prohibitions),
            "in_acknowledgment_set": False,
            "auto_cleanup": False,
        }


def default_incident_dust_record() -> LifecycleDustRecord:
    flat = classify_flatness(
        conditional_balance=INCIDENT_DUST_QTY,
        balance_known=True,
        min_tradable=DEFAULT_MIN_TRADABLE,
    )
    return LifecycleDustRecord(
        schema_version="r7_lifecycle_dust_v1",
        token_id=INCIDENT_DUST_TOKEN,
        condition_id=INCIDENT_DUST_CONDITION,
        quantity=str(INCIDENT_DUST_QTY),
        classification=flat["classification"],
        provenance="r7b_buy_0x68efa63a_plus_manual_ui_sell",
        market_slug="btc-updown-5m-1784303100",
        min_tradable=str(DEFAULT_MIN_TRADABLE),
        buy_order_id="0x68efa63a23abb0ab55042204683f48f4303ed2db3e9d955317bc41add43e71db",
        created_at=datetime.now(timezone.utc).isoformat(),
        prohibitions={
            "sell_auto": True,
            "redeem": True,
            "merge_split": True,
            "transfer": True,
            "approve": True,
            "on_chain": True,
            "append_to_acknowledgment_set": True,
        },
    )


def write_lifecycle_dust(
    path: Path | None = None,
    record: LifecycleDustRecord | None = None,
    *,
    repo_root: Path | None = None,
) -> Path:
    ensure_r7_state_dir(repo_root)
    dest = path or ((repo_root or Path.cwd()) / DEFAULT_LIFECYCLE_DUST_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps((record or default_incident_dust_record()).to_dict(), indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".dust_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return dest


def read_lifecycle_dust(path: Path | None = None, *, repo_root: Path | None = None) -> dict[str, Any] | None:
    dest = path or ((repo_root or Path.cwd()) / DEFAULT_LIFECYCLE_DUST_PATH)
    if not dest.exists():
        return None
    return json.loads(dest.read_text(encoding="utf-8"))


def dust_token_ids(dust: dict[str, Any] | None) -> list[str]:
    if not dust:
        return []
    tid = dust.get("token_id")
    return [str(tid)] if tid else []
