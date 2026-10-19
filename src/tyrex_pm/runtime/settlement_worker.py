"""Automatic resolution and payout tracking for committed holdings (continuous runner).

The worker periodically runs ``ResolutionManager.reconcile`` over the durable settlement
ledger: committed holdings whose condition has a *finalized* on-chain payout move to
``PAYOUT_PROVEN`` and then either complete (zero payout) or join the operator
redemption worklist. Redemption itself stays an operator action in live; a payout
receivable is never counted as available cash (the governor's unredeemed caps and the
account's collateral read apply). In paper mode an injected redeemer may simulate the
operator step after a delay so the whole capital cycle can be exercised.

Ownership: the worker runs inside the trading process. When the process stops, the
ledger keeps every unresolved episode and its worklist; ``tyrex-pm settlement
reconcile`` (operator) or the next runner start continues from there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tyrex_pm.execution.settlement.ledger import EpisodeState
from tyrex_pm.execution.settlement.resolution import ResolutionManager

# (episode) -> tx reference once the simulated operator redemption is done, else None.
PaperRedeemer = Callable[[Any], Awaitable[str | None]]


@dataclass
class SettlementWorker:
    manager: ResolutionManager
    interval_s: float
    record: Callable[[str, dict], None]
    paper_redeemer: PaperRedeemer | None = None
    # Automatic recognition of UI redemptions (read-only; settlement/redemption_discovery).
    discovery: Any | None = None
    runs: int = 0
    last_report: dict = field(default_factory=dict)
    _task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def once(self) -> dict:
        report = await self.manager.reconcile()
        redeemed: list[str] = []
        if self.paper_redeemer is not None:
            for ep in list(self.manager.ledger.episodes.values()):
                if ep.state is not EpisodeState.REDEMPTION_PENDING_OPERATOR:
                    continue
                tx = await self.paper_redeemer(ep)
                if tx is None:
                    continue
                ok, reasons = await self.manager.confirm_redemption(ep.episode_id, tx)
                if ok:
                    redeemed.append(ep.episode_id)
                else:
                    self.record("PAPER_REDEMPTION_REFUSED",
                                {"episode_id": ep.episode_id, "reasons": list(reasons)})
        discovered: dict | None = None
        if self.discovery is not None and (self.runs == 0 or self.discovery.due()):
            try:
                discovered = await self.discovery.run_once(apply=True)
            except Exception as exc:  # noqa: BLE001 - discovery never stops the worker
                discovered = {"error": f"{type(exc).__name__}: {exc}"}
                self.record("REDEMPTION_DISCOVERY_ERROR", discovered)
        self.runs += 1
        totals = self.manager.ledger.totals()
        self.last_report = {
            "at": datetime.now(timezone.utc).isoformat(),
            "proven": list(report.proven),
            "completed_zero_payout": list(report.completed_zero_payout),
            "uncertain": dict(report.uncertain),
            "paper_redeemed": redeemed,
            "unresolved_count": totals.unresolved_count,
            "unredeemed_count": totals.unredeemed_count,
            "unredeemed_cost": str(totals.unredeemed_cost),
            "realized_pnl": str(totals.realized_pnl),
            "realized_losses": str(totals.realized_losses),
            "winning_receivable": str(totals.winning_receivable),
            "winning_receivable_cost": str(totals.winning_receivable_cost),
            "redeemed_cash": str(totals.redeemed_cash),
            "worklist": len(self.manager.worklist()),
        }
        if discovered is not None:
            self.last_report["redemption_discovery"] = {
                k: discovered.get(k) for k in ("scanned", "applied", "unresolved", "skipped",
                                               "scan_error", "error")}
        applied = bool(discovered and discovered.get("applied"))
        if report.proven or report.completed_zero_payout or redeemed or applied:
            self.record("SETTLEMENT_RECONCILED", self.last_report)
        return self.last_report

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.once()
            except Exception as exc:  # noqa: BLE001 - provider failure is recorded, not fatal
                self.record("SETTLEMENT_WORKER_ERROR",
                            {"error_type": type(exc).__name__, "message": str(exc)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="settlement-worker")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        totals = self.manager.ledger.totals()
        self.record(
            "SETTLEMENT_WORKER_STOPPED",
            {
                "runs": self.runs,
                "unresolved_count": totals.unresolved_count,
                "unredeemed_count": totals.unredeemed_count,
                "owner_after_stop": "settlement ledger worklist (operator / next run)",
            },
        )
