"""Continuous BTC 5m paired-binary orchestration (run_continue MVP)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.errors import ConfigError
from tyrex_pm.ingestion.btc_5m_window_scheduler import (
    Btc5mWindowPlan,
    select_next_btc_5m_trading_window,
    validate_btc_5m_reference_url,
)
from tyrex_pm.ingestion.market_discovery import MarketDiscoveryResult, discover_btc_5m_by_slug
from tyrex_pm.runtime.config import (
    STRATEGY_KIND_PAIRED_BINARY,
    load_app_config,
)
from tyrex_pm.venue.polymarket.event_metadata import EventMetadataError

log = logging.getLogger(__name__)

RUN_CONTINUE_DIR = "var/reporting/run_continue"
GAMMA_START_TS_TOLERANCE_S = 2.0
DEFAULT_GAMMA_RETRY_BACKOFF_S = 2.0
MAX_GAMMA_RETRY_BACKOFF_S = 10.0


@dataclass(frozen=True)
class RunContinueConfig:
    strategy: str
    scenario: str
    session_run_name: str
    reference_event_url: str
    repo_root: Path
    state_dir: str
    prestart_seconds: float
    entry_grace_seconds: float
    sleep_granularity: float
    max_windows: int | None


def _repo_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    return Path(__file__).resolve().parents[3]


def _git_sha(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _safe_run_dir_label(name: str) -> str:
    from tyrex_pm.runtime.app import _safe_run_dir_label

    return _safe_run_dir_label(name)


def session_dir(repo_root: Path, session_run_name: str) -> Path:
    label = _safe_run_dir_label(session_run_name)
    if not label:
        raise ConfigError("--run-name is required and must not sanitize to empty")
    return repo_root / RUN_CONTINUE_DIR / label


def build_window_run_name(session_name: str, market_id: str) -> str:
    composed = f"{session_name}__{market_id}"
    safe = _safe_run_dir_label(composed)
    return safe or composed.replace("/", "_")[:120]


def write_session_manifest(session_path: Path, manifest: dict) -> None:
    session_path.mkdir(parents=True, exist_ok=True)
    (session_path / "session_manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )


def append_session_event(session_path: Path, event: dict) -> None:
    session_path.mkdir(parents=True, exist_ok=True)
    row = dict(event)
    row.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with (session_path / "session_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


async def wait_until(
    wake_at_ts: float,
    stop: asyncio.Event,
    granularity: float,
) -> None:
    """Sleep until *wake_at_ts*, checking *stop* every *granularity* seconds."""
    while not stop.is_set():
        remaining = wake_at_ts - time.time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(remaining, max(0.05, granularity)))
        except asyncio.TimeoutError:
            continue


def _register_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def _request_stop(*_args) -> None:
        stop.set()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass


def _validate_paired_binary_strategy(repo_root: Path, strategy: str, scenario: str) -> None:
    app = load_app_config(
        repo_root=repo_root,
        strategy_file=strategy,
        scenario_file=scenario,
    )
    if app.strategy_kind != STRATEGY_KIND_PAIRED_BINARY:
        raise ConfigError(
            f"run_continue MVP supports paired_binary only (got {app.strategy_kind!r})"
        )


async def resolve_window_metadata(
    plan: Btc5mWindowPlan,
    *,
    entry_grace_seconds: float,
    stop: asyncio.Event,
) -> MarketDiscoveryResult | None:
    """Resolve Gamma metadata for *plan* with bounded retry until entry grace expires."""
    deadline = plan.window_start_ts + entry_grace_seconds
    backoff = DEFAULT_GAMMA_RETRY_BACKOFF_S

    while time.time() < deadline and not stop.is_set():
        discovered = await asyncio.to_thread(discover_btc_5m_by_slug, plan.event_slug)
        if discovered is not None:
            delta = abs(float(discovered.event_start_ts) - float(plan.window_start_ts))
            if delta <= GAMMA_START_TS_TOLERANCE_S:
                return discovered
            log.warning(
                "run_continue: Gamma start_ts mismatch slug=%s planned=%s resolved=%s delta=%s",
                plan.event_slug,
                plan.window_start_ts,
                discovered.event_start_ts,
                delta,
            )
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(backoff, remaining))
            return None
        except asyncio.TimeoutError:
            backoff = min(backoff * 1.5, MAX_GAMMA_RETRY_BACKOFF_S)
            continue
    return None


def _make_run_args(config: RunContinueConfig, *, run_name: str, event_url: str) -> argparse.Namespace:
    return argparse.Namespace(
        strategy=config.strategy,
        scenario=config.scenario,
        repo_root=config.repo_root,
        state_dir=config.state_dir,
        run_name=run_name,
        event_url=event_url,
        once=False,
        fixture=None,
        max_iterations=None,
    )


async def run_continue_session(
    config: RunContinueConfig,
    *,
    stop: asyncio.Event,
    execute_run=None,
) -> int:
    if execute_run is None:
        from tyrex_pm.runtime.run_once import execute_run as _execute_run

        execute_run = _execute_run

    try:
        validate_btc_5m_reference_url(config.reference_event_url)
    except EventMetadataError as exc:
        log.error("run_continue reference URL validation failed: %s", exc)
        return 2

    sess_path = session_dir(config.repo_root, config.session_run_name)
    session_id = str(uuid4())
    write_session_manifest(
        sess_path,
        {
            "session_id": session_id,
            "session_run_name": config.session_run_name,
            "git_sha": _git_sha(config.repo_root),
            "strategy": config.strategy,
            "scenario": config.scenario,
            "reference_event_url": config.reference_event_url,
            "prestart_seconds": config.prestart_seconds,
            "entry_grace_seconds": config.entry_grace_seconds,
            "max_windows": config.max_windows,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    append_session_event(
        sess_path,
        {
            "event": "session_started",
            "session_id": session_id,
            "session_run_name": config.session_run_name,
            "reference_event_url": config.reference_event_url,
            "prestart_seconds": config.prestart_seconds,
        },
    )

    windows_completed = 0
    session_exit = 0

    while not stop.is_set():
        plan = select_next_btc_5m_trading_window(
            now_ts=time.time(),
            prestart_seconds=config.prestart_seconds,
            entry_grace_seconds=config.entry_grace_seconds,
        )
        append_session_event(
            sess_path,
            {
                "event": "window_scheduled",
                "window_start_ts": plan.window_start_ts,
                "window_end_ts": plan.window_end_ts,
                "event_slug": plan.event_slug,
                "event_url": plan.event_url,
                "wake_at_ts": plan.wake_at_ts,
                "skip_reason": plan.skip_reason,
            },
        )
        log.info(
            "run_continue: scheduled window start=%s wake_at=%s slug=%s",
            plan.window_start_ts,
            plan.wake_at_ts,
            plan.event_slug,
        )

        await wait_until(plan.wake_at_ts, stop, config.sleep_granularity)
        if stop.is_set():
            break

        meta = await resolve_window_metadata(
            plan,
            entry_grace_seconds=config.entry_grace_seconds,
            stop=stop,
        )
        if meta is None:
            append_session_event(
                sess_path,
                {
                    "event": "window_skipped",
                    "event_slug": plan.event_slug,
                    "reason": "gamma_unavailable_before_entry_grace",
                },
            )
            log.warning(
                "run_continue: skipping window slug=%s (Gamma unavailable before entry grace)",
                plan.event_slug,
            )
            if stop.is_set():
                break
            continue
        if stop.is_set():
            break

        window_run_name = build_window_run_name(config.session_run_name, meta.market_id)
        append_session_event(
            sess_path,
            {
                "event": "window_run_start",
                "run_name": window_run_name,
                "market_id": meta.market_id,
                "event_url": plan.event_url,
                "event_slug": plan.event_slug,
            },
        )
        log.info(
            "run_continue: starting window market_id=%s run_name=%s",
            meta.market_id,
            window_run_name,
        )

        run_args = _make_run_args(config, run_name=window_run_name, event_url=plan.event_url)
        exit_code = await execute_run(run_args)

        if exit_code != 0:
            append_session_event(
                sess_path,
                {
                    "event": "window_run_failed",
                    "run_name": window_run_name,
                    "market_id": meta.market_id,
                    "exit_code": exit_code,
                },
            )
            log.error(
                "run_continue: window failed market_id=%s exit_code=%s; stopping session",
                meta.market_id,
                exit_code,
            )
            session_exit = exit_code
            break

        append_session_event(
            sess_path,
            {
                "event": "window_run_complete",
                "run_name": window_run_name,
                "market_id": meta.market_id,
                "exit_code": exit_code,
            },
        )
        windows_completed += 1
        log.info(
            "run_continue: window complete market_id=%s (%s/%s)",
            meta.market_id,
            windows_completed,
            config.max_windows or "∞",
        )

        if config.max_windows is not None and windows_completed >= config.max_windows:
            append_session_event(
                sess_path,
                {
                    "event": "session_stopped",
                    "reason": "max_windows_reached",
                    "windows_completed": windows_completed,
                },
            )
            return 0

        if stop.is_set():
            break

    reason = "operator_stop" if stop.is_set() else "completed"
    append_session_event(
        sess_path,
        {
            "event": "session_stopped",
            "reason": reason,
            "windows_completed": windows_completed,
        },
    )
    return session_exit if session_exit != 0 else 0


def _config_from_args(args: argparse.Namespace) -> RunContinueConfig:
    repo_root = _repo_root(getattr(args, "repo_root", None))
    session_run_name = getattr(args, "run_name", None)
    if not session_run_name or not str(session_run_name).strip():
        raise ConfigError("--run-name is required for run_continue")

    reference = getattr(args, "event_url", None)
    if not reference or not str(reference).strip():
        raise ConfigError("--event-url is required for run_continue (BTC 5m reference URL)")

    max_windows = getattr(args, "max_windows", None)
    if max_windows is not None and max_windows < 1:
        raise ConfigError("--max-windows must be >= 1 when set")

    return RunContinueConfig(
        strategy=str(args.strategy),
        scenario=str(args.scenario),
        session_run_name=str(session_run_name).strip(),
        reference_event_url=str(reference).strip(),
        repo_root=repo_root,
        state_dir=str(getattr(args, "state_dir", "var/state")),
        prestart_seconds=float(getattr(args, "prestart_seconds", 30.0)),
        entry_grace_seconds=float(getattr(args, "entry_grace_seconds", 15.0)),
        sleep_granularity=float(getattr(args, "sleep_granularity", 1.0)),
        max_windows=max_windows,
    )


async def cmd_run_continue(args: argparse.Namespace) -> int:
    repo_root = _repo_root(getattr(args, "repo_root", None))
    try:
        config = _config_from_args(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    try:
        _validate_paired_binary_strategy(repo_root, config.strategy, config.scenario)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    logging.basicConfig(level=logging.INFO)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _register_signal_handlers(loop, stop)

    log.info(
        "run_continue session=%s reference=%s prestart=%ss",
        config.session_run_name,
        config.reference_event_url,
        config.prestart_seconds,
    )
    try:
        return await run_continue_session(config, stop=stop)
    except KeyboardInterrupt:
        stop.set()
        return 0
