"""Allocation-finality wait helper (P4.5 live wiring).

Waits for CONFIRMED / allocation-final evidence after a live BUY submit.
Procedural now; event-ready later via ``on_trade_event(TradeFillRecord)``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import monotonic_s, utc_now
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.state import fill_state


@dataclass(frozen=True)
class FinalityWaitResult:
    final: bool
    status: str | None
    source: str
    waited_s: float
    evidence: dict


def _find_confirmed_buy(
    coord,
    *,
    token_id: TokenId,
    since: datetime | None = None,
) -> tuple[str | None, str, dict]:
    """Return (status, source, evidence) for allocation-final BUY if found."""
    wallet = coord.wallet
    for rec in reversed(wallet.trade_fill_records):
        if rec.token_id != token_id or rec.side != Side.BUY:
            continue
        if since is not None and rec.ts_utc < since:
            continue
        status = str(rec.status).upper()
        if fill_state.is_allocation_final(status):
            return status, rec.source or "user_ws", {
                "trade_status": status,
                "trade_size": str(rec.size),
                "trade_price": str(rec.price),
                "trade_ts": rec.ts_utc.isoformat(),
            }
    pos = wallet.positions.get(token_id)
    if pos is not None and pos.qty > 0:
        return "CONFIRMED", "wallet_position", {
            "position_qty": str(pos.qty),
            "position_avg_price": str(pos.avg_price_usd),
        }
    latest_status: str | None = None
    for rec in reversed(wallet.trade_fill_records):
        if rec.token_id == token_id and rec.side == Side.BUY:
            latest_status = str(rec.status).upper()
            break
    return latest_status, "pending", {"latest_trade_status": latest_status}


async def wait_for_allocation_final(
    coord,
    *,
    token_id: TokenId,
    timeout_s: float,
    poll_interval_s: float = 0.5,
    apply_local_shadow_fill: bool = False,
    since: datetime | None = None,
    sink=None,
    run_id: str | None = None,
    correlation_id: str | None = None,
) -> FinalityWaitResult:
    """Poll until allocation-final BUY evidence or timeout."""
    start = monotonic_s()
    if apply_local_shadow_fill:
        waited = monotonic_s() - start
        result = FinalityWaitResult(
            final=True,
            status="CONFIRMED",
            source="shadow_instant",
            waited_s=waited,
            evidence={"shadow_instant_fill": True},
        )
        _emit_wait(sink, run_id, correlation_id, result)
        return result

    deadline = start + timeout_s
    last_evidence: dict = {}
    while monotonic_s() < deadline:
        status, source, evidence = _find_confirmed_buy(
            coord, token_id=token_id, since=since
        )
        last_evidence = evidence
        if status is not None and fill_state.is_allocation_final(status):
            waited = monotonic_s() - start
            result = FinalityWaitResult(
                final=True,
                status=status,
                source=source,
                waited_s=waited,
                evidence=evidence,
            )
            _emit_wait(sink, run_id, correlation_id, result)
            return result
        await asyncio.sleep(poll_interval_s)

    waited = monotonic_s() - start
    status, source, evidence = _find_confirmed_buy(coord, token_id=token_id, since=since)
    last_evidence = evidence or last_evidence
    final = status is not None and fill_state.is_allocation_final(status)
    result = FinalityWaitResult(
        final=final,
        status=status,
        source=source if final else "timeout",
        waited_s=waited,
        evidence=last_evidence,
    )
    _emit_wait(sink, run_id, correlation_id, result)
    return result


def _emit_wait(sink, run_id: str | None, correlation_id: str | None, result: FinalityWaitResult) -> None:
    if sink is None or run_id is None:
        return
    payload = {
        "event": "finality_wait",
        "final": result.final,
        "status": result.status,
        "source": result.source,
        "waited_s": round(result.waited_s, 3),
        **result.evidence,
    }
    sink.write(make_fact(FACT_TYPE_HEALTH, run_id, payload, correlation_id=correlation_id))
