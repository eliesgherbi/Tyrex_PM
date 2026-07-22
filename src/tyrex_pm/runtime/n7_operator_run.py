"""N7 operator one-shot orchestration (Scope A).

``--live`` on the CLI is the authorization. This module never auto-arms
mutations unless ``live=True`` is passed by the operator tool.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.auth import assert_no_secrets, redact_text
from tyrex_pm.runtime.n7_preflight import run_n7_preflight
from tyrex_pm.runtime.n7_ptb_policy import ptb_trust_fields
from tyrex_pm.runtime.n7_sealed import load_n7_sealed_config


@dataclass
class N7OperatorResult:
    ok: bool
    outcome: str
    report_path: Path
    payload: dict[str, Any]


def _write_report(out_dir: Path, payload: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    text = redact_text(json.dumps(payload, indent=2, default=str))
    assert_no_secrets(text)
    path = out_dir / "n7_oneshot_report.json"
    path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    return path


def run_fake_oneshot_rehearsal(*, out_dir: Path, config_path: Path) -> N7OperatorResult:
    """Deterministic FakeTransport lifecycle (no real venue)."""
    import sys

    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo / "tests"))
    from helpers_n7 import fill_order, make_enter, make_exit, make_n7_host, yes_book

    sealed = load_n7_sealed_config(config_path)
    host = make_n7_host(persistence_path=out_dir / "state.json", sealed=sealed)
    host.inner.preflight_reconcile()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    if entered.get("status") == "ACKNOWLEDGED":
        qty = entered["sizing"]["quantity"]
        fill_order(host, entered["order_id"], qty=qty, price="0.51")
        exited = host.try_exit(
            make_exit(host), book=book, limit_price=Decimal("0.49")
        )
        if exited.get("status") == "ACKNOWLEDGED":
            fill_order(
                host,
                exited["order_id"],
                qty=exited["exit_qty"],
                price="0.49",
                side="SELL",
            )
    econ = host.economics_report()
    host.terminate(reason="fake_rehearsal")
    payload = {
        "mode": "n7_fake_oneshot",
        "outcome": "PASS_FAKE_FLAT" if econ.get("flat") else "FAIL_FAKE",
        "live": False,
        "real_venue_mutations": host.inner.real_venue_mutations,
        "entry": entered,
        "economics": econ,
        "status": host.status(),
        "config_fingerprint": sealed.fingerprint(),
        "evals": 1 if entered.get("status") == "ACKNOWLEDGED" else 0,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(
        ptb_trust_fields(
            sealed_k="fake_host_fixture_k",
            require_ssr_price_match=sealed.require_ssr_price_match,
            ptb_ready=True,
        )
    )
    path = _write_report(out_dir, payload)
    return N7OperatorResult(
        ok=bool(econ.get("flat")) and host.inner.real_venue_mutations == 0,
        outcome=str(payload["outcome"]),
        report_path=path,
        payload=payload,
    )


async def run_operator_oneshot(
    *,
    repo: Path,
    out_dir: Path,
    config_path: Path,
    dotenv: Path | None,
    live: bool,
    max_duration_s: float = 300.0,
) -> N7OperatorResult:
    """Full operator path: preflight → (optional) live one-shot for one window."""
    sealed = load_n7_sealed_config(config_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Preflight is sync and may use asyncio.run (user stream). Always run it
    # off the operator event loop to avoid nested-loop failures.
    pf = await asyncio.to_thread(
        run_n7_preflight,
        out_dir=out_dir / "preflight",
        config_path=config_path,
        repo=repo,
        dotenv=dotenv,
        user_stream_observe_s=2.0,
        require_clean_worktree=False,
    )
    if not pf.ok:
        payload = {
            "mode": "n7_operator_oneshot",
            "outcome": "BLOCKED_PREFLIGHT",
            "live": live,
            "real_venue_mutations": 0,
            "preflight": pf.payload,
            "vpn_hint": "hint_check_vpn_or_dns" in pf.abort_codes,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        path = _write_report(out_dir, payload)
        return N7OperatorResult(False, "BLOCKED_PREFLIGHT", path, payload)

    if not live:
        # Read-only / dry: stop after preflight GO
        payload = {
            "mode": "n7_operator_oneshot",
            "outcome": "PREFLIGHT_OK_DRY",
            "live": False,
            "real_venue_mutations": 0,
            "preflight": pf.payload,
            "note": "Pass --live to enable the bounded mutation path for one window.",
            "config_fingerprint": sealed.fingerprint(),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        path = _write_report(out_dir, payload)
        return N7OperatorResult(True, "PREFLIGHT_OK_DRY", path, payload)

    # --- LIVE path (operator only) ---
    try:
        from tyrex_pm.runtime.n7_live_session import run_live_oneshot_session

        session = await run_live_oneshot_session(
            repo=repo,
            out_dir=out_dir,
            sealed=sealed,
            dotenv=dotenv,
            max_duration_s=max_duration_s,
            preflight=pf.payload,
        )
        path = _write_report(out_dir, session)
        return N7OperatorResult(
            ok=session.get("outcome", "").startswith("PASS"),
            outcome=str(session.get("outcome")),
            report_path=path,
            payload=session,
        )
    except Exception as exc:  # noqa: BLE001
        payload = {
            "mode": "n7_operator_oneshot",
            "outcome": "FAIL_SAFETY",
            "live": True,
            "error": type(exc).__name__,
            "detail": str(exc),
            "traceback": traceback.format_exc()[-4000:],
            "real_venue_mutations": 0,
            "preflight": pf.payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        path = _write_report(out_dir, payload)
        return N7OperatorResult(False, "FAIL_SAFETY", path, payload)
