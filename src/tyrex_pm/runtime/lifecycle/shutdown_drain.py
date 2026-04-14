"""Phase 4 — live shutdown cancel-and-drain (``shutdown_drain.md``)."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nautilus_trader.adapters.polymarket import POLYMARKET_CLIENT_ID
from nautilus_trader.model.identifiers import InstrumentId, StrategyId
from nautilus_trader.trading.strategy import Strategy

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.lifecycle.status import ExecutionLifecycleStatus
from tyrex_pm.runtime.state_readers import (
    POLYMARKET_VENUE_ID,
    NautilusExecutionStateReader,
    OrderSnapshot,
)

_LOG = logging.getLogger(__name__)

FactEmitFn = Callable[[str, dict[str, Any]], None]

_SHUTDOWN_POLL_INTERVAL_S = 0.2


def _env_shutdown_override() -> bool:
    raw = str(os.environ.get("TYREX_SHUTDOWN_DRAIN_OVERRIDE", "")).strip().lower()
    return raw in ("1", "true", "yes")


@dataclass(frozen=True, slots=True)
class ShutdownDrainResult:
    """``shutdown_drain.md`` §7 — coordinator output + skip metadata."""

    skipped: bool
    skip_reason: str | None
    canceled_order_count: int
    residual_open_orders: tuple[str, ...]
    timed_out: bool
    drain_duration_ms: int
    instruments_cancelled: int
    #: Per-instrument cancel API failures (``instrument_id:exc``); WP1 containment.
    cancel_failures: tuple[str, ...] = ()
    #: WP1 — set when drain aborted due to an unexpected exception (poll/read/cancel-all path);
    #: terminal ``shutdown_drain`` fact still emitted with this populated.
    internal_error: str | None = None

    def manifest_clean(self) -> bool:
        if self.internal_error:
            return False
        if self.skipped:
            # Shadow: drain not required. Live skips (override / disabled) are **unclean** — venue may
            # still hold working orders after process exit.
            return self.skip_reason == "shadow_mode"
        if self.cancel_failures:
            return False
        return not self.timed_out and not self.residual_open_orders


class ShutdownDrainCoordinator:
    """
    Ordered shutdown phases — ``shutdown_drain.md`` §8 (codable path).

    Live mass cancel uses the **public** :meth:`nautilus_trader.trading.strategy.Strategy.cancel_all_orders`
    per distinct open **instrument** (adapter routes ``CancelAllOrders``). There is no separate
    Tyrex-verified single-call “cancel all instruments” API on ``Strategy`` in the pinned stack.
    """

    __slots__ = ("_runtime", "_poll_interval_s")

    def __init__(
        self,
        runtime: RuntimeSettings,
        *,
        poll_interval_s: float = _SHUTDOWN_POLL_INTERVAL_S,
    ) -> None:
        self._runtime = runtime
        self._poll_interval_s = float(poll_interval_s)

    def run(
        self,
        *,
        strategy: Strategy,
        lifecycle: ExecutionLifecycleStatus,
        execution_reader: NautilusExecutionStateReader,
        fact_emit: FactEmitFn | None,
        run_id: str | None,
        run_context: Any | None = None,
    ) -> ShutdownDrainResult:
        env_override = _env_shutdown_override()
        yaml_override = bool(self._runtime.shutdown_drain_override)
        override_active = env_override or yaml_override

        if self._runtime.execution_mode != "live":
            return self._emit_and_return(
                fact_emit=fact_emit,
                run_id=run_id,
                run_context=run_context,
                result=ShutdownDrainResult(
                    skipped=True,
                    skip_reason="shadow_mode",
                    canceled_order_count=0,
                    residual_open_orders=(),
                    timed_out=False,
                    drain_duration_ms=0,
                    instruments_cancelled=0,
                    cancel_failures=(),
                ),
            )

        if not self._runtime.shutdown_drain_enabled:
            _LOG.error(
                "event=shutdown_drain_skip reason=disabled_by_config "
                "execution_mode=live — live shutdown cleanup is mandatory by default; "
                "re-enable shutdown_drain_enabled unless you accept venue orders surviving process exit",
            )
            return self._emit_and_return(
                fact_emit=fact_emit,
                run_id=run_id,
                run_context=run_context,
                result=ShutdownDrainResult(
                    skipped=True,
                    skip_reason="disabled_by_config",
                    canceled_order_count=0,
                    residual_open_orders=(),
                    timed_out=False,
                    drain_duration_ms=0,
                    instruments_cancelled=0,
                    cancel_failures=(),
                    internal_error=None,
                ),
            )

        if override_active:
            _LOG.error(
                "event=shutdown_drain_override "
                "TYREX_SHUTDOWN_DRAIN_OVERRIDE=%r shutdown_drain_override_yaml=%s — "
                "skipping cancel-and-drain; venue orders may remain live after exit",
                os.environ.get("TYREX_SHUTDOWN_DRAIN_OVERRIDE"),
                yaml_override,
            )
            return self._emit_and_return(
                fact_emit=fact_emit,
                run_id=run_id,
                run_context=run_context,
                result=ShutdownDrainResult(
                    skipped=True,
                    skip_reason="operator_override",
                    canceled_order_count=0,
                    residual_open_orders=(),
                    timed_out=False,
                    drain_duration_ms=0,
                    instruments_cancelled=0,
                    cancel_failures=(),
                    internal_error=None,
                ),
            )

        timeout_s = float(self._runtime.shutdown_drain_timeout_seconds)
        if timeout_s <= 0:
            raise ValueError("shutdown_drain_timeout_seconds must be positive")

        t_wall0 = time.monotonic()
        lifecycle.begin_shutdown_drain(transition_mono=t_wall0)

        sid = strategy.id
        strategy_id = sid if isinstance(sid, StrategyId) else StrategyId(str(sid))

        try:
            result = self._live_drain_body(
                strategy=strategy,
                strategy_id=strategy_id,
                execution_reader=execution_reader,
                timeout_s=timeout_s,
                t_wall0=t_wall0,
            )
        except Exception as exc:  # noqa: BLE001 — WP1: never skip terminal fact / manifest on drain bug
            _LOG.exception(
                "event=shutdown_drain_internal_error msg=drain_aborted_best_effort error=%s",
                exc,
            )
            duration_ms = int((time.monotonic() - t_wall0) * 1000)
            msg = str(exc)
            if len(msg) > 4000:
                msg = msg[:4000] + "…"
            result = ShutdownDrainResult(
                skipped=False,
                skip_reason=None,
                canceled_order_count=0,
                residual_open_orders=(),
                timed_out=False,
                drain_duration_ms=duration_ms,
                instruments_cancelled=0,
                cancel_failures=(),
                internal_error=msg,
            )
        return self._emit_and_return(
            fact_emit=fact_emit,
            run_id=run_id,
            run_context=run_context,
            result=result,
        )

    def _live_drain_body(
        self,
        *,
        strategy: Strategy,
        strategy_id: StrategyId,
        execution_reader: NautilusExecutionStateReader,
        timeout_s: float,
        t_wall0: float,
    ) -> ShutdownDrainResult:
        try:
            open0 = execution_reader.list_open_orders_for_strategy(
                strategy_id=strategy_id,
                venue=POLYMARKET_VENUE_ID,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "event=shutdown_drain_list_open_failed phase=initial error=%s",
                exc,
                exc_info=True,
            )
            duration_ms = int((time.monotonic() - t_wall0) * 1000)
            return ShutdownDrainResult(
                skipped=False,
                skip_reason=None,
                canceled_order_count=0,
                residual_open_orders=(),
                timed_out=False,
                drain_duration_ms=duration_ms,
                instruments_cancelled=0,
                cancel_failures=(),
                internal_error=f"list_open_orders_initial:{exc!s}",
            )

        initial_count = len(open0)
        instruments = {InstrumentId.from_str(s.instrument_id) for s in open0}
        n_cancel_calls = 0
        cancel_failures: list[str] = []
        for inst in sorted(instruments, key=str):
            try:
                strategy.cancel_all_orders(inst, client_id=POLYMARKET_CLIENT_ID)
                n_cancel_calls += 1
            except Exception as exc:  # noqa: BLE001
                cancel_failures.append(f"{inst}:{exc!s}")
                _LOG.warning(
                    "event=shutdown_drain_cancel_failed instrument=%s error=%s",
                    inst,
                    exc,
                    exc_info=True,
                )

        if initial_count:
            _LOG.info(
                "event=shutdown_drain_cancel "
                "open_orders=%s instruments=%s timeout_s=%s",
                initial_count,
                n_cancel_calls,
                timeout_s,
            )

        timed_out = False
        deadline = time.monotonic() + timeout_s
        residual: tuple[OrderSnapshot, ...] = ()
        while True:
            try:
                open_now = execution_reader.list_open_orders_for_strategy(
                    strategy_id=strategy_id,
                    venue=POLYMARKET_VENUE_ID,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "event=shutdown_drain_list_open_failed phase=poll error=%s",
                    exc,
                    exc_info=True,
                )
                residual = ()
                cancel_failures.append(f"poll:{exc!s}")
                break
            if not open_now:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                residual = open_now
                _LOG.error(
                    "event=shutdown_drain_timeout residual_open=%s timeout_s=%s",
                    len(open_now),
                    timeout_s,
                )
                break
            time.sleep(self._poll_interval_s)

        duration_ms = int((time.monotonic() - t_wall0) * 1000)
        res_ids = tuple(s.client_order_id for s in residual)
        return ShutdownDrainResult(
            skipped=False,
            skip_reason=None,
            canceled_order_count=initial_count,
            residual_open_orders=res_ids,
            timed_out=timed_out,
            drain_duration_ms=duration_ms,
            instruments_cancelled=n_cancel_calls,
            cancel_failures=tuple(cancel_failures),
            internal_error=None,
        )

    def _emit_and_return(
        self,
        *,
        fact_emit: FactEmitFn | None,
        run_id: str | None,
        run_context: Any | None,
        result: ShutdownDrainResult,
    ) -> ShutdownDrainResult:
        payload = {
            "skipped": result.skipped,
            "skip_reason": result.skip_reason or "",
            "timed_out": result.timed_out,
            "residual_count": len(result.residual_open_orders),
            "canceled_count": result.canceled_order_count,
            "drain_duration_ms": result.drain_duration_ms,
            "residual_client_order_ids": list(result.residual_open_orders),
            "instruments_cancelled": result.instruments_cancelled,
            "cancel_failures": list(result.cancel_failures),
            "cancel_partial_failure": bool(result.cancel_failures),
            "internal_error": result.internal_error or "",
            "drain_aborted_internal": bool(result.internal_error),
        }
        if fact_emit is not None and run_id is not None:
            fact_emit("shutdown_drain", payload)
        if run_context is not None:
            run_context.update_manifest_fields(
                shutdown_drain_clean=result.manifest_clean(),
                shutdown_drain_skipped=result.skipped,
                shutdown_drain_skip_reason=result.skip_reason,
                shutdown_drain_timed_out=result.timed_out,
                shutdown_residual_orders=list(result.residual_open_orders),
                shutdown_drain_canceled_count=result.canceled_order_count,
                shutdown_drain_duration_ms=result.drain_duration_ms,
                shutdown_drain_instruments_cancelled=result.instruments_cancelled,
                shutdown_drain_cancel_failures=list(result.cancel_failures),
                shutdown_drain_cancel_partial_failure=bool(result.cancel_failures),
                shutdown_drain_internal_error=result.internal_error,
                shutdown_drain_aborted_internal=bool(result.internal_error),
            )
        return result
