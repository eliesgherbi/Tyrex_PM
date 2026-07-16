"""Protection runtime wiring (P4.5 architecture_enhance).

Initializes :class:`ProtectionMonitor`, registers after allocation-final BUY fills,
and routes triggered exits through the generic pipeline.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent
from tyrex_pm.protection.config import ProtectionPolicy
from tyrex_pm.protection.monitor import ProtectionMonitor
from tyrex_pm.protection.registry import ProtectionRegistry
from tyrex_pm.runtime.allocation_runtime import resolve_owner_id
from tyrex_pm.runtime.config import AppConfig, ProtectionRuntimeConfig
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.state import fill_state

log = logging.getLogger(__name__)


def init_protection_monitor(coord) -> ProtectionMonitor | None:
    existing = getattr(coord, "protection_monitor", None)
    if existing is not None:
        return existing
    mon = ProtectionMonitor(ProtectionRegistry())
    coord.protection_monitor = mon
    return mon


def protection_policy_from_config(cfg: ProtectionRuntimeConfig) -> ProtectionPolicy:
    return ProtectionPolicy(
        take_profit_pct=cfg.take_profit_pct,
        stop_loss_pct=cfg.stop_loss_pct,
        take_profit_price=cfg.take_profit_price,
        stop_loss_price=cfg.stop_loss_price,
        size_mode=cfg.size_mode,
        fixed_size=cfg.fixed_size,
        percent=cfg.percent,
        exit_order_style=cfg.exit_order_style,
        exit_limit_price=cfg.exit_limit_price,
        max_book_age_s=cfg.max_book_age_s,
    )


def fill_status_for_protection_registration(
    *,
    apply_local_shadow_fill: bool,
    match_evidence: dict,
) -> str:
    """Map submit evidence to a venue fill status for registration gating."""
    if apply_local_shadow_fill:
        return "CONFIRMED"
    raw = match_evidence.get("match_status") or match_evidence.get("status") or ""
    return str(raw).upper() or "LIVE"


def maybe_register_protection_after_buy(
    coord,
    app: AppConfig,
    *,
    strategy: object,
    ap: ApprovedIntent,
    match_evidence: dict,
    correlation_id: str,
    intent_extensions: dict | None,
    run_id: str,
    apply_local_shadow_fill: bool,
) -> None:
    """Register TP/SL after a BUY when fill evidence is allocation-final (CONFIRMED).

    ``allocation_buy_applied`` from MATCHED-only submit ack is not sufficient.
    When ``validation.wait_for_confirmed`` is set (live Level 5), registration is
    deferred to ``register_protection_after_finality`` after FinalityWaiter.
    """
    prot = app.protection
    if prot is None or not prot.enabled or not prot.register_on_buy:
        return
    vh = app.validation_harness
    if (
        vh is not None
        and vh.wait_for_confirmed
        and not apply_local_shadow_fill
    ):
        return
    intent = ap.intent
    if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
        return
    mon = getattr(coord, "protection_monitor", None)
    if mon is None:
        mon = init_protection_monitor(coord)
    if mon is None:
        return
    owner_id = resolve_owner_id(strategy, intent, intent_extensions=intent_extensions)
    entry_price = intent.limit_price
    if entry_price is None or entry_price <= 0:
        return
    status = fill_status_for_protection_registration(
        apply_local_shadow_fill=apply_local_shadow_fill,
        match_evidence=match_evidence,
    )
    if not fill_state.is_allocation_final(status):
        log.debug(
            "protection registration skipped: status=%s not allocation-final",
            status,
        )
        return
    policy = protection_policy_from_config(prot)
    if not policy.has_triggers():
        return
    sink = coord.allocation_ledger_sink
    rid = RunId(str(run_id))
    mon.register(
        owner_id=owner_id,
        token_id=intent.token_id,
        entry_price=entry_price,
        policy=policy,
        parent_correlation_id=correlation_id,
        sink=sink,
        run_id=rid,
    )


def register_protection_manual(
    coord,
    app: AppConfig,
    *,
    owner_id: str,
    token_id: TokenId,
    entry_price: Decimal,
    correlation_id: str,
    run_id: RunId,
    sink,
) -> None:
    """Explicit registration for shadow harness trigger modes only."""
    from tyrex_pm.core.enums import ExecutionMode

    if app.runtime.execution_mode == ExecutionMode.LIVE:
        log.error("register_protection_manual is forbidden in live mode")
        return
    prot = app.protection
    if prot is None or not prot.enabled:
        return
    mon = init_protection_monitor(coord)
    assert mon is not None
    policy = protection_policy_from_config(prot)
    mon.register(
        owner_id=owner_id,
        token_id=token_id,
        entry_price=entry_price,
        policy=policy,
        parent_correlation_id=correlation_id,
        sink=sink,
        run_id=run_id,
    )


def register_protection_after_finality(
    coord,
    app: AppConfig,
    *,
    owner_id: str,
    token_id: TokenId,
    entry_price: Decimal,
    correlation_id: str,
    run_id: RunId,
    sink,
    finality_status: str,
) -> bool:
    """Register protection only when ``fill_state.is_allocation_final(status)``."""
    if not fill_state.is_allocation_final(finality_status):
        log.debug(
            "register_protection_after_finality skipped: status=%s not allocation-final",
            finality_status,
        )
        return False
    prot = app.protection
    if prot is None or not prot.enabled:
        return False
    mon = init_protection_monitor(coord)
    if mon is None:
        return False
    policy = protection_policy_from_config(prot)
    if not policy.has_triggers():
        return False
    mon.register(
        owner_id=owner_id,
        token_id=token_id,
        entry_price=entry_price,
        policy=policy,
        parent_correlation_id=correlation_id,
        sink=sink,
        run_id=run_id,
    )
    return True


def register_protection_from_venue_position(
    coord,
    app: AppConfig,
    *,
    owner_id: str,
    token_id: TokenId,
    correlation_id: str,
    run_id: RunId,
    sink,
    entry_price: Decimal | None = None,
) -> bool:
    """Live Level 6: register from real venue position (not fixture/seed)."""
    pos = coord.wallet.positions.get(token_id)
    if pos is None or pos.qty <= 0:
        log.error("cannot register protection: no venue position for %s", token_id)
        return False
    price = entry_price if entry_price is not None and entry_price > 0 else pos.avg_price_usd
    if price is None or price <= 0:
        log.error("cannot register protection: no entry price for %s", token_id)
        return False
    return register_protection_after_finality(
        coord,
        app,
        owner_id=owner_id,
        token_id=token_id,
        entry_price=price,
        correlation_id=correlation_id,
        run_id=run_id,
        sink=sink,
        finality_status=fill_state.STATUS_CONFIRMED,
    )


async def process_protection_work_units(
    work_units: list[IntentWorkUnit],
    *,
    app: AppConfig,
    run_id: RunId,
    strategy: object,
    coord,
    sink,
    oms,
    apply_local_shadow_fill: bool = True,
    live_clob_client: object | None = None,
) -> None:
    from tyrex_pm.runtime.pipeline import process_intent_work_unit

    for work in work_units:
        await process_intent_work_unit(
            work,
            app=app,
            run_id=run_id,
            strategy=strategy,
            coord=coord,
            sink=sink,
            oms=oms,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )


async def run_protection_tick(
    coord,
    app: AppConfig,
    *,
    run_id: RunId,
    strategy: object,
    sink,
    oms,
    apply_local_shadow_fill: bool = True,
    live_clob_client: object | None = None,
) -> list[IntentWorkUnit]:
    mon = getattr(coord, "protection_monitor", None)
    if mon is None:
        return []
    work = mon.tick(coord, sink=sink, run_id=run_id)
    if work:
        await process_protection_work_units(
            work,
            app=app,
            run_id=run_id,
            strategy=strategy,
            coord=coord,
            sink=sink,
            oms=oms,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )
    return work
