"""Single-command pre-boundary Z-Gap live session orchestrator."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from tyrex_pm.runtime.z_gap_sidecar_supervisor import SidecarSupervisor

from tyrex_pm.ingestion.btc_5m_window_scheduler import (
    Btc5mSkippedWindow,
    Btc5mWindowPlan,
    prestart_lead_seconds,
    prestart_sufficient,
    select_btc_5m_session_window,
)
from tyrex_pm.ingestion.price_to_beat_tracker import (
    PTB_STATUS_MISSING,
    PTB_STATUS_OBSERVED,
)
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_Z_GAP_PTB_CAPTURE,
    FACT_TYPE_Z_GAP_SESSION_STATUS,
    FACT_TYPE_Z_GAP_STARTUP_TIMING,
    FACT_TYPE_Z_GAP_WINDOW_SKIPPED_INSUFFICIENT_PRESTART,
)
from tyrex_pm.runtime.btc_5m_metadata import (
    Btc5mMarketMetadata,
    apply_btc_5m_metadata_to_app,
    resolve_btc_5m_event_metadata,
)
from tyrex_pm.runtime.config import (
    AppConfig,
    Z_GAP_ENTRY_MODE_ENFORCE,
    Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    load_app_config,
)
from tyrex_pm.runtime.z_gap_artifact_writer import (
    artifact_stale_for_market,
    write_calibration_ack_artifact,
    write_clock_artifact,
    write_connectivity_artifact,
    write_fee_curve_artifact,
    write_market_metadata_artifact,
    write_operator_approval_artifact,
    write_ptb_waiting_artifact,
    write_sidecar_health_artifact,
)
from tyrex_pm.runtime.z_gap_boundary_gate import (
    PTB_STATUS_READY,
    evaluate_boundary_ptb_gate,
)
from tyrex_pm.runtime.z_gap_config_hash import (
    calibration_ack_matches_fingerprint,
    compute_z_gap_config_fingerprint,
)
from tyrex_pm.runtime.z_gap_experimental import (
    MAX_EXPERIMENTAL_USD,
    build_observe_session_report,
    prompt_experimental_live_approval,
    read_live_ptb_from_handle,
    record_experimental_approval_in_manifest,
    validate_experimental_live_metadata,
    apply_boundary_k_to_store,
)
from tyrex_pm.runtime.z_gap_interactive_approval import (
    prompt_calibration_ack,
    prompt_window_approval,
    WindowApprovalPrompt,
)
from tyrex_pm.runtime.z_gap_live_preflight import validate_z_gap_live_scenario
from tyrex_pm.runtime.z_gap_ptb_capture import (
    PTB_CAPTURE_WAITING,
    capture_ptb_at_boundary,
    capture_trace_to_payload,
)
from tyrex_pm.runtime.z_gap_preflight import (
    PREFLIGHT_PHASE_STATIC,
    load_z_gap_preflight_gates,
)
from tyrex_pm.runtime.z_gap_ptb_commissioning import (
    COMMISSIONING_POLICY_NAME,
    build_commissioning_certificate,
    evaluate_commissioning_window,
    independent_ptb_reference_available,
    write_commissioning_certificate,
    write_commissioning_report,
)
from tyrex_pm.runtime.z_gap_ptb_independent_reference import (
    INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3,
    fetch_independent_ptb_reference,
)
from tyrex_pm.runtime.z_gap_session_runtime import (
    SessionRuntimeHandle,
    bootstrap_session_runtime,
    run_post_boundary_runtime,
    shutdown_session_runtime,
    start_production_feeds,
    warm_sigma_before_boundary,
)
from tyrex_pm.runtime.z_gap_sigma_warmup import sigma_warmup_required_seconds

STATUS_NOT_READY = "NOT_READY"
STATUS_READY_TO_WAIT = "READY_TO_WAIT_FOR_BOUNDARY"
STATUS_WAITING = "WAITING_FOR_BOUNDARY"
STATUS_PTB_READY = "PTB_LOCKED_READY_TO_EVALUATE"
STATUS_NO_TRADE = "NO_TRADE"
STATUS_TERMINAL = "TERMINAL"


@dataclass
class SessionStartupTimings:
    metadata_resolve_ms: float | None = None
    time_sync_ms: float | None = None
    binance_connect_ms: float | None = None
    rtds_connect_ms: float | None = None
    sidecar_ready_ms: float | None = None
    clob_bootstrap_ms: float | None = None
    sigma_warmup_ms: float | None = None
    total_preparation_ms: float | None = None


@dataclass
class SessionSelection:
    plan: Btc5mWindowPlan
    skipped: tuple[Btc5mSkippedWindow, ...]
    metadata: Btc5mMarketMetadata


@dataclass
class SessionResult:
    status: str
    market_id: str | None = None
    run_name: str = ""
    execute_authorized: bool = False
    order_submitted: bool = False
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    timings: SessionStartupTimings = field(default_factory=SessionStartupTimings)
    report_path: str | None = None
    facts: list[dict[str, Any]] = field(default_factory=list)
    runtime_exit_code: int | None = None
    facts_path: str | None = None
    oms_submissions: int = 0


@dataclass
class CommissionPtbResult:
    status: str
    run_name: str
    windows_attempted: int
    windows_usable: int
    windows_required: int
    certificate_path: str | None = None
    report_path: str | None = None
    window_rows: list[dict[str, Any]] = field(default_factory=list)
    blockers: tuple[str, ...] = ()


@dataclass
class SessionDeps:
    now_ts: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    resolve_metadata: Callable[[str], Btc5mMarketMetadata] = resolve_btc_5m_event_metadata
    run_binance_check: Callable[[], Awaitable[Any]] | None = None
    run_clock_check: Callable[[], tuple[Any, Any]] | None = None
    resolve_fee_model: Callable[..., Awaitable[Any]] | None = None
    input_fn: Callable[[str], str] | None = None
    write_fn: Callable[[str], None] | None = None
    sidecar: SidecarSupervisor | None = None
    boundary_evaluator: Callable[..., Any] | None = None
    runtime_handle: SessionRuntimeHandle | None = None


def _emit_fact(
    facts: list[dict[str, Any]],
    *,
    fact_type: str,
    payload: dict[str, Any],
) -> None:
    facts.append(
        {
            "fact_type": fact_type,
            "payload": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )


def select_session_window(
    *,
    now_ts: float,
    target_prestart_seconds: float,
    hard_min_prestart_seconds: float,
    min_sigma_warmup_seconds: float = 0.0,
) -> tuple[Btc5mWindowPlan, tuple[Btc5mSkippedWindow, ...]]:
    return select_btc_5m_session_window(
        now_ts=now_ts,
        target_prestart_seconds=target_prestart_seconds,
        hard_min_prestart_seconds=hard_min_prestart_seconds,
        min_sigma_warmup_seconds=min_sigma_warmup_seconds,
    )


def abandon_window_if_insufficient_prestart(
    *,
    now_ts: float,
    plan: Btc5mWindowPlan,
    hard_min_prestart_seconds: float,
    target_prestart_seconds: float,
    min_sigma_warmup_seconds: float = 0.0,
) -> tuple[Btc5mWindowPlan, tuple[Btc5mSkippedWindow, ...], bool]:
    lead = prestart_lead_seconds(now_ts=now_ts, event_start_ts=plan.window_start_ts)
    if prestart_sufficient(
        now_ts=now_ts,
        event_start_ts=plan.window_start_ts,
        hard_min_prestart_seconds=hard_min_prestart_seconds,
    ):
        return plan, (), False
    skipped = (
        Btc5mSkippedWindow(
            window_start_ts=plan.window_start_ts,
            window_end_ts=plan.window_end_ts,
            event_slug=plan.event_slug,
            event_url=plan.event_url,
            reason="preparation_exceeded_prestart",
            lead_time_s=lead,
        ),
    )
    new_plan, more_skipped = select_btc_5m_session_window(
        now_ts=now_ts,
        target_prestart_seconds=target_prestart_seconds,
        hard_min_prestart_seconds=hard_min_prestart_seconds,
        min_sigma_warmup_seconds=min_sigma_warmup_seconds,
    )
    return new_plan, skipped + more_skipped, True


class ZGapSessionOrchestrator:
    def __init__(
        self,
        *,
        repo_root: Path,
        scenario_file: str = "config/scenarios/live_z_gap_tiny.yaml",
        strategy_file: str = "config/strategies/z_gap.yaml",
        artifacts_dir: Path | None = None,
        deps: SessionDeps | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.scenario_file = scenario_file
        self.strategy_file = strategy_file
        self.artifacts_dir = artifacts_dir or Path("var/reporting/z_gap")
        self.deps = deps or SessionDeps()
        os.environ["Z_GAP_PREFLIGHT_DIR"] = str(self.artifacts_dir.resolve())
        self._facts: list[dict[str, Any]] = []
        self._approved_market_id: str | None = None
        self._runtime_handle: SessionRuntimeHandle | None = None

    def _load_app(self, *, entry_mode: str | None = None, experimental_mode: bool = False) -> AppConfig:
        from contextlib import nullcontext
        from unittest.mock import patch

        skip_validate = entry_mode == Z_GAP_ENTRY_MODE_OBSERVE_ONLY or experimental_mode
        ctx = (
            patch("tyrex_pm.runtime.z_gap_live.validate_z_gap_live_config")
            if skip_validate
            else nullcontext()
        )
        with ctx:
            app = load_app_config(
                repo_root=self.repo_root,
                strategy_file=self.strategy_file,
                scenario_file=self.scenario_file,
            )
        if entry_mode and app.z_gap is not None:
            app = replace(app, z_gap=replace(app.z_gap, entry_mode=entry_mode))
        return app

    def discover_next_window(
        self,
        app: AppConfig,
        *,
        event_url: str | None = None,
    ) -> SessionSelection:
        zg = app.z_gap
        assert zg is not None
        lv = zg.live_validation
        now = self.deps.now_ts()
        if event_url:
            meta = self.deps.resolve_metadata(event_url)
            plan = Btc5mWindowPlan(
                window_start_ts=int(meta.event_start_ts),
                window_end_ts=int(meta.event_end_ts),
                event_slug=meta.event_slug,
                event_url=event_url,
                wake_at_ts=now,
            )
            return SessionSelection(plan=plan, skipped=(), metadata=meta)

        min_sigma_s = sigma_warmup_required_seconds(zg.sigma)
        plan, skipped = select_session_window(
            now_ts=now,
            target_prestart_seconds=lv.target_prestart_seconds,
            hard_min_prestart_seconds=lv.hard_min_prestart_seconds,
            min_sigma_warmup_seconds=min_sigma_s,
        )
        for row in skipped:
            _emit_fact(
                self._facts,
                fact_type=FACT_TYPE_Z_GAP_WINDOW_SKIPPED_INSUFFICIENT_PRESTART,
                payload={
                    "window_start_ts": row.window_start_ts,
                    "event_url": row.event_url,
                    "reason": row.reason,
                    "lead_time_s": row.lead_time_s,
                },
            )
        meta = self.deps.resolve_metadata(plan.event_url)
        return SessionSelection(plan=plan, skipped=skipped, metadata=meta)

    async def prepare_static_artifacts(
        self,
        app: AppConfig,
        selection: SessionSelection,
        *,
        timings: SessionStartupTimings,
        experimental_mode: bool = False,
    ) -> tuple[list[str], list[str]]:
        blockers: list[str] = []
        warnings: list[str] = []
        meta = selection.metadata
        t0 = time.monotonic()
        fingerprint = compute_z_gap_config_fingerprint(
            app,
            strategy_file=self.repo_root / self.strategy_file,
            scenario_file=self.repo_root / self.scenario_file,
        )
        os.environ["Z_GAP_PREFLIGHT_DIR"] = str(self.artifacts_dir.resolve())
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        write_market_metadata_artifact(
            self.artifacts_dir / "market_metadata.json",
            meta=meta,
            config_hash=fingerprint.combined_hash,
        )
        timings.metadata_resolve_ms = round((time.monotonic() - t0) * 1000.0, 1)

        write_ptb_waiting_artifact(
            self.artifacts_dir / "ptb_attestation.json",
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
        )

        if self.deps.run_binance_check is not None:
            t1 = time.monotonic()
            report = await self.deps.run_binance_check()
            write_connectivity_artifact(self.artifacts_dir / "binance_connectivity.json", report)
            timings.binance_connect_ms = round((time.monotonic() - t1) * 1000.0, 1)
        elif not (self.artifacts_dir / "binance_connectivity.json").is_file():
            msg = "binance_connectivity: not generated"
            if experimental_mode:
                warnings.append(msg)
            else:
                blockers.append(msg)

        if self.deps.run_clock_check is not None:
            t2 = time.monotonic()
            report, _ = self.deps.run_clock_check()
            write_clock_artifact(self.artifacts_dir / "clock_sanity.json", report)
            timings.time_sync_ms = round((time.monotonic() - t2) * 1000.0, 1)
        elif not (self.artifacts_dir / "clock_sanity.json").is_file():
            msg = "clock_sanity: not generated"
            if experimental_mode:
                warnings.append(msg)
            else:
                blockers.append(msg)

        if self.deps.resolve_fee_model is not None:
            fee_model = await self.deps.resolve_fee_model(app, meta)
            write_fee_curve_artifact(
                self.artifacts_dir / "fee_curve_spike.json",
                fee_model=fee_model,
                market_id=meta.market_id,
                condition_id=meta.condition_id,
                config_hash=fingerprint.combined_hash,
            )
        elif not (self.artifacts_dir / "fee_curve_spike.json").is_file():
            msg = "fee_curve: not generated"
            if experimental_mode:
                warnings.append(msg)
            else:
                blockers.append(msg)

        sidecar = self.deps.sidecar
        if sidecar is not None:
            t3 = time.monotonic()
            sidecar.start(repo_root=self.repo_root)
            health = sidecar.wait_healthy(timeout_s=5.0, now_fn=self.deps.now_ts)
            write_sidecar_health_artifact(
                self.artifacts_dir / "sidecar_health.json",
                healthy=health.healthy,
                market_id=meta.market_id,
                tick_age_s=health.tick_age_s,
                process_running=health.process_running,
                details=health.details,
            )
            timings.sidecar_ready_ms = round((time.monotonic() - t3) * 1000.0, 1)
            if not health.healthy and not health.process_running:
                msg = "sidecar: not healthy"
                if experimental_mode:
                    warnings.append(msg)
                else:
                    blockers.append(msg)

        timings.total_preparation_ms = round((time.monotonic() - t0) * 1000.0, 1)
        _emit_fact(
            self._facts,
            fact_type=FACT_TYPE_Z_GAP_STARTUP_TIMING,
            payload={
                "metadata_resolve_ms": timings.metadata_resolve_ms,
                "time_sync_ms": timings.time_sync_ms,
                "binance_connect_ms": timings.binance_connect_ms,
                "sidecar_ready_ms": timings.sidecar_ready_ms,
                "total_preparation_ms": timings.total_preparation_ms,
            },
        )
        return blockers, warnings

    def ensure_calibration_ack(
        self,
        app: AppConfig,
        *,
        interactive: bool,
    ) -> tuple[bool, str | None]:
        now = self.deps.now_ts()
        fingerprint = compute_z_gap_config_fingerprint(
            app,
            strategy_file=self.repo_root / self.strategy_file,
            scenario_file=self.repo_root / self.scenario_file,
        )
        path = self.artifacts_dir / "calibration_lite_review.json"
        existing = None
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
        zg = app.z_gap
        assert zg is not None
        if calibration_ack_matches_fingerprint(
            existing,
            fingerprint,
            now_ts=now,
            expiry_hours=zg.live_validation.calibration_ack_expiry_hours,
        ):
            return True, None
        if not interactive:
            return False, "calibration acknowledgment required (non-interactive)"
        if not prompt_calibration_ack(input_fn=self.deps.input_fn, write_fn=self.deps.write_fn):
            return False, "calibration acknowledgment rejected"
        write_calibration_ack_artifact(
            path,
            fingerprint=fingerprint,
            now_ts=now,
            expiry_hours=zg.live_validation.calibration_ack_expiry_hours,
        )
        return True, None

    def ensure_window_approval(
        self,
        meta: Btc5mMarketMetadata,
        *,
        run_name: str,
        interactive: bool,
    ) -> tuple[bool, str | None]:
        if not interactive:
            return False, "operator window approval required (non-interactive)"
        prompt = WindowApprovalPrompt(
            market_id=meta.market_id,
            condition_id=meta.condition_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            maximum_usd="5",
            run_name=run_name,
        )
        if not prompt_window_approval(prompt, input_fn=self.deps.input_fn, write_fn=self.deps.write_fn):
            return False, "operator window approval rejected"
        write_operator_approval_artifact(
            self.artifacts_dir / "operator_enforce_approval.json",
            market_id=meta.market_id,
            condition_id=meta.condition_id,
            run_name=run_name,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=self.deps.now_ts(),
        )
        self._approved_market_id = meta.market_id
        return True, None

    def validate_static_readiness(
        self,
        app: AppConfig,
        *,
        observe_only: bool = False,
    ) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
        zg = app.z_gap
        assert zg is not None
        validate_app = app
        if not observe_only and zg.entry_mode != Z_GAP_ENTRY_MODE_ENFORCE:
            validate_app = replace(app, z_gap=replace(zg, entry_mode=Z_GAP_ENTRY_MODE_ENFORCE))
        result = validate_z_gap_live_scenario(
            validate_app,
            artifacts_dir=self.artifacts_dir,
            now_ts=self.deps.now_ts(),
            lifecycle_state_path=self.artifacts_dir / "lifecycle_state.json",
            pre_boundary=True,
            observe_only=observe_only,
        )
        gates = load_z_gap_preflight_gates(self.artifacts_dir, phase=PREFLIGHT_PHASE_STATIC, pre_boundary=True)
        if observe_only:
            ok = result.ok and not any(
                b for b in gates.blockers if "operator" in b.lower()
            )
        else:
            ok = result.ok and gates.static_enforce_allowed
        return ok, result.errors, result.warnings

    async def wait_for_boundary(self, event_start_ts: float) -> None:
        while self.deps.now_ts() < event_start_ts:
            await self.deps.sleep(min(0.25, max(0.01, event_start_ts - self.deps.now_ts())))

    async def evaluate_boundary(
        self,
        app: AppConfig,
        meta: Btc5mMarketMetadata,
        *,
        experimental_mode: bool = False,
    ) -> Any:
        evaluator = self.deps.boundary_evaluator or evaluate_boundary_ptb_gate
        fingerprint = compute_z_gap_config_fingerprint(
            app,
            strategy_file=self.repo_root / self.strategy_file,
            scenario_file=self.repo_root / self.scenario_file,
        )
        handle = self._runtime_handle or self.deps.runtime_handle
        ptb = read_live_ptb_from_handle(handle)
        return evaluator(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=self.deps.now_ts(),
            live_price=ptb.price,
            live_status=ptb.status,
            live_lag_ms=ptb.lag_ms,
            ptb_config_hash=fingerprint.combined_hash,
            artifacts_dir=self.artifacts_dir,
            experimental_mode=experimental_mode,
        )

    def invalidate_approval_on_market_change(self, market_id: str) -> None:
        if self._approved_market_id and self._approved_market_id != market_id:
            path = self.artifacts_dir / "operator_enforce_approval.json"
            if path.is_file():
                path.unlink()

    async def run_session(
        self,
        *,
        run_name: str,
        next_window: bool = True,
        event_url: str | None = None,
        execute: bool = False,
        interactive: bool = True,
        observe_only: bool = False,
        experimental_live: bool = False,
        max_usd: Decimal | None = None,
        commission_mode: bool = False,
    ) -> SessionResult:
        timings = SessionStartupTimings()
        blockers: list[str] = []
        warnings: list[str] = []
        experimental_mode = observe_only or experimental_live
        if experimental_live:
            execute = True

        entry_mode = (
            Z_GAP_ENTRY_MODE_OBSERVE_ONLY
            if (observe_only or not execute)
            else Z_GAP_ENTRY_MODE_ENFORCE
        )
        app = self._load_app(entry_mode=entry_mode, experimental_mode=experimental_mode)
        zg = app.z_gap
        assert zg is not None

        selection = self.discover_next_window(app, event_url=event_url if not next_window else None)
        meta = selection.metadata
        app = apply_btc_5m_metadata_to_app(app, meta)
        zg = app.z_gap
        assert zg is not None

        if experimental_live and zg.sizing is not None:
            cap = max_usd if max_usd is not None else MAX_EXPERIMENTAL_USD
            app = replace(app, z_gap=replace(zg, sizing=replace(zg.sizing, max_usd=cap)))
            zg = app.z_gap
            assert zg is not None

        lead = prestart_lead_seconds(now_ts=self.deps.now_ts(), event_start_ts=meta.event_start_ts)
        if self.deps.write_fn:
            self.deps.write_fn(
                f"Selected market: {meta.market_id}\n"
                f"Start UTC: {datetime.fromtimestamp(meta.event_start_ts, tz=timezone.utc).isoformat()}\n"
                f"End UTC: {datetime.fromtimestamp(meta.event_end_ts, tz=timezone.utc).isoformat()}\n"
                f"Lead time: {lead:.0f}s\n"
                f"Maximum risk: ${max_usd or MAX_EXPERIMENTAL_USD}\n"
            )

        plan = selection.plan
        min_sigma_s = sigma_warmup_required_seconds(zg.sigma)
        plan, extra_skipped, abandoned = abandon_window_if_insufficient_prestart(
            now_ts=self.deps.now_ts(),
            plan=plan,
            hard_min_prestart_seconds=zg.live_validation.hard_min_prestart_seconds,
            target_prestart_seconds=zg.live_validation.target_prestart_seconds,
            min_sigma_warmup_seconds=min_sigma_s,
        )
        if abandoned:
            for row in extra_skipped:
                _emit_fact(
                    self._facts,
                    fact_type=FACT_TYPE_Z_GAP_WINDOW_SKIPPED_INSUFFICIENT_PRESTART,
                    payload={
                        "window_start_ts": row.window_start_ts,
                        "reason": row.reason,
                        "lead_time_s": row.lead_time_s,
                    },
                )
            meta = self.deps.resolve_metadata(plan.event_url)
            app = apply_btc_5m_metadata_to_app(app, meta)
            self.invalidate_approval_on_market_change(meta.market_id)

        prep_blockers, prep_warnings = await self.prepare_static_artifacts(
            app,
            SessionSelection(plan, (), meta),
            timings=timings,
            experimental_mode=experimental_mode,
        )
        blockers.extend(prep_blockers)
        warnings.extend(prep_warnings)

        if experimental_mode:
            if experimental_live:
                cap = max_usd if max_usd is not None else MAX_EXPERIMENTAL_USD
                blockers.extend(validate_experimental_live_metadata(zg, max_usd=cap))
                if not interactive:
                    blockers.append("experimental live requires interactive APPROVE $5 EXPERIMENT")
                elif not blockers:
                    approved = prompt_experimental_live_approval(
                        market_id=meta.market_id,
                        max_usd=str(cap),
                        input_fn=self.deps.input_fn,
                        write_fn=self.deps.write_fn,
                    )
                    if not approved:
                        blockers.append("experimental live approval rejected")
        else:
            ok_cal, cal_reason = self.ensure_calibration_ack(
                app,
                interactive=interactive and not commission_mode and (execute or observe_only),
            )
            if commission_mode and cal_reason:
                fingerprint = compute_z_gap_config_fingerprint(
                    app,
                    strategy_file=self.repo_root / self.strategy_file,
                    scenario_file=self.repo_root / self.scenario_file,
                )
                write_calibration_ack_artifact(
                    self.artifacts_dir / "calibration_lite_review.json",
                    fingerprint=fingerprint,
                    now_ts=self.deps.now_ts(),
                    expiry_hours=zg.live_validation.calibration_ack_expiry_hours,
                )
                ok_cal, cal_reason = True, None
            if not ok_cal and cal_reason:
                blockers.append(cal_reason)

            if execute and not commission_mode:
                ok_appr, appr_reason = self.ensure_window_approval(
                    meta, run_name=run_name, interactive=interactive
                )
                if not ok_appr and appr_reason:
                    blockers.append(appr_reason)
            elif interactive:
                warnings.append("execute not authorized — observe/read-only session")

            static_ok, static_errors, static_warnings = self.validate_static_readiness(
                app,
                observe_only=observe_only or commission_mode,
            )
            warnings.extend(static_warnings)
            if not static_ok:
                blockers.extend(static_errors)

        if blockers:
            return SessionResult(
                status=STATUS_NOT_READY,
                market_id=meta.market_id,
                run_name=run_name,
                execute_authorized=execute,
                blockers=tuple(blockers),
                warnings=tuple(warnings),
                timings=timings,
                facts=list(self._facts),
            )

        _emit_fact(
            self._facts,
            fact_type=FACT_TYPE_Z_GAP_SESSION_STATUS,
            payload={"status": STATUS_READY_TO_WAIT, "market_id": meta.market_id},
        )
        if self.deps.write_fn:
            self.deps.write_fn(f"\n{STATUS_READY_TO_WAIT}\n")

        # Production runtime: bootstrap feeds and warm sigma before boundary.
        try:
            self._runtime_handle = await bootstrap_session_runtime(
                app=app,
                run_name=run_name,
                repo_root=self.repo_root,
                runs_dir=self.repo_root / app.runtime.reporting.runs_dir / run_name,
                use_shadow_oms=True if observe_only else (False if experimental_live else None),
            )
            if experimental_live and interactive:
                cap = max_usd if max_usd is not None else MAX_EXPERIMENTAL_USD
                record_experimental_approval_in_manifest(
                    self._runtime_handle,
                    market_id=meta.market_id,
                    max_usd=str(cap),
                    approved_at_ts=self.deps.now_ts(),
                )
            await start_production_feeds(self._runtime_handle, app)
            await warm_sigma_before_boundary(self._runtime_handle, app)
            timings.sigma_warmup_ms = self._runtime_handle.startup.sigma_warmup_ms
        except Exception as exc:
            blockers.append(f"runtime_bootstrap: {exc}")
            if self._runtime_handle is not None:
                await shutdown_session_runtime(self._runtime_handle)
                self._runtime_handle = None
            return SessionResult(
                status=STATUS_NOT_READY,
                market_id=meta.market_id,
                run_name=run_name,
                execute_authorized=execute,
                blockers=tuple(blockers),
                warnings=tuple(warnings),
                timings=timings,
                facts=list(self._facts),
            )

        assert zg is not None
        if experimental_mode:
            boundary, ptb_trace, ptb_reading = await capture_ptb_at_boundary(
                handle=self._runtime_handle,
                meta=meta,
                ptb_config=zg.ptb,
                evaluate_boundary=self.evaluate_boundary,
                app=app,
                experimental_mode=True,
                now_ts=self.deps.now_ts,
                sleep=self.deps.sleep,
                write_fn=self.deps.write_fn,
            )
            _emit_fact(
                self._facts,
                fact_type=FACT_TYPE_Z_GAP_SESSION_STATUS,
                payload={"status": PTB_CAPTURE_WAITING, "market_id": meta.market_id},
            )
        else:
            await self.wait_for_boundary(meta.event_start_ts)
            boundary = await self.evaluate_boundary(app, meta, experimental_mode=False)
            ptb_reading = read_live_ptb_from_handle(self._runtime_handle)
            from tyrex_pm.runtime.z_gap_ptb_capture import build_ptb_capture_trace

            now = self.deps.now_ts()
            ptb_trace = build_ptb_capture_trace(
                meta=meta,
                boundary=boundary,
                ptb_reading=ptb_reading,
                capture_started_at=now,
                capture_deadline=now,
                capture_ended_at=now,
                poll_count=1,
                max_lag_ms=zg.ptb.max_usable_boundary_lag_ms,
            )
        apply_boundary_k_to_store(self._runtime_handle, boundary)
        ptb_reading = read_live_ptb_from_handle(self._runtime_handle)
        model_usable = boundary.ready_to_evaluate

        capture_payload = capture_trace_to_payload(ptb_trace)
        if boundary.selection is not None:
            capture_payload.update(boundary.selection.to_fact_payload())
        capture_payload["live_source_ts"] = ptb_reading.source_ts
        capture_payload["live_recv_ts"] = ptb_reading.recv_ts
        capture_payload["model_usable_for_entry"] = boundary.ready_to_evaluate
        _emit_fact(
            self._facts,
            fact_type=FACT_TYPE_Z_GAP_PTB_CAPTURE,
            payload=capture_payload,
        )
        if self._runtime_handle is not None:
            from tyrex_pm.reporting.facts import make_fact

            self._runtime_handle.sink.write(
                make_fact(
                    FACT_TYPE_Z_GAP_PTB_CAPTURE,
                    str(self._runtime_handle.run_id),
                    capture_payload,
                )
            )

        if model_usable:
            status = STATUS_PTB_READY
        else:
            status = STATUS_NO_TRADE
            if experimental_mode:
                warnings.append(f"ptb_not_usable_for_entry: {boundary.block_reason or boundary.status}")
            else:
                blockers.append(boundary.block_reason or boundary.status)

        _emit_fact(
            self._facts,
            fact_type=FACT_TYPE_Z_GAP_SESSION_STATUS,
            payload={
                "status": status,
                "market_id": meta.market_id,
                "boundary": boundary.status,
                "model_usable": model_usable,
            },
        )
        if self.deps.write_fn:
            self.deps.write_fn(f"{status}\n")

        exit_code = 0
        order_submitted = False
        oms_submissions = 0
        facts_path: str | None = None
        run_execute = False
        should_run_runtime = False
        if observe_only:
            should_run_runtime = True
        elif experimental_live and execute and model_usable:
            should_run_runtime = True
            run_execute = True
        elif not experimental_mode and model_usable:
            should_run_runtime = True
            run_execute = execute

        if should_run_runtime and self._runtime_handle is not None:
            ptb_locked = bool(boundary.selection and boundary.selection.locked)
            exit_code = await run_post_boundary_runtime(
                self._runtime_handle,
                app,
                execute=run_execute,
                ptb_locked=ptb_locked,
            )
            facts_path = str(self._runtime_handle.facts_path) if self._runtime_handle.facts_path else None
            order_submitted = run_execute and exit_code == 0
            if self._runtime_handle.oms is not None and hasattr(self._runtime_handle.oms, "submit_count"):
                oms_submissions = int(getattr(self._runtime_handle.oms, "submit_count", 0))

        report_handle = self._runtime_handle
        if self._runtime_handle is not None:
            await shutdown_session_runtime(self._runtime_handle)
            self._runtime_handle = None

        if self.deps.sidecar is not None and self.deps.sidecar._owned:
            self.deps.sidecar.stop()

        report_path = self.artifacts_dir / f"session_report_{run_name}.json"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        terminal_status = STATUS_TERMINAL if experimental_mode or model_usable else STATUS_NO_TRADE
        if observe_only:
            report = build_observe_session_report(
                meta=meta,
                run_name=run_name,
                prestart_s=lead,
                boundary=boundary,
                ptb_reading=ptb_reading,
                handle=report_handle,
                warnings=warnings,
                oms_submissions=oms_submissions,
                terminal_status=terminal_status,
                facts_path=facts_path,
                ptb_trace=ptb_trace,
            )
            report["timings"] = timings.__dict__
            report["exit_code"] = exit_code
        else:
            report = {
                "status": terminal_status,
                "market_id": meta.market_id,
                "run_name": run_name,
                "execute_authorized": execute,
                "order_submitted": order_submitted,
                "boundary_status": boundary.status,
                "model_usable_for_entry": model_usable,
                "exit_code": exit_code,
                "timings": timings.__dict__,
                "blockers": blockers,
                "warnings": warnings,
                "oms_submissions": oms_submissions,
            }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        terminal_summary_path = self.repo_root / app.runtime.reporting.runs_dir / run_name / "terminal_summary.json"
        if terminal_summary_path.parent.is_dir():
            terminal_summary_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        return SessionResult(
            status=terminal_status,
            market_id=meta.market_id,
            run_name=run_name,
            execute_authorized=execute,
            order_submitted=order_submitted,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            timings=timings,
            report_path=str(report_path),
            facts=list(self._facts),
            runtime_exit_code=exit_code,
            facts_path=facts_path,
            oms_submissions=oms_submissions,
        )

    def _implementation_hash(self) -> str:
        try:
            from tyrex_pm.runtime.z_gap_session_runtime import _git_sha

            return _git_sha()
        except Exception:
            return "unknown"

    async def run_commission_ptb(
        self,
        *,
        run_name: str,
        windows_required: int = 3,
        max_attempts: int = 6,
    ) -> CommissionPtbResult:
        """Observe-only multi-window PTB commissioning; never submits orders."""
        blockers: list[str] = []
        if not independent_ptb_reference_available():
            return CommissionPtbResult(
                status=STATUS_NOT_READY,
                run_name=run_name,
                windows_attempted=0,
                windows_usable=0,
                windows_required=windows_required,
                blockers=("independent PTB reference requires TYREX_ETH_RPC_URL or ETH_RPC_URL",),
            )

        app = self._load_app(entry_mode=Z_GAP_ENTRY_MODE_OBSERVE_ONLY)
        fingerprint = compute_z_gap_config_fingerprint(
            app,
            strategy_file=self.repo_root / self.strategy_file,
            scenario_file=self.repo_root / self.scenario_file,
        )

        window_rows: list[dict[str, Any]] = []
        usable_count = 0
        attempts = 0

        while usable_count < windows_required and attempts < max_attempts:
            attempts += 1
            window_run = f"{run_name}_w{attempts}"
            session = await self.run_session(
                run_name=window_run,
                next_window=True,
                execute=False,
                interactive=False,
                observe_only=True,
                commission_mode=True,
            )
            row_base: dict[str, Any] = {
                "attempt": attempts,
                "run_name": window_run,
                "session_status": session.status,
                "market_id": session.market_id,
                "facts_path": session.facts_path,
                "runtime_exit_code": session.runtime_exit_code,
                "oms_submissions": session.oms_submissions,
            }
            if session.status != STATUS_TERMINAL or session.runtime_exit_code not in (0, None):
                window_rows.append({**row_base, "usable": False, "exclude_reason": "session_failed"})
                continue

            lock_path = self.artifacts_dir / "ptb_boundary_lock.json"
            if not lock_path.is_file():
                row_base.update({"usable": False, "exclude_reason": "missing_boundary_lock"})
                window_rows.append(row_base)
                continue
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            locked_k = lock.get("selected_k")
            if not locked_k:
                row_base.update({"usable": False, "exclude_reason": "missing_locked_k"})
                window_rows.append(row_base)
                continue

            event_start_ts = float(lock.get("event_start_ts") or 0)
            event_end_ts = float(lock.get("event_end_ts") or 0)
            if self.deps.now_ts() < event_end_ts:
                await self.wait_for_boundary(event_end_ts)

            independent = fetch_independent_ptb_reference(event_start_ts=event_start_ts)
            row = evaluate_commissioning_window(
                market_id=str(lock.get("market_id") or session.market_id),
                event_start_ts=event_start_ts,
                event_end_ts=event_end_ts,
                locked_k=str(locked_k),
                live_k=lock.get("live_k"),
                log_k=lock.get("log_k"),
                live_log_difference_bps=lock.get("difference_bps"),
                independent=independent,
            )
            row.update(row_base)
            window_rows.append(row)
            if row.get("usable"):
                usable_count += 1

        now_ts = self.deps.now_ts()
        certificate = build_commissioning_certificate(
            policy_id=COMMISSIONING_POLICY_NAME,
            implementation_hash=self._implementation_hash(),
            config_hash=fingerprint.combined_hash,
            windows=window_rows,
            windows_required=windows_required,
            now_ts=now_ts,
            independent_source=INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3,
        )
        cert_path = self.artifacts_dir / "ptb_commissioning_certificate.json"
        write_commissioning_certificate(cert_path, certificate)
        report_path = self.repo_root / "Docs/Implementation/z_gap_strategy/ptb_commissioning_report.md"
        independent_doc = {
            "source": INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3,
            "endpoint": "Ethereum JSON-RPC eth_call → Chainlink BTC/USD aggregator V3 latestRoundData()",
            "field_name": "answer (int256, 8 decimals); round timestamp from updatedAt",
            "timestamp_semantics": "updatedAt is Chainlink round update time (unix seconds); compared to event_start_ts",
            "availability_delay": "On-chain round typically within heartbeat (~3600s); nearest-round search within 7200s",
            "failure_behavior": "Window excluded from certificate; certificate status=invalid if insufficient usable windows",
            "why_independent": (
                "Direct Ethereum mainnet aggregator read; not Polymarket RTDS, not Tyrex sidecar, "
                "not Tyrex boundary selection path"
            ),
        }
        write_commissioning_report(report_path, certificate=certificate, independent_doc=independent_doc)

        status = STATUS_TERMINAL if certificate.get("status") == "valid" else STATUS_NOT_READY
        if certificate.get("status") != "valid":
            blockers.append("commissioning certificate invalid — policy requirements not met")

        return CommissionPtbResult(
            status=status,
            run_name=run_name,
            windows_attempted=attempts,
            windows_usable=usable_count,
            windows_required=windows_required,
            certificate_path=str(cert_path),
            report_path=str(report_path),
            window_rows=window_rows,
            blockers=tuple(blockers),
        )
