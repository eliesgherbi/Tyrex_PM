"""Signal-only live observation over successive windows (no orders, ever).

The strategy runs on live public feeds through the same ``run_market_data_runtime``,
``MarketSessionRuntime`` and plugin driver as a trading run. Its intents become
**recommendation records**; they are never planned, signed or submitted.

Structural guarantees (tested):

* this module and its import closure load nothing from ``tyrex_pm.execution`` (no
  planner, coordinator, lifecycle, gateway or signer) and no authenticated SDK client;
* there is no intent queue: ``ObservationSession`` only appends records;
* positions are always flat in the strategy context; a recommendation never becomes a
  position or a fill. Hypothetical outcomes are computed offline from the recorded book
  at decision time and labelled hypothetical.

What observation can validate: indicator warm-up and freshness on real feeds, reference
and K alignment, signal frequency, local decision latency, quote staleness at decision
time, offline/live input similarity, hypothetical outcomes of recommendations.
What it cannot validate: real fills, venue latency/acknowledgement, fees charged,
rejections, hold commitment, resolution/payout of our own position, redemption, restart
with a live holding.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from tyrex_pm.core.clock import Clock, SystemClock
from tyrex_pm.recording import RecordingConfig, recording_config_from_mapping
from tyrex_pm.recording.records import input_timing
from tyrex_pm.runtime.decision_records import (
    RECORD_SCHEMA,
    decision_context,
    evaluated_record,
    skipped_record,
)
from tyrex_pm.strategies.registry import get_strategy_plugin

OBSERVATION_MODE = "observe"
LIVE_REFERENCE_SERIES = "polymarket_rtds:crypto_prices_twap_sixty:btc/usd"


class ObservationConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ObservationConfig:
    run_name: str
    strategy_kind: str
    strategy_parameters: Mapping[str, Any]
    output_directory: Path
    market_family: str = "btc_updown_5m"
    binance_symbol: str = "BTCUSDT"
    max_windows: int = 12
    max_duration_s: float = 3900.0
    evaluation_interval_s: float = 1.0
    basis_ewma_half_life_s: float = 30.0
    require_ssr_price_match: bool = False
    flush_rows: int = 200
    flush_interval_s: float = 2.0
    max_buffered_rows: int = 20_000
    target_notional: Decimal = Decimal("5")
    # Decision rows are always written (``decisions`` stream); level ``explain`` adds
    # indicator explanations through the shared recorder (recording/host.py).
    recording: RecordingConfig = field(default_factory=RecordingConfig)

    @property
    def strategy_config(self) -> Any:
        plugin = get_strategy_plugin(self.strategy_kind)
        return plugin.load_config(dict(self.strategy_parameters), f"observe:{self.run_name}")


_TOP = {"schema_version", "mode", "run_name", "strategy", "market", "observation", "output",
        "recording"}


def load_observation_config(path: Path) -> ObservationConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ObservationConfigError("observation config must be a mapping")
    unknown = sorted(set(raw) - _TOP)
    if unknown:
        raise ObservationConfigError(f"unknown top-level field(s): {unknown}")
    if raw.get("schema_version") != 1:
        raise ObservationConfigError("schema_version must be 1")
    if raw.get("mode") != OBSERVATION_MODE:
        raise ObservationConfigError("mode must be 'observe' (signal-only, no orders)")
    strategy = dict(raw.get("strategy") or {})
    if set(strategy) - {"kind", "parameters"}:
        raise ObservationConfigError("strategy accepts only kind and parameters")
    market = dict(raw.get("market") or {})
    if set(market) - {"family", "binance_symbol", "require_ssr_price_match"}:
        raise ObservationConfigError("unknown market field(s)")
    obs = dict(raw.get("observation") or {})
    allowed_obs = {
        "max_windows", "max_duration_s", "evaluation_interval_s", "basis_ewma_half_life_s",
        "flush_rows", "flush_interval_s", "max_buffered_rows",
    }
    extra = sorted(set(obs) - allowed_obs)
    if extra:
        raise ObservationConfigError(f"unknown observation field(s): {extra}")
    output = dict(raw.get("output") or {})
    if set(output) - {"directory"}:
        raise ObservationConfigError("output accepts only directory")
    config = ObservationConfig(
        run_name=str(raw.get("run_name") or "observe"),
        strategy_kind=str(strategy.get("kind") or ""),
        strategy_parameters=dict(strategy.get("parameters") or {}),
        output_directory=Path(str(output.get("directory") or "var/observation")),
        market_family=str(market.get("family", "btc_updown_5m")),
        binance_symbol=str(market.get("binance_symbol", "BTCUSDT")),
        require_ssr_price_match=bool(market.get("require_ssr_price_match", False)),
        max_windows=int(obs.get("max_windows", 12)),
        max_duration_s=float(obs.get("max_duration_s", 3900.0)),
        evaluation_interval_s=float(obs.get("evaluation_interval_s", 1.0)),
        basis_ewma_half_life_s=float(obs.get("basis_ewma_half_life_s", 30.0)),
        flush_rows=int(obs.get("flush_rows", 200)),
        flush_interval_s=float(obs.get("flush_interval_s", 2.0)),
        max_buffered_rows=int(obs.get("max_buffered_rows", 20_000)),
        recording=_recording(raw.get("recording")),
    )
    if config.max_windows < 1 or config.max_duration_s <= 0:
        raise ObservationConfigError("max_windows and max_duration_s must be positive")
    try:
        config.strategy_config  # noqa: B018 - validates the plugin parameters
    except KeyError as exc:
        raise ObservationConfigError(str(exc)) from exc
    return config


@dataclass
class ObservationRecorder:
    """Bounded, batched JSONL writer. Telemetry may drop (counted); nothing else exists."""

    directory: Path
    flush_rows: int = 200
    flush_interval_s: float = 2.0
    max_buffered_rows: int = 20_000
    dropped: int = 0
    written: dict[str, int] = field(default_factory=dict)
    _buffers: dict[str, list[str]] = field(default_factory=dict)
    _last_flush: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def record(self, stream: str, row: Mapping[str, Any]) -> None:
        buffered = sum(len(b) for b in self._buffers.values())
        if buffered >= self.max_buffered_rows:
            self.dropped += 1
            return
        self._buffers.setdefault(stream, []).append(json.dumps(row, default=str, sort_keys=True))
        if buffered + 1 >= self.flush_rows or time.monotonic() - self._last_flush >= (
            self.flush_interval_s
        ):
            self.flush()

    def flush(self) -> None:
        for stream, lines in self._buffers.items():
            if not lines:
                continue
            with (self.directory / f"{stream}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            self.written[stream] = self.written.get(stream, 0) + len(lines)
            lines.clear()
        self._last_flush = time.monotonic()


def _book_tops(snapshot: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, book in (("up", snapshot.yes_book), ("down", snapshot.no_book)):
        if book is None:
            continue
        out[f"{name}_asks"] = [[str(lv.price), str(lv.quantity)] for lv in book.asks[:10]]
        out[f"{name}_bids"] = [[str(lv.price), str(lv.quantity)] for lv in book.bids[:10]]
    return out


def _recording(raw: Any) -> RecordingConfig:
    try:
        return recording_config_from_mapping(raw if isinstance(raw, Mapping) else None)
    except ValueError as exc:
        raise ObservationConfigError(str(exc)) from exc


@dataclass
class ObservationSession:
    """The only consumer of strategy output in observe mode: it records, nothing else."""

    config: ObservationConfig
    recorder: ObservationRecorder
    clock: Clock = field(default_factory=SystemClock)
    evaluations: int = 0
    recommendations: int = 0
    windows_seen: list[str] = field(default_factory=list)
    rollovers: int = 0
    evaluate_ms: list[float] = field(default_factory=list)
    # Explain stream (level ``explain``); decision rows go through ``recorder``.
    explain_recording: Any = None

    def on_evaluation(self, market_runtime: Any) -> list[dict[str, Any]]:
        ready = market_runtime.prepare_aligned_eval()
        now = self.clock.now_utc()
        at_ns = int(now.timestamp() * 1e9)
        session = ready.session or market_runtime.active
        end = None if session is None or session.market.event_end is None else int(
            session.market.event_end.timestamp() * 1e9
        )
        window_id = None if session is None else session.window_id
        if window_id and window_id not in self.windows_seen:
            self.windows_seen.append(window_id)
        if not ready.ok or ready.snapshot is None or ready.binding is None:
            row = {**skipped_record(ready, at_ns=at_ns, window_end_ns=end), "window_id": window_id,
                   "inputs": input_timing(ready)}
            self.recorder.record("decisions", row)
            if self.explain_recording is not None:
                self.explain_recording.on_skipped(ready, row, window_id=window_id)
            return [row]
        # Always flat: recommendations never become positions.
        context = decision_context(
            ready,
            run_id=f"observe:{self.config.run_name}",
            target_notional=self.config.target_notional,
        )
        graph = getattr(market_runtime, "indicator_graph", None)
        features = graph.snapshot() if graph is not None else None
        started = time.perf_counter()
        result = ready.binding.evaluate(
            market_snapshot=ready.snapshot,
            causation_id=ready.snapshot.causation_id,
            correlation_id=ready.snapshot.correlation_id,
            context=context,
            trigger="observe",
            features=features,
        )
        self.evaluate_ms.append((time.perf_counter() - started) * 1000.0)
        self.evaluations += 1
        row = {
            **evaluated_record(ready, result, at_ns=at_ns, window_end_ns=end),
            "window_id": window_id,
            "evaluate_ms": self.evaluate_ms[-1],
            "snapshot_observed_at": ready.snapshot.observed_at.isoformat(),
            "inputs": input_timing(ready),
        }
        self.recorder.record("decisions", row)
        if self.explain_recording is not None:
            self.explain_recording.on_evaluated(ready, row, window_id=window_id, graph=graph)
        for intent in result.intents:
            self.recommendations += 1
            self.recorder.record(
                "recommendations",
                {
                    "schema": RECORD_SCHEMA,
                    "hypothetical": True,
                    "no_order_submitted": True,
                    "at_ns": at_ns,
                    "window_id": window_id,
                    "intent_type": type(intent).__name__,
                    "reason_code": intent.reason_code,
                    "instrument_id": intent.instrument_id.value,
                    "max_price": str(getattr(intent, "max_price", None)),
                    "target_notional": str(getattr(intent, "target_notional", None)),
                    "evidence": dict(intent.evidence),
                    **_book_tops(ready.snapshot),
                },
            )
        return [row]

    def on_rollover(self, payload: dict[str, Any]) -> None:
        self.rollovers += 1
        self.recorder.record("windows", {"event": "rollover", **payload})

    def should_stop(self) -> bool:
        return self.rollovers >= self.config.max_windows


MarketDataRunner = Callable[..., Awaitable[Any]]


async def run_observation(
    config: ObservationConfig,
    *,
    out_dir: Path,
    market_data_runner: MarketDataRunner | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Run signal-only observation. ``market_data_runner`` is injectable for tests."""
    from tyrex_pm.runtime.market_runtime import MarketSessionRuntime

    if market_data_runner is None:
        from tyrex_pm.runtime.market_data_runtime import run_market_data_runtime

        market_data_runner = run_market_data_runtime
    clock = clock or SystemClock()
    recorder = ObservationRecorder(
        out_dir,
        flush_rows=config.flush_rows,
        flush_interval_s=config.flush_interval_s,
        max_buffered_rows=config.max_buffered_rows,
    )
    session = ObservationSession(config=config, recorder=recorder, clock=clock)
    plugin_config = config.strategy_config
    runtime = MarketSessionRuntime.create(
        clock=clock,
        strategy_kind=config.strategy_kind,
        strategy_config=plugin_config,
        require_ssr_price_match=config.require_ssr_price_match,
        target_notional=config.target_notional,
    )
    started = datetime.now(timezone.utc)
    manifest = {
        "mode": OBSERVATION_MODE,
        "schema": RECORD_SCHEMA,
        "run_name": config.run_name,
        "strategy_kind": config.strategy_kind,
        "strategy_parameters": dict(config.strategy_parameters),
        "market_family": config.market_family,
        "reference_series": LIVE_REFERENCE_SERIES,
        "started_at": started.isoformat(),
        "orders": "NEVER: signal-only observation",
    }
    (out_dir / "observation_manifest.json").write_text(
        json.dumps(manifest, indent=1, default=str), encoding="utf-8"
    )
    from tyrex_pm.recording.host import open_decision_recording

    plugin = get_strategy_plugin(config.strategy_kind)
    try:
        band = tuple(float(v) for v in plugin.entry_tau_bounds(plugin_config))
    except Exception:  # noqa: BLE001 - the explain band is optional
        band = None
    session.explain_recording = open_decision_recording(
        config.recording, output_directory=out_dir, mode=OBSERVATION_MODE,
        run_id=config.run_name, schema=None, entry_band=band, manifest=manifest,
        record_decisions=False)

    async def tick(_runtime: Any) -> None:
        recorder.flush()

    try:
        summary = await market_data_runner(
            out_dir=out_dir,
            run_id=config.run_name,
            runtime=runtime,
            max_duration_s=config.max_duration_s,
            duration_anchor="compose_start",
            stop_when_seals_met=False,
            evaluation_interval_s=config.evaluation_interval_s,
            basis_ewma_half_life_s=config.basis_ewma_half_life_s,
            require_ssr_price_match=config.require_ssr_price_match,
            target_notional=config.target_notional,
            binance_symbol=config.binance_symbol,
            market_family=config.market_family,
            on_evaluation=session.on_evaluation,
            on_rollover=session.on_rollover,
            should_stop=session.should_stop,
            on_async_tick=tick,
        )
    finally:
        recording_status = (
            None if session.explain_recording is None
            else session.explain_recording.close()
        )
    recorder.flush()
    result = {
        **manifest,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "evaluations": session.evaluations,
        "recommendations": session.recommendations,
        "windows_seen": session.windows_seen,
        "rollovers": session.rollovers,
        "records_written": dict(recorder.written),
        "records_dropped": recorder.dropped,
        "evaluate_ms_p50": _quantile(session.evaluate_ms, 0.5),
        "evaluate_ms_p99": _quantile(session.evaluate_ms, 0.99),
        "market_data_errors": list(getattr(summary, "errors", []) or []),
        "recording": recording_status,
    }
    (out_dir / "observation_summary.json").write_text(
        json.dumps(result, indent=1, default=str), encoding="utf-8"
    )
    return result


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def run_observation_sync(config: ObservationConfig, *, out_dir: Path) -> dict[str, Any]:
    return asyncio.run(run_observation(config, out_dir=out_dir))
