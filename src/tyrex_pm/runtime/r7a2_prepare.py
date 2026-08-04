"""R7A.2: record position acknowledgment + draft session envelope (no R7B arm)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.r7_position_ack import (
    ACKNOWLEDGMENT_TEXT,
    build_acknowledgment,
    inventory_rows_marked_acknowledged,
    validate_acknowledgment_against_inventory,
    write_acknowledgment,
)
from tyrex_pm.runtime.r7_position_inventory import build_inventory_report
from tyrex_pm.runtime.r7_readiness import build_r7_readiness
from tyrex_pm.runtime.r7b_session import (
    build_session_authorization,
    future_authorization_wording,
    write_session_authorization,
)


def git_commit_identity(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _load_dotenv_map(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _fetch_positions(repo_root: Path) -> list[dict[str, Any]]:
    from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport

    env = _load_dotenv_map(repo_root / ".env")
    ro = SdkReadonlyTransport.from_env(env)
    return ro.get_positions_raw()


def prepare_r7a2(
    *,
    repo_root: Path,
    output_dir: Path,
    preflight_path: Path | None = None,
) -> dict[str, Any]:
    """Record ack + draft session. Never sets user_authorization_present or arms mutations."""
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    commit = git_commit_identity(repo_root)

    raw_positions = _fetch_positions(repo_root)
    ack = build_acknowledgment(raw_positions=raw_positions, commit_identity=commit)
    ack_path = output_dir / "r7a2_position_acknowledgment.json"
    ack_hash = write_acknowledgment(ack_path, ack)

    ack_validation = validate_acknowledgment_against_inventory(
        ack,
        raw_positions=raw_positions,
        selected_token_ids=[],
        selected_condition_id=None,
        open_order_count=0,
    )

    inventory = build_inventory_report(
        raw_positions,
        selected_token_ids=[],
        selected_condition_id=None,
        reconciliation_clean=True,
    )

    user_stream_ready = False
    reconciliation_unreachable = True
    open_order_count = 0
    balance_ok = False
    preflight_hash = "none"
    if preflight_path and preflight_path.exists():
        raw = preflight_path.read_text(encoding="utf-8")
        preflight_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        pf = json.loads(raw)
        user_stream_ready = bool((pf.get("user_stream") or {}).get("authenticated"))
        recon = pf.get("reconciliation") or {}
        reconciliation_unreachable = bool(recon.get("unreachable_account", True))
        open_order_count = int(recon.get("open_order_count") or 0)
        balance_ok = bool((pf.get("balance_evidence") or {}).get("retrieved"))
        ack_validation = validate_acknowledgment_against_inventory(
            ack,
            raw_positions=raw_positions,
            open_order_count=open_order_count,
        )

    readiness_before = build_r7_readiness(
        inventory=inventory,
        user_stream_ready=user_stream_ready,
        reconciliation_unreachable=reconciliation_unreachable,
        open_order_count=open_order_count,
        fee_resolved=True,
        market_window_blocker=None,
        sizing_blocker=None,
        balance_ok=balance_ok,
        account_policy="require_ack_resolved_redeemable",
    )
    readiness_after = build_r7_readiness(
        inventory=inventory,
        user_stream_ready=user_stream_ready,
        reconciliation_unreachable=reconciliation_unreachable,
        open_order_count=open_order_count,
        fee_resolved=True,
        market_window_blocker=None,
        sizing_blocker=None,
        balance_ok=balance_ok,
        account_policy="acknowledged_resolved_redeemable",
        acknowledgment_valid=ack_validation.ok,
        acknowledgment_blockers=ack_validation.blockers,
    )

    session = build_session_authorization(
        commit_identity=commit,
        acknowledged_position_set_id=ack.acknowledgment_id,
        acknowledged_position_set_fingerprint=ack.set_fingerprint(),
    )
    # Explicitly NOT user-authorized — draft for future approval only
    assert session.user_authorization_present is False
    session_path = output_dir / "r7b_session_envelope_DRAFT.json"
    session_hash = write_session_authorization(session_path, session)
    wording = future_authorization_wording(session)
    wording_path = output_dir / "r7b_future_authorization_statement.txt"
    wording_path.write_text(wording + "\n", encoding="utf-8")

    report = {
        "phase": "R7A.2",
        "ts": now.isoformat(),
        "commit_identity": commit,
        "r7a_r7a1_checkpoint": commit,
        "acknowledgment_text": ACKNOWLEDGMENT_TEXT,
        "acknowledgment": {
            "id": ack.acknowledgment_id,
            "content_hash": ack_hash,
            "position_set_fingerprint": ack.set_fingerprint(),
            "path": str(ack_path),
            "expected_count": 4,
            "validation": ack_validation.to_dict(),
            "visible_account_wide": inventory_rows_marked_acknowledged(inventory, ack),
            "cleanup_targets": False,
            "sell_forbidden": True,
            "redeem_forbidden": True,
        },
        "readiness_before_ack_policy": readiness_before.to_dict(),
        "readiness_after_ack": readiness_after.to_dict(),
        "session_draft": {
            "session_id": session.session_id,
            "content_hash": session_hash,
            "path": str(session_path),
            "expires_at": session.expires_at,
            "user_authorization_present": False,
            "armed": False,
            "mutations_enabled": False,
            "limits": session.limits,
            "authorization_statement_path": str(wording_path),
        },
        "preflight_hash": preflight_hash,
        "mutations_enabled": False,
        "r7b_authorized": False,
        "network_arm_token_issued": False,
    }
    report_path = output_dir / "r7a2_report.json"
    report["r7a2_report_hash"] = hashlib.sha256(
        json.dumps(report, sort_keys=True).encode("utf-8")
    ).hexdigest()
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
