"""R6C mutation-impossible live preflight composition root.

Binds only ``PreflightReadClient`` / ``ReadOnlyAccountTransport``.
Does not import mutation transport methods or LiveOMS submit/cancel paths.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.execution.order_store import OrderStore
from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.polymarket.auth import (
    CredentialError,
    assert_no_secrets,
    credentials_present,
    load_l2_credentials,
    redact_text,
)
from tyrex_pm.execution.polymarket.preflight_client import PreflightReadClient
from tyrex_pm.execution.polymarket.readiness import ExecutionReadiness, ReadinessReason
from tyrex_pm.execution.polymarket.reconciliation import ReconciliationService
from tyrex_pm.execution.polymarket.readonly_transport import (
    AUTH_CLOB_HEARTBEAT,
    EndpointProbeResult,
)
from tyrex_pm.portfolio.portfolio import Portfolio

# Env var *names* only — never values in artifacts.
CREDENTIAL_ENV_NAMES = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_PASSPHRASE",
    "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_FUNDER",
    "POLYMARKET_ADDRESS",
    "POLYMARKET_PK",
    "POLYMARKET_SIGNATURE_TYPE",
)


@dataclass
class LivePreflightResult:
    ok: bool
    artifact_path: Path
    payload: dict[str, Any] = field(default_factory=dict)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _env_presence() -> dict[str, bool]:
    return {name: bool(os.environ.get(name, "").strip()) for name in CREDENTIAL_ENV_NAMES}


def classify_cloudflare(status: int | str, body_snippet: str = "") -> str | None:
    """Classify Cloudflare / edge blocks without exposing body secrets."""
    text = body_snippet.lower()
    if "1010" in text or "error code: 1010" in text:
        return "1010"
    if status in {403, 503, 520, 521, 522, 523, 524} and (
        "cloudflare" in text or "attention required" in text
    ):
        return "cloudflare"
    if status == 403 and not text.strip().startswith("{"):
        return "403_non_json"
    return None


def run_live_preflight(
    *,
    output_path: Path,
    dotenv_path: Path | None = None,
    user_stream_observe_s: float = 0.0,
    skip_auth: bool = False,
) -> LivePreflightResult:
    """Run public then authenticated read-only preflight.

    ``user_stream_observe_s`` defaults to 0 (skip WS) so CI/agent runs stay offline-safe.
    Heartbeat endpoint is never called.
    """
    if dotenv_path is not None:
        _load_dotenv(dotenv_path)

    readiness = ExecutionReadiness()
    readiness.deny(ReadinessReason.MUTATIONS_DISABLED)

    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "environment": "local_preflight",
        "mutations_attempted": False,
        "heartbeat_called": False,
        "heartbeat_endpoint": {
            "host": AUTH_CLOB_HEARTBEAT[0],
            "path": AUTH_CLOB_HEARTBEAT[1],
            "method": AUTH_CLOB_HEARTBEAT[2],
            "status": "not_called_r6c_policy",
            "note": (
                "POST /heartbeats can arm a dead-man's switch that cancels all open "
                "orders if heartbeats stop (~10s + buffer). Unresolved for R6C."
            ),
        },
        "credential_env_present": _env_presence(),
        "credentials_present": credentials_present(),
        "probes": [],
        "reconciliation": None,
        "user_stream": {"attempted": False},
        "readiness": readiness.to_dict(),
        "readiness_reasons_expected_when_clean": [
            ReadinessReason.MUTATIONS_DISABLED.value,
            "R7_AUTHORIZATION_ABSENT",
        ],
    }

    client = PreflightReadClient()
    # Public first
    client.get_server_time()
    client.probe_public_book()

    public_ok = any(
        p.ok and p.category == "public_market_data" and p.path == "/time" for p in client.probes
    )
    if not public_ok:
        readiness.deny(ReadinessReason.TRANSPORT_DISCONNECTED)
        payload["probes"] = [p.to_dict() for p in client.probes]
        payload["readiness"] = readiness.to_dict()
        payload["blocker"] = "public_clob_unreachable"
        return _finalize(payload, output_path, client=None)

    if skip_auth or not credentials_present():
        readiness.deny(ReadinessReason.CREDENTIALS_MISSING)
        payload["probes"] = [p.to_dict() for p in client.probes]
        payload["readiness"] = readiness.to_dict()
        payload["blocker"] = "credentials_missing_or_skipped"
        return _finalize(payload, output_path, client=None)

    try:
        creds = load_l2_credentials()
    except CredentialError:
        readiness.deny(ReadinessReason.CREDENTIALS_MISSING)
        payload["probes"] = [p.to_dict() for p in client.probes]
        payload["readiness"] = readiness.to_dict()
        return _finalize(payload, output_path, client=None)

    client.creds = creds
    orders_store = OrderStore()
    ledger = FillLedger()
    portfolio = Portfolio(fill_ledger=ledger)
    recon = ReconciliationService(order_store=orders_store, portfolio=portfolio)

    missing_evidence = False
    venue_orders = []
    venue_trades = []
    venue_positions = []
    balance_ok = False

    try:
        venue_orders = client.get_open_orders()
    except RuntimeError:
        missing_evidence = True
        readiness.deny(ReadinessReason.TRANSPORT_DISCONNECTED)

    try:
        venue_trades = client.get_trades()
    except RuntimeError:
        missing_evidence = True

    try:
        bal = client.get_balance()
        balance_ok = True
        payload["balance_evidence"] = {
            "retrieved": True,
            "has_allowance_field": bal.allowance is not None,
            # Never exact balances in artifact
            "collateral_nonzero": bal.collateral_balance != 0,
        }
    except RuntimeError:
        missing_evidence = True
        readiness.deny(ReadinessReason.BALANCE_UNKNOWN)
        payload["balance_evidence"] = {"retrieved": False}

    venue_positions = client.get_positions()

    report = recon.reconcile(
        venue_orders=venue_orders,
        venue_trades=venue_trades,
        venue_positions=venue_positions,
        missing_evidence=missing_evidence,
    )
    payload["reconciliation"] = {
        "missing_evidence": missing_evidence,
        "counts": report.counts(),
        "blocks_entry": report.blocks_entry,
        "requires_manual": report.requires_manual,
        "open_order_count": len(venue_orders),
        "trade_count": len(venue_trades),
        "position_row_count": len(venue_positions),
        "nonzero_position_count": sum(1 for p in venue_positions if p.size != 0),
        "clean_empty_account": (
            not missing_evidence
            and not report.findings
            and len(venue_orders) == 0
            and sum(1 for p in venue_positions if p.size != 0) == 0
        ),
        "unreachable_account": missing_evidence,
    }

    if missing_evidence:
        readiness.deny(ReadinessReason.RECONCILIATION_FAILED)
    elif report.blocks_entry or report.requires_manual:
        readiness.deny(ReadinessReason.UNRESOLVED_MISMATCH)
        readiness.deny(ReadinessReason.RECONCILIATION_FAILED)
    else:
        readiness.clear(ReadinessReason.RECONCILIATION_PENDING)
        readiness.clear(ReadinessReason.RECONCILIATION_FAILED)
        readiness.clear(ReadinessReason.UNRESOLVED_MISMATCH)
        readiness.clear(ReadinessReason.TRANSPORT_DISCONNECTED)

    # Optional user-stream (bounded). Never creates orders.
    if user_stream_observe_s > 0:
        from tyrex_pm.execution.polymarket.user_stream_readonly import (
            observe_user_stream_readonly,
        )

        payload["user_stream"] = observe_user_stream_readonly(
            creds=creds,
            observe_s=user_stream_observe_s,
            on_disconnect=lambda: readiness.deny(ReadinessReason.USER_STREAM_UNREADY),
            on_ready=lambda: readiness.clear(ReadinessReason.USER_STREAM_UNREADY),
        )
        if not payload["user_stream"].get("authenticated"):
            readiness.deny(ReadinessReason.USER_STREAM_UNREADY)
    else:
        payload["user_stream"] = {
            "attempted": False,
            "note": "skipped; pass --user-stream-s on target host",
        }
        readiness.deny(ReadinessReason.USER_STREAM_UNREADY)

    payload["probes"] = [p.to_dict() for p in client.probes]
    payload["readiness"] = readiness.to_dict()
    payload["auth_app_validation_reached"] = any(
        p.app_auth_validation_reached for p in client.probes
    )
    payload["cloudflare_blocked"] = any(p.cloudflare_error for p in client.probes)
    payload["balance_ok"] = balance_ok
    # R7 auth never present in R6C
    payload["r7_authorization_present"] = False

    client.stop()
    return _finalize(payload, output_path, client=creds)


def _finalize(
    payload: dict[str, Any],
    output_path: Path,
    *,
    client: Any,
) -> LivePreflightResult:
    text = json.dumps(payload, indent=2) + "\n"
    if client is not None and hasattr(client, "api_key"):
        text = redact_text(text, client)
        assert_no_secrets(text, client)
    else:
        # Still scrub header-shaped fragments
        text = redact_text(text, None)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    # Hash for handoff integrity (no secrets in hash input after redaction)
    payload["artifact_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    ok = (
        not payload.get("cloudflare_blocked")
        and payload.get("reconciliation") is not None
        and not payload["reconciliation"].get("unreachable_account", True)
        and payload.get("credentials_present")
    )
    return LivePreflightResult(ok=ok, artifact_path=output_path, payload=payload)


def assert_preflight_cannot_mutate(client: PreflightReadClient) -> None:
    """Structural gate used by tests and composition checks."""
    for name in ("submit_order", "cancel_order", "post_heartbeat", "post_order"):
        if hasattr(client, name):
            raise AssertionError(f"preflight client exposes mutation method: {name}")
