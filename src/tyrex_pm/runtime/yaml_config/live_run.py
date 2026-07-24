"""YAML ``run --mode live`` orchestration → existing N7 operator session.

No second live engine: preflight, FakeTransport rehearsal, and live one-shot
all call ``tyrex_pm.runtime.n7_operator_run``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.auth import assert_no_secrets, redact_text
from tyrex_pm.runtime.n7_operator_run import (
    N7OperatorResult,
    run_fake_oneshot_rehearsal,
    run_fake_oneshot_no_signal,
    run_operator_oneshot,
)
from tyrex_pm.runtime.yaml_config.n7_bridge import (
    merge_yaml_into_operator_payload,
    write_effective_n7_sealed,
)
from tyrex_pm.runtime.yaml_config.resolve import ResolvedRunConfig


@dataclass
class YamlLiveRunResult:
    ok: bool
    outcome: str
    report_path: Path
    payload: dict[str, Any]
    real_venue_mutations: int


def _rewrite_report(path: Path, payload: dict[str, Any]) -> None:
    text = redact_text(json.dumps(payload, indent=2, default=str))
    assert_no_secrets(text)
    path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")


def run_yaml_live(
    *,
    resolved: ResolvedRunConfig,
    repo: Path,
    out_dir: Path | None,
    dotenv: Path | None,
    live: bool,
    fake_rehearsal: bool,
    fake_no_signal: bool = False,
    max_duration_s: float = 300.0,
) -> YamlLiveRunResult:
    """Resolve already validated; bind sealed config; invoke N7 lifecycle."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = resolved.run_name or f"yaml_live_{stamp}"
    out = out_dir or (repo / "var" / "reporting" / "yaml_run" / name)
    out.mkdir(parents=True, exist_ok=True)

    sealed_path = out / "effective_n7_sealed.json"
    write_effective_n7_sealed(resolved, sealed_path)

    if fake_rehearsal or fake_no_signal:
        if fake_no_signal:
            result = run_fake_oneshot_no_signal(out_dir=out, config_path=sealed_path)
        else:
            result = run_fake_oneshot_rehearsal(out_dir=out, config_path=sealed_path)
        payload = merge_yaml_into_operator_payload(result.payload, resolved=resolved)
        payload["requested_mode"] = "live"
        payload["effective_mode"] = "live"
        payload["live_armed"] = False
        payload["fake_rehearsal"] = True
        _rewrite_report(result.report_path, payload)
        return YamlLiveRunResult(
            ok=result.ok,
            outcome=result.outcome,
            report_path=result.report_path,
            payload=payload,
            real_venue_mutations=int(payload.get("real_venue_mutations") or 0),
        )

    result = asyncio.run(
        run_operator_oneshot(
            repo=repo,
            out_dir=out,
            config_path=sealed_path,
            dotenv=dotenv,
            live=bool(live),
            max_duration_s=float(max_duration_s),
            zgap_config=resolved.zgap,
            target_notional=resolved.risk.target_notional,
        )
    )
    payload = merge_yaml_into_operator_payload(result.payload, resolved=resolved)
    payload["requested_mode"] = "live"
    payload["effective_mode"] = "live"
    payload["live_armed"] = bool(live)
    payload["fake_rehearsal"] = False
    # Without --live the operator path is dry preflight only.
    if not live and payload.get("outcome") == "PREFLIGHT_OK_DRY":
        payload["mutations_disabled"] = True
    _rewrite_report(result.report_path, payload)
    return YamlLiveRunResult(
        ok=result.ok,
        outcome=result.outcome,
        report_path=result.report_path,
        payload=payload,
        real_venue_mutations=int(payload.get("real_venue_mutations") or 0),
    )
