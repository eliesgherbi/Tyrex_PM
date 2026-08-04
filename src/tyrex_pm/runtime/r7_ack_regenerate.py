"""Read-only regeneration of durable R7 acknowledgment state (R7D.2).

Zero mutations. Never restores a deleted artifact by acknowledging every
resolved position on the account.

Policy source (exact four identities):
  1. ``config/r7/acknowledgment_policy.json`` (committed source of truth)
  2. ``var/runtime_state/r7/acknowledgment_policy.json`` (durable mirror)

After the acknowledgment artifact is deleted, regenerate reloads that sealed
policy and matches inventory rows by identity. Extra resolved positions are
never added. Changed token/condition IDs block regeneration.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.r7_ack_gate import enforce_acknowledgment_gate
from tyrex_pm.runtime.r7_ack_policy import (
    AcknowledgmentPolicy,
    assert_policy_not_broadened,
    load_acknowledgment_policy,
    policy_from_acknowledgment_dict,
    select_rows_for_policy,
    write_acknowledgment_policy,
)
from tyrex_pm.runtime.r7_lifecycle_residuals import (
    ensure_incident_in_registry,
    residual_token_ids,
)
from tyrex_pm.runtime.r7_paths import (
    DEFAULT_ACKNOWLEDGMENT_PATH,
    ensure_r7_state_dir,
    resolve_acknowledgment_path,
)
from tyrex_pm.runtime.r7_position_ack import (
    AckError,
    build_acknowledgment,
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
    from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport

    env = _load_dotenv(repo_root / ".env")
    return SdkReadonlyTransport.from_env(env).get_positions_raw()


def _resolve_policy(
    *,
    repo: Path,
    policy: AcknowledgmentPolicy | None,
    bootstrap_from_ack_path: Path | None,
) -> AcknowledgmentPolicy:
    if policy is not None:
        return policy
    try:
        return load_acknowledgment_policy(repo_root=repo)
    except AckError:
        if bootstrap_from_ack_path and bootstrap_from_ack_path.exists():
            ack = json.loads(bootstrap_from_ack_path.read_text(encoding="utf-8"))
            sealed = policy_from_acknowledgment_dict(ack)
            write_acknowledgment_policy(sealed, repo_root=repo)
            return sealed
        raise AckError(
            "ACK_POLICY_MISSING — sealed policy required at "
            "config/r7/acknowledgment_policy.json (or var/runtime_state mirror). "
            "Regenerate will not invent identities from all account positions."
        )


def regenerate_acknowledgment(
    *,
    repo_root: Path | None = None,
    output_path: Path | None = None,
    positions_provider: Any | None = None,
    write_residual_state: bool = True,
    write_dust_state: bool = True,  # back-compat alias → residuals
    policy: AcknowledgmentPolicy | None = None,
    bootstrap_policy_from_existing_ack: bool = False,
) -> dict[str, Any]:
    """Regenerate durable ack from sealed policy + inventory. mutations_attempted=False."""
    del write_dust_state  # superseded by residual registry
    repo = repo_root or Path.cwd()
    ensure_r7_state_dir(repo)
    dest = output_path or resolve_acknowledgment_path(None, repo_root=repo)
    commit = _git_commit(repo)
    now = datetime.now(timezone.utc).isoformat()

    bootstrap_path = (
        (repo / DEFAULT_ACKNOWLEDGMENT_PATH)
        if bootstrap_policy_from_existing_ack
        else None
    )
    pol = _resolve_policy(
        repo=repo,
        policy=policy,
        bootstrap_from_ack_path=bootstrap_path,
    )

    raw = (
        list(positions_provider())
        if positions_provider is not None
        else fetch_positions_readonly(repo)
    )

    registry = None
    residual_path = None
    if write_residual_state:
        registry = ensure_incident_in_registry(repo_root=repo)
        residual_path = repo / "var" / "state" / "r7" / "lifecycle_residuals.json"
    exclude = set(residual_token_ids(registry))

    selected = select_rows_for_policy(raw, pol, exclude_token_ids=exclude)
    assert_policy_not_broadened(pol, selected)

    # Ensure policy mirror exists under durable state (never under reporting)
    write_acknowledgment_policy(pol, repo_root=repo, write_config=True, write_state=True)

    ack = build_acknowledgment(raw_positions=selected, commit_identity=commit)
    content_hash = write_acknowledgment(dest, ack)

    gate = enforce_acknowledgment_gate(
        acknowledgment_path=dest,
        raw_positions=selected,
        repo_root=repo,
        ignore_selected_market_tokens=list(exclude),
    )

    report = {
        "schema": "r7d2_ack_regenerate_v1",
        "ts": now,
        "mutations_attempted": False,
        "mutations_enabled": False,
        "path": str(dest),
        "artifact_id": ack.acknowledgment_id,
        "content_hash": content_hash,
        "schema_version": ack.schema_version,
        "policy_id": ack.policy_id,
        "policy_source": pol.source,
        "policy_paths": [
            "config/r7/acknowledgment_policy.json",
            "var/runtime_state/r7/acknowledgment_policy.json",
        ],
        "policy_identity_keys": sorted(pol.content_keys()),
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
        "lifecycle_residuals_path": None if residual_path is None else str(residual_path),
        "lifecycle_residuals": None if registry is None else registry.to_dict(),
        # back-compat field for older report consumers
        "lifecycle_dust": (
            None
            if registry is None or not registry.open_residuals()
            else registry.open_residuals()[0].to_dict()
        ),
        "prohibitions": dict(ack.prohibitions),
        "note": (
            "Regeneration uses sealed acknowledgment_policy identities only. "
            "It does not acknowledge every resolved position. "
            "Not authorization to sell/redeem/transfer/approve/merge/split "
            "or perform on-chain actions."
        ),
    }
    if not gate.ok:
        report["ok"] = False
        return report
    report["ok"] = True
    return report
