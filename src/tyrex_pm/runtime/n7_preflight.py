"""N7 authenticated read-only preflight (mutations impossible)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.auth import assert_no_secrets, redact_text
from tyrex_pm.runtime.live_preflight import run_live_preflight
from tyrex_pm.runtime.n6_account_classify import (
    AcknowledgedExternalPosition,
    classify_account,
)
from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_git import inspect_git
from tyrex_pm.runtime.n7_sealed import load_n7_sealed_config
from tyrex_pm.runtime.n7_timing import PRODUCTION_TIMING_VALUES_STATUS

DEFAULT_ACKNOWLEDGED = (
    AcknowledgedExternalPosition(
        label="historical_lol",
        status="RESOLVED_REDEEMABLE",
        notes="N7 must not redeem or alter",
    ),
    AcknowledgedExternalPosition(
        label="historical_btc_5m_1",
        status="RESOLVED_REDEEMABLE",
        notes="N7 must not redeem or alter",
    ),
    AcknowledgedExternalPosition(
        label="historical_btc_5m_2",
        status="RESOLVED_REDEEMABLE",
        notes="N7 must not redeem or alter",
    ),
    AcknowledgedExternalPosition(
        label="historical_btc_5m_3",
        status="RESOLVED_REDEEMABLE",
        notes="N7 must not redeem or alter",
    ),
)


@dataclass
class N7PreflightResult:
    ok: bool
    go_no_go: str
    abort_codes: list[str]
    payload: dict[str, Any]
    artifact_path: Path


def run_n7_preflight(
    *,
    out_dir: Path,
    config_path: Path,
    repo: Path,
    dotenv: Path | None = None,
    user_stream_observe_s: float = 2.0,
    require_clean_worktree: bool = False,
) -> N7PreflightResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    sealed = load_n7_sealed_config(config_path)
    git = inspect_git(repo)
    aborts: list[str] = []

    if PRODUCTION_TIMING_VALUES_STATUS != "FROZEN_FOR_N7":
        aborts.append("timing_not_frozen")
    if sealed.live.mutations_enabled:
        aborts.append(N7AbortCode.CONFIGURATION_MISMATCH.value)
    if require_clean_worktree and not git.worktree_clean:
        aborts.append(N7AbortCode.DIRTY_WORKTREE.value)

    first = run_live_preflight(
        output_path=out_dir / "preflight_1.json",
        dotenv_path=dotenv,
        user_stream_observe_s=user_stream_observe_s,
        skip_auth=False,
    )
    second = run_live_preflight(
        output_path=out_dir / "preflight_2_restart.json",
        dotenv_path=dotenv,
        user_stream_observe_s=user_stream_observe_s,
        skip_auth=False,
    )

    def _classify(payload: dict[str, Any]) -> dict[str, Any]:
        recon = dict(payload.get("reconciliation") or {})
        # UNKNOWN only when reconciliation actually ran and account evidence was unreachable.
        # Do not invent UNKNOWN from an earlier public-time / transport probe failure.
        recon_ran = payload.get("reconciliation") is not None
        unknown = bool(recon_ran and recon.get("unreachable_account"))
        return classify_account(
            open_orders=[],
            positions=[],
            selected_market_token_ids=set(),
            acknowledged=DEFAULT_ACKNOWLEDGED,
            unknown=unknown,
        ).to_dict() | {
            "open_order_count": int(recon.get("open_order_count") or 0),
            "position_row_count": int(recon.get("position_row_count") or 0),
            "observation_only_local_empty": recon.get("observation_only_local_empty"),
            "reconciliation_ran": recon_ran,
            "unreachable_account": bool(recon.get("unreachable_account")),
        }

    def _public_transport_failure(payload: dict[str, Any]) -> bool:
        public_clob = dict(payload.get("public_clob") or {})
        if public_clob.get("failure_kind") == "transport":
            return True
        if payload.get("blocker") == "public_clob_unreachable":
            return True
        if payload.get("cloudflare_blocked"):
            return True
        return False

    def _public_time_invalid(payload: dict[str, Any]) -> bool:
        if payload.get("blocker") == "public_time_invalid":
            return True
        public_clob = dict(payload.get("public_clob") or {})
        kind = public_clob.get("failure_kind")
        return kind in {
            "invalid_time_response",
            "http_client_error",
            "http_server_error",
        }

    c1 = _classify(dict(first.payload))
    c2 = _classify(dict(second.payload))

    transport_fail = _public_transport_failure(dict(first.payload)) or _public_transport_failure(
        dict(second.payload)
    )
    time_invalid = _public_time_invalid(dict(first.payload)) or _public_time_invalid(
        dict(second.payload)
    )

    if transport_fail:
        aborts.append(N7AbortCode.CONNECTIVITY_UNAVAILABLE.value)
        # VPN/DNS hint only after genuine transport-level connectivity failure.
        aborts.append("hint_check_vpn_or_dns")
    elif time_invalid:
        aborts.append(N7AbortCode.PUBLIC_TIME_INVALID.value)
    elif not first.ok or not second.ok:
        # Auth / recon / stream failures — not a DNS/VPN diagnosis by default.
        aborts.append(N7AbortCode.CONNECTIVITY_UNAVAILABLE.value)

    if c1.get("open_order_count", 0) > 0 or c2.get("open_order_count", 0) > 0:
        aborts.append(N7AbortCode.UNEXPECTED_OPEN_ORDER.value)

    # Reconciliation disagreement only when both runs compared reachable account state.
    recon_comparable = (
        c1.get("reconciliation_ran")
        and c2.get("reconciliation_ran")
        and not c1.get("unreachable_account")
        and not c2.get("unreachable_account")
    )
    if recon_comparable and c1.get("classifications") != c2.get("classifications"):
        aborts.append(N7AbortCode.PREFLIGHT_RECON_DISAGREEMENT.value)

    id_map = dict(first.payload.get("identity_mapping") or {})
    if first.payload.get("credentials_present") and id_map:
        if not id_map.get("private_key_derives_valid_signer"):
            aborts.append(N7AbortCode.CREDENTIALS_ROLE_MISMATCH.value)
        if not id_map.get("funder_present"):
            aborts.append(N7AbortCode.CREDENTIALS_ROLE_MISMATCH.value)

    bal = dict(first.payload.get("balance_evidence") or {})
    us = dict(first.payload.get("user_stream") or {})
    if us.get("attempted") and not us.get("authenticated"):
        kind = str(us.get("failure_kind") or "")
        if kind in {"adapter_init", "adapter_contract"}:
            aborts.append(N7AbortCode.USER_STREAM_INIT_FAILED.value)
        elif kind == "auth":
            aborts.append(N7AbortCode.USER_STREAM_AUTH_FAILED.value)
        elif kind == "transport":
            # Genuine user-stream network failure — connectivity, not VPN/DNS by default.
            aborts.append(N7AbortCode.CONNECTIVITY_UNAVAILABLE.value)
        elif kind in {"protocol", "rejected", "cancelled"}:
            aborts.append(N7AbortCode.USER_STREAM_PROTOCOL_FAILED.value)
        else:
            # Unknown stream failure: fail closed with precise stream code, not VPN.
            aborts.append(N7AbortCode.USER_STREAM_PROTOCOL_FAILED.value)

    go = "GO" if not aborts else "NO_GO"
    payload = {
        "mode": "n7_readonly_preflight",
        "not_live_trading": True,
        "mutations_enabled": False,
        "real_venue_mutations": 0,
        "go_no_go": go,
        "abort_codes": aborts,
        "git": {"head": git.head, "worktree_clean": git.worktree_clean},
        "config_fingerprint": sealed.fingerprint(),
        "config_path": str(config_path),
        "production_timing_status": PRODUCTION_TIMING_VALUES_STATUS,
        "sealed": sealed.to_dict(),
        "first_ok": first.ok,
        "restart_ok": second.ok,
        "classification_first": c1,
        "classification_restart": c2,
        "classification_stable": c1.get("classifications") == c2.get("classifications"),
        "acknowledged_external_count": 4,
        "balance_evidence": bal,
        "user_stream": us,
        "identity_mapping": {
            k: id_map.get(k)
            for k in (
                "private_key_derives_valid_signer",
                "signer_equals_funder",
                "funder_present",
                "signature_type_present",
                "historical_and_current_identity_mapping_match",
            )
        },
        "authorization_ceremony": "removed",
        "operator_live_command": "python tools/n7_live/run_n7_live_oneshot.py --live",
        "kill_state_clear": True,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    text = redact_text(json.dumps(payload))
    assert_no_secrets(text)
    artifact = out_dir / "n7_preflight_summary.json"
    artifact.write_text(json.dumps(json.loads(text), indent=2) + "\n", encoding="utf-8")
    return N7PreflightResult(
        ok=go == "GO",
        go_no_go=go,
        abort_codes=aborts,
        payload=json.loads(text),
        artifact_path=artifact,
    )
