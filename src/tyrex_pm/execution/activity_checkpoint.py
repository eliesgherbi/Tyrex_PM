"""Accumulated wallet activity and the cursor that must not run ahead of it.

The checkpoint and the accepted rows are one journal document. A truncated or
rejected inspection does not replace that document.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

LEDGER_META_KEY = "account.activity.ledger"
CHECKPOINT_SCHEMA = 1
QUERY_SCOPE = "authenticated_wallet_activity"
OVERLAP_S = 120
FULL_VERIFY_EVERY = 12
ACTIVITY_PAGE_SIZE = 500


def load_activity_ledger(journal: Any) -> dict[str, Any] | None:
    if journal is None or not hasattr(journal, "get_meta"):
        return None
    raw = journal.get_meta(LEDGER_META_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    checkpoint = payload.get("checkpoint")
    records = payload.get("records")
    if not isinstance(checkpoint, dict) or not isinstance(records, list):
        return None
    return {"checkpoint": dict(checkpoint), "records": list(records)}


def save_activity_ledger(journal: Any, ledger: Mapping[str, Any]) -> None:
    journal.set_meta(LEDGER_META_KEY, json.dumps(_jsonable(ledger), sort_keys=True))


def ledger_is_compatible(
    ledger: Mapping[str, Any] | None,
    *,
    wallet: str,
    chain_id: int,
    environment: str,
) -> bool:
    if not ledger:
        return False
    checkpoint = ledger.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return False
    return (
        checkpoint.get("schema") == CHECKPOINT_SCHEMA
        and str(checkpoint.get("wallet") or "").lower() == wallet.lower()
        and int(checkpoint.get("chain_id") or 0) == int(chain_id)
        and checkpoint.get("environment") == environment
        and checkpoint.get("query_scope") == QUERY_SCOPE
        and isinstance(checkpoint.get("coverage_through_unix"), int)
        and isinstance(checkpoint.get("boundary_activity_ids"), list)
    )


def collection_mode(
    ledger: Mapping[str, Any] | None,
    *,
    wallet: str,
    chain_id: int,
    environment: str,
) -> str:
    if not ledger_is_compatible(
        ledger, wallet=wallet, chain_id=chain_id, environment=environment
    ):
        return "full"
    checkpoint = ledger["checkpoint"] if ledger is not None else {}
    if int(checkpoint.get("incremental_since_full") or 0) >= FULL_VERIFY_EVERY:
        return "full"
    return "incremental"


def incremental_start_unix(
    ledger: Mapping[str, Any] | None,
    *,
    wallet: str,
    chain_id: int,
    environment: str,
) -> int | None:
    if collection_mode(ledger, wallet=wallet, chain_id=chain_id, environment=environment) != (
        "incremental"
    ):
        return None
    assert ledger is not None
    through = int(ledger["checkpoint"]["coverage_through_unix"])
    return max(0, through - OVERLAP_S)


def propose_activity_ledger(
    previous: Mapping[str, Any] | None,
    activities: Sequence[Any],
    *,
    mode: str,
    complete: bool,
    wallet: str,
    chain_id: int,
    environment: str,
    observed_unix: int,
) -> dict[str, Any] | None:
    """Return the next ledger, or None when this read must not move coverage."""
    if not complete or mode not in {"full", "incremental"}:
        return None
    if mode == "incremental" and not ledger_is_compatible(
        previous, wallet=wallet, chain_id=chain_id, environment=environment
    ):
        return None
    rows: dict[str, dict[str, Any]] = {}
    if previous and ledger_is_compatible(
        previous, wallet=wallet, chain_id=chain_id, environment=environment
    ):
        for item in previous["records"]:
            if isinstance(item, Mapping) and item.get("activity_id"):
                rows[str(item["activity_id"])] = dict(item)
    for activity in activities:
        row = activity_row(activity)
        rows[row["activity_id"]] = row
    timestamps = [
        int(row["observed_unix"])
        for row in rows.values()
        if row.get("observed_unix") is not None
    ]
    previous_through = 0
    previous_full = 0
    previous_incremental = 0
    if previous and ledger_is_compatible(
        previous, wallet=wallet, chain_id=chain_id, environment=environment
    ):
        previous_through = int(previous["checkpoint"]["coverage_through_unix"])
        previous_full = int(previous["checkpoint"].get("full_verifications_accepted") or 0)
        previous_incremental = int(previous["checkpoint"].get("incremental_since_full") or 0)
    if timestamps:
        coverage = max(previous_through, max(timestamps))
    elif mode == "full":
        coverage = max(previous_through, int(observed_unix))
    else:
        coverage = max(previous_through, int(observed_unix))
    boundary = sorted(
        row["activity_id"]
        for row in rows.values()
        if row.get("observed_unix") == coverage
    )
    records = sorted(
        rows.values(),
        key=lambda item: (item.get("observed_unix") or 0, item["activity_id"]),
    )
    return {
        "checkpoint": {
            "schema": CHECKPOINT_SCHEMA,
            "wallet": wallet,
            "chain_id": int(chain_id),
            "environment": environment,
            "query_scope": QUERY_SCOPE,
            "coverage_through_unix": coverage,
            "overlap_s": OVERLAP_S,
            "boundary_activity_ids": boundary,
            "full_verifications_accepted": previous_full + (1 if mode == "full" else 0),
            "incremental_since_full": 0 if mode == "full" else previous_incremental + 1,
            "full_verify_every": FULL_VERIFY_EVERY,
        },
        "records": records,
    }


def activity_row(activity: Any) -> dict[str, Any]:
    observed = getattr(activity, "observed_at", None)
    observed_unix = None if observed is None else int(observed.timestamp())
    return {
        "activity_id": str(activity.activity_id),
        "condition_id": str(activity.condition_id),
        "token_id": str(activity.token_id),
        "shares": str(activity.shares),
        "cause": str(activity.cause),
        "side": activity.side,
        "observed_at": None if observed is None else observed.astimezone(timezone.utc).isoformat(),
        "observed_unix": observed_unix,
        "transaction_hash": activity.transaction_hash,
        "venue_trade_id": activity.venue_trade_id,
        "venue_order_id": activity.venue_order_id,
        "activity_type": activity.activity_type,
    }


def activities_from_ledger(ledger: Mapping[str, Any] | None) -> tuple[Any, ...]:
    from tyrex_pm.execution.startup_reconciliation import ActivityView

    if not ledger:
        return ()
    views = []
    for row in ledger.get("records") or ():
        if not isinstance(row, Mapping) or not row.get("activity_id"):
            continue
        observed = row.get("observed_at")
        parsed = None
        if isinstance(observed, str) and observed:
            parsed = datetime.fromisoformat(observed)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        views.append(
            ActivityView(
                activity_id=str(row["activity_id"]),
                condition_id=str(row.get("condition_id") or ""),
                token_id=str(row.get("token_id") or ""),
                shares=Decimal(str(row.get("shares") or "0")),
                cause=str(row.get("cause") or ""),
                side=None if row.get("side") is None else str(row.get("side")),
                observed_at=parsed,
                transaction_hash=(
                    None
                    if row.get("transaction_hash") is None
                    else str(row.get("transaction_hash"))
                ),
                venue_trade_id=(
                    None if row.get("venue_trade_id") is None else str(row.get("venue_trade_id"))
                ),
                venue_order_id=(
                    None if row.get("venue_order_id") is None else str(row.get("venue_order_id"))
                ),
                activity_type=(
                    None if row.get("activity_type") is None else str(row.get("activity_type"))
                ),
            )
        )
    return tuple(views)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
