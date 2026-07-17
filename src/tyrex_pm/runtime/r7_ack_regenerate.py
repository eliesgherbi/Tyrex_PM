"""Read-only regeneration of durable R7 acknowledgment state (R7D.1).

Zero mutations. Never restores a deleted artifact blindly — rebuilds from
current authoritative inventory and validates exactly four
RESOLVED_REDEEMABLE positions.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tyrex_pm.runtime.r7_ack_gate import enforce_acknowledgment_gate
from tyrex_pm.runtime.r7_lifecycle_dust import (
    default_incident_dust_record,
    write_lifecycle_dust,
)
from tyrex_pm.runtime.r7_paths import (
    DEFAULT_ACKNOWLEDGMENT_PATH,
    ensure_r7_state_dir,
    resolve_acknowledgment_path,
)
from tyrex_pm.runtime.r7_position_ack import (
    AckError,
    PositionCategory,
    build_acknowledgment,
    fingerprint_from_raw,
    write_acknowledgment,
)


def _git_commit(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def fetch_positions_readonly(repo_root: Path) -> list[dict[str, Any]]:
    from tyrex_pm.execution.polymarket.auth import (
        load_l2_credentials,
        positions_wallet_address,
    )

    env = _load_dotenv(repo_root / ".env")
    creds = load_l2_credentials(env)
    user = positions_wallet_address(creds)
    url = f"https://data-api.polymarket.com/positions?{urlencode({'user': user})}"
    req = Request(url, headers={"User-Agent": "tyrex-pm-r7d1-ack/1.0"}, method="GET")
    with urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _select_exactly_four_resolved(
    raw: list[dict[str, Any]],
    *,
    exclude_token_ids: set[str],
) -> list[dict[str, Any]]:
    """Pick RESOLVED_REDEEMABLE nonzero rows, excluding known lifecycle dust tokens."""
    chosen: list[dict[str, Any]] = []
    for row in raw:
        size = Decimal(str(row.get("size") or "0"))
        if size == 0:
            continue
        token = str(row.get("asset") or row.get("token_id") or "")
        if token in exclude_token_ids:
            continue
        fp = fingerprint_from_raw(row)
        if not fp.redeemable or not fp.resolved:
            continue
        if fp.category != PositionCategory.RESOLVED_REDEEMABLE_POSITION.value:
            continue
        chosen.append(row)
    if len(chosen) != 4:
        raise AckError(f"EXPECTED_FOUR_RESOLVED_REDEEMABLE_GOT_{len(chosen)}")
    return chosen


def regenerate_acknowledgment(
    *,
    repo_root: Path | None = None,
    output_path: Path | None = None,
    positions_provider: Any | None = None,
    write_dust_state: bool = True,
) -> dict[str, Any]:
    """Regenerate durable ack from current inventory. mutations_attempted=False."""
    repo = repo_root or Path.cwd()
    ensure_r7_state_dir(repo)
    dest = output_path or resolve_acknowledgment_path(None, repo_root=repo)
    commit = _git_commit(repo)
    now = datetime.now(timezone.utc).isoformat()

    raw = (
        list(positions_provider())
        if positions_provider is not None
        else fetch_positions_readonly(repo)
    )
    dust_rec = default_incident_dust_record()
    exclude = {dust_rec.token_id}
    selected = _select_exactly_four_resolved(raw, exclude_token_ids=exclude)
    ack = build_acknowledgment(raw_positions=selected, commit_identity=commit)
    content_hash = write_acknowledgment(dest, ack)

    dust_path = None
    if write_dust_state:
        dust_path = write_lifecycle_dust(repo_root=repo, record=dust_rec)

    gate = enforce_acknowledgment_gate(
        acknowledgment_path=dest,
        raw_positions=selected,
        repo_root=repo,
        ignore_selected_market_tokens=[dust_rec.token_id],
    )

    report = {
        "schema": "r7d1_ack_regenerate_v1",
        "ts": now,
        "mutations_attempted": False,
        "mutations_enabled": False,
        "path": str(dest),
        "artifact_id": ack.acknowledgment_id,
        "content_hash": content_hash,
        "schema_version": ack.schema_version,
        "policy_id": ack.policy_id,
        "commit_identity": commit,
        "evidence_source": "data_api_positions_funder_wallet",
        "expected_count": 4,
        "positions": [
            {
                "condition_suffix": p.condition_id[-8:],
                "token_suffix": p.token_id[-8:],
                "quantity": p.quantity,
                "category": p.category,
            }
            for p in ack.positions
        ],
        "gate_ok": gate.ok,
        "gate_blockers": gate.blockers,
        "lifecycle_dust_path": None if dust_path is None else str(dust_path),
        "lifecycle_dust": dust_rec.to_dict(),
        "prohibitions": dict(ack.prohibitions),
        "note": (
            "Regeneration is not authorization to sell/redeem/transfer/approve/"
            "merge/split or perform on-chain actions."
        ),
    }
    if not gate.ok:
        report["ok"] = False
        return report
    report["ok"] = True
    return report
