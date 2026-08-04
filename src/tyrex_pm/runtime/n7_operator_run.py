"""N7 operator one-shot orchestration (Scope A).

``--live`` on the CLI is the authorization. This module never auto-arms
mutations unless ``live=True`` is passed by the operator tool.

Primary evidence is the common reporter under ``var/runs/<strategy>/<run_id>/``
(``manifest.json``, ``run_summary.json``, audit/analytics JSONL). The legacy
``n7_oneshot_report.json`` primary is removed.
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
from tyrex_pm.reporting import open_run_reporter
from tyrex_pm.reporting.config import (
    ReportingConfig,
    default_reporting_config,
    load_reporting_config,
)
from tyrex_pm.reporting.reporter import RunReporter
from tyrex_pm.reporting.writer import atomic_write_json
from tyrex_pm.runtime.n7_preflight import run_n7_preflight
from tyrex_pm.runtime.n7_ptb_policy import ptb_trust_fields
from tyrex_pm.runtime.n7_sealed import load_n7_sealed_config


@dataclass
class N7OperatorResult:
    ok: bool
    outcome: str
    report_path: Path
    payload: dict[str, Any]
    run_dir: Path | None = None


def _open_n7_reporter(
    *,
    out_dir: Path,
    run_id: str,
    reporting_config: ReportingConfig | None = None,
    fake_transport: bool = False,
) -> RunReporter:
    cfg = reporting_config or default_reporting_config()
    out_dir.mkdir(parents=True, exist_ok=True)
    return open_run_reporter(
        run_dir=out_dir,
        run_id=run_id,
        mode="live",
        strategy_id="z_gap",
        strategy_version="n7",
        config=cfg,
        performance_label="real",
        fake_transport=fake_transport,
        identity_extra={
            "host": "n7_operator_oneshot",
            "fake_transport": fake_transport,
        },
        configuration={"n7": True},
    )


def _finalize_payload(reporter: RunReporter, payload: dict[str, Any]) -> Path:
    """Persist operator outcome into summary/audit; return run_summary path."""
    text = redact_text(json.dumps(payload, indent=2, default=str))
    assert_no_secrets(text)
    attach_dir = reporter.run_dir / "attachments"
    attach_dir.mkdir(parents=True, exist_ok=True)
    outcome_path = attach_dir / "operator_outcome.json"
    atomic_write_json(outcome_path, json.loads(text))
    reporter.add_attachment(
        name="operator_outcome",
        relative_path="attachments/operator_outcome.json",
    )
    reporter.emit_dict(
        event_family="lifecycle",
        event_type="n7.operator_outcome",
        payload={
            "outcome": payload.get("outcome"),
            "live": payload.get("live"),
            "real_venue_mutations": payload.get("real_venue_mutations"),
            "reason": payload.get("reason"),
            "fake_transport": bool(payload.get("mode") == "n7_fake_oneshot"),
        },
        producer="n7_operator_run",
        force_critical=True,
    )
    reporter.configuration["operator_outcome"] = {
        "outcome": payload.get("outcome"),
        "reason": payload.get("reason"),
        "real_venue_mutations": payload.get("real_venue_mutations"),
    }
    terminal = "COMPLETE" if str(payload.get("outcome", "")).startswith("PASS") else "PARTIAL"
    if str(payload.get("outcome", "")).startswith(("FAIL", "BLOCKED", "ABORTED")):
        terminal = "ABORTED"
    return reporter.finalize(
        terminal_status=terminal,
        terminal_reason=str(payload.get("outcome") or "n7_complete"),
        clean_shutdown=terminal == "COMPLETE",
    )


def run_fake_oneshot_rehearsal(
    *,
    out_dir: Path,
    config_path: Path,
    reporting_config: ReportingConfig | None = None,
) -> N7OperatorResult:
    """Deterministic FakeTransport lifecycle (no real venue)."""
    import sys

    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo / "tests"))
    from helpers_n7 import fill_order, make_enter, make_exit, make_n7_host, yes_book

    sealed = load_n7_sealed_config(config_path)
    run_id = datetime.now(timezone.utc).strftime("n7_fake_%Y%m%dT%H%M%SZ")
    reporter = _open_n7_reporter(
        out_dir=out_dir,
        run_id=run_id,
        reporting_config=reporting_config,
        fake_transport=True,
    )
    host = make_n7_host(persistence_path=out_dir / "runtime_state.json", sealed=sealed)
    host.attach_reporter(reporter)
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
        "mutations_disabled": True,
        "performance_label": "real",
        "fake_transport": True,
        "transport": "FakeTransport",
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(out_dir),
    }
    payload.update(
        ptb_trust_fields(
            sealed_k="fake_host_fixture_k",
            require_ssr_price_match=sealed.require_ssr_price_match,
            ptb_ready=True,
        )
    )
    path = _finalize_payload(reporter, payload)
    return N7OperatorResult(
        ok=bool(econ.get("flat")) and host.inner.real_venue_mutations == 0,
        outcome=str(payload["outcome"]),
        report_path=path,
        payload=payload,
        run_dir=out_dir,
    )


def run_fake_oneshot_no_signal(
    *,
    out_dir: Path,
    config_path: Path,
    reporting_config: ReportingConfig | None = None,
) -> N7OperatorResult:
    """Fake rehearsal with zero EnterIntent / zero mutations."""
    import sys

    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo / "tests"))
    from helpers_n7 import make_n7_host

    sealed = load_n7_sealed_config(config_path)
    run_id = datetime.now(timezone.utc).strftime("n7_fake_ns_%Y%m%dT%H%M%SZ")
    reporter = _open_n7_reporter(
        out_dir=out_dir,
        run_id=run_id,
        reporting_config=reporting_config,
        fake_transport=True,
    )
    host = make_n7_host(persistence_path=out_dir / "runtime_state.json", sealed=sealed)
    host.attach_reporter(reporter)
    host.inner.preflight_reconcile()
    econ = host.economics_report()
    host.terminate(reason="fake_no_signal")
    payload = {
        "mode": "n7_fake_oneshot",
        "outcome": "PASS_N7_SAFE_NO_ENTRY",
        "live": False,
        "reason": "evaluated_no_enter_signal",
        "real_venue_mutations": host.inner.real_venue_mutations,
        "entry": None,
        "economics": econ,
        "status": host.status(),
        "config_fingerprint": sealed.fingerprint(),
        "evals": 1,
        "mutations_disabled": True,
        "performance_label": "real",
        "fake_transport": True,
        "transport": "FakeTransport",
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(out_dir),
    }
    payload.update(
        ptb_trust_fields(
            sealed_k="fake_host_fixture_k",
            require_ssr_price_match=sealed.require_ssr_price_match,
            ptb_ready=True,
        )
    )
    path = _finalize_payload(reporter, payload)
    return N7OperatorResult(
        ok=host.inner.real_venue_mutations == 0,
        outcome=str(payload["outcome"]),
        report_path=path,
        payload=payload,
        run_dir=out_dir,
    )


async def run_operator_oneshot(
    *,
    repo: Path,
    out_dir: Path,
    config_path: Path,
    dotenv: Path | None,
    live: bool,
    max_duration_s: float = 300.0,
    zgap_config: Any | None = None,
    target_notional: Decimal | None = None,
    reporting_config: ReportingConfig | None = None,
    reporting_config_path: Path | None = None,
) -> N7OperatorResult:
    """Full operator path: preflight → (optional) live one-shot for one window."""
    sealed = load_n7_sealed_config(config_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("n7_%Y%m%dT%H%M%SZ")
    cfg = reporting_config
    if cfg is None and reporting_config_path is not None:
        cfg = load_reporting_config(reporting_config_path)
    reporter = _open_n7_reporter(
        out_dir=out_dir,
        run_id=run_id,
        reporting_config=cfg,
        fake_transport=False,
    )

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
            "run_dir": str(out_dir),
        }
        path = _finalize_payload(reporter, payload)
        return N7OperatorResult(False, "BLOCKED_PREFLIGHT", path, payload, out_dir)

    if not live:
        payload = {
            "mode": "n7_operator_oneshot",
            "outcome": "PREFLIGHT_OK_DRY",
            "live": False,
            "real_venue_mutations": 0,
            "preflight": pf.payload,
            "note": "Pass --live to enable the bounded mutation path for one window.",
            "config_fingerprint": sealed.fingerprint(),
            "mutations_disabled": True,
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_dir": str(out_dir),
        }
        path = _finalize_payload(reporter, payload)
        return N7OperatorResult(True, "PREFLIGHT_OK_DRY", path, payload, out_dir)

    try:
        from tyrex_pm.runtime.n7_live_session import run_live_oneshot_session

        session = await run_live_oneshot_session(
            repo=repo,
            out_dir=out_dir,
            sealed=sealed,
            dotenv=dotenv,
            max_duration_s=max_duration_s,
            preflight=pf.payload,
            zgap_config=zgap_config,
            target_notional=target_notional,
            reporter=reporter,
        )
        session["run_dir"] = str(out_dir)
        path = _finalize_payload(reporter, session)
        return N7OperatorResult(
            ok=session.get("outcome", "").startswith("PASS"),
            outcome=str(session.get("outcome")),
            report_path=path,
            payload=session,
            run_dir=out_dir,
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
            "run_dir": str(out_dir),
        }
        path = _finalize_payload(reporter, payload)
        return N7OperatorResult(False, "FAIL_SAFETY", path, payload, out_dir)
