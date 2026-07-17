"""Pinned acknowledgment policy — exact four identities (R7D.2).

Source of truth (committed, not disposable reports):
  ``config/r7/acknowledgment_policy.json``

Runtime mirror (durable state):
  ``var/state/r7/acknowledgment_policy.json``

Regeneration must match this policy exactly. It must not acknowledge every
resolved position on the account, and must not broaden the policy.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from tyrex_pm.runtime.r7_position_ack import AckError, identity_key


POLICY_SCHEMA = "r7_acknowledgment_policy_v1"
POLICY_ID = "ACK_RESOLVED_REDEEMABLE_UNTOUCHED_R7A1"

# Committed source of truth (survives report cleanup and state wipes if restored from git)
CONFIG_ACK_POLICY_PATH = Path("config/r7/acknowledgment_policy.json")
STATE_ACK_POLICY_PATH = Path("var/state/r7/acknowledgment_policy.json")


@dataclass(frozen=True)
class PolicyIdentity:
    condition_id: str
    token_id: str
    category: str = "RESOLVED_REDEEMABLE_POSITION"
    slug: str | None = None

    @property
    def key(self) -> str:
        return identity_key(self.condition_id, self.token_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "identity_key": self.key,
            "category": self.category,
            "slug": self.slug,
            "condition_suffix": self.condition_id[-8:],
            "token_suffix": self.token_id[-8:],
        }


@dataclass
class AcknowledgmentPolicy:
    schema_version: str
    policy_id: str
    expected_count: int
    identities: list[PolicyIdentity]
    created_at: str
    source: str
    sealed: bool = True

    def content_keys(self) -> set[str]:
        return {i.key for i in self.identities}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "expected_count": self.expected_count,
            "sealed": self.sealed,
            "created_at": self.created_at,
            "source": self.source,
            "note": (
                "Exact identities only. Regeneration must not broaden this set. "
                "Not stored under var/reporting/."
            ),
            "identities": [i.to_dict() for i in self.identities],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AcknowledgmentPolicy:
        ids = [
            PolicyIdentity(
                condition_id=str(x["condition_id"]),
                token_id=str(x["token_id"]),
                category=str(x.get("category") or "RESOLVED_REDEEMABLE_POSITION"),
                slug=None if x.get("slug") is None else str(x["slug"]),
            )
            for x in data["identities"]
        ]
        return cls(
            schema_version=str(data.get("schema_version") or POLICY_SCHEMA),
            policy_id=str(data.get("policy_id") or POLICY_ID),
            expected_count=int(data.get("expected_count") or len(ids)),
            identities=ids,
            created_at=str(data.get("created_at") or ""),
            source=str(data.get("source") or "unknown"),
            sealed=bool(data.get("sealed", True)),
        )


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".policy_", suffix=".tmp")
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


def write_acknowledgment_policy(
    policy: AcknowledgmentPolicy,
    *,
    repo_root: Path | None = None,
    write_config: bool = True,
    write_state: bool = True,
) -> list[Path]:
    root = repo_root or Path.cwd()
    raw = json.dumps(policy.to_dict(), indent=2) + "\n"
    written: list[Path] = []
    if write_config:
        p = root / CONFIG_ACK_POLICY_PATH
        _atomic_write(p, raw)
        written.append(p)
    if write_state:
        p = root / STATE_ACK_POLICY_PATH
        _atomic_write(p, raw)
        written.append(p)
    return written


def load_acknowledgment_policy(*, repo_root: Path | None = None) -> AcknowledgmentPolicy:
    """Load sealed policy: config (committed) preferred, then durable state."""
    root = repo_root or Path.cwd()
    for rel in (CONFIG_ACK_POLICY_PATH, STATE_ACK_POLICY_PATH):
        path = root / rel
        if path.exists():
            pol = AcknowledgmentPolicy.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if pol.expected_count != 4 or len(pol.identities) != 4:
                raise AckError("ACK_POLICY_EXPECTED_FOUR")
            if not pol.sealed:
                raise AckError("ACK_POLICY_NOT_SEALED")
            return pol
    raise AckError("ACK_POLICY_MISSING")


def policy_from_acknowledgment_dict(ack: Mapping[str, Any]) -> AcknowledgmentPolicy:
    ids = [
        PolicyIdentity(
            condition_id=str(p["condition_id"]),
            token_id=str(p["token_id"]),
            category=str(p.get("category") or "RESOLVED_REDEEMABLE_POSITION"),
            slug=None if p.get("slug") is None else str(p["slug"]),
        )
        for p in ack["positions"]
    ]
    if len(ids) != 4:
        raise AckError("ACK_POLICY_EXPECTED_FOUR")
    return AcknowledgmentPolicy(
        schema_version=POLICY_SCHEMA,
        policy_id=str(ack.get("policy_id") or POLICY_ID),
        expected_count=4,
        identities=ids,
        created_at=datetime.now(timezone.utc).isoformat(),
        source="sealed_from_acknowledgment_artifact",
        sealed=True,
    )


def select_rows_for_policy(
    raw: Sequence[Mapping[str, Any]],
    policy: AcknowledgmentPolicy,
    *,
    exclude_token_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return exactly the four policy rows. Fail closed on broaden/missing/change."""
    exclude = exclude_token_ids or set()
    by_key: dict[str, dict[str, Any]] = {}
    extras: list[str] = []
    for row in raw:
        size = Decimal(str(row.get("size") or "0"))
        if size == 0:
            continue
        cond = str(row.get("conditionId") or row.get("condition_id") or "")
        tok = str(row.get("asset") or row.get("token_id") or "")
        if tok in exclude:
            continue
        key = identity_key(cond, tok)
        if key in policy.content_keys():
            if key in by_key:
                raise AckError("ACK_POLICY_DUPLICATE_INVENTORY_ROW")
            by_key[key] = dict(row)
        else:
            # Extra resolved redeemable must not be auto-acknowledged
            if bool(row.get("redeemable")):
                extras.append(key)

    missing = policy.content_keys() - set(by_key)
    if missing:
        raise AckError("ACK_POLICY_IDENTITY_MISSING_OR_CHANGED")
    if extras:
        # Presence of additional resolved positions is allowed on account,
        # but they must not enter the acknowledgment set. We only fail if
        # selection would somehow include them — selection never includes extras.
        pass
    if len(by_key) != 4:
        raise AckError("ACK_POLICY_SELECTION_COUNT_MISMATCH")

    # Preserve policy order
    ordered = [by_key[i.key] for i in policy.identities]
    return ordered


def assert_policy_not_broadened(
    policy: AcknowledgmentPolicy, selected_rows: Sequence[Mapping[str, Any]]
) -> None:
    keys = set()
    for row in selected_rows:
        cond = str(row.get("conditionId") or row.get("condition_id") or "")
        tok = str(row.get("asset") or row.get("token_id") or "")
        keys.add(identity_key(cond, tok))
    if keys != policy.content_keys():
        raise AckError("ACK_POLICY_BROADEN_OR_SHRINK_FORBIDDEN")
