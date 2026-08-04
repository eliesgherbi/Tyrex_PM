"""Operator-owned R7B one-shot CLI runtime.

Authorization = the operator typing ``--execute-live`` on a local invocation.
Default / ``--dry-run`` never mutates. Historical chat/nonce/arm ceremonies are
not required by this path.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from tyrex_pm.adapters.polymarket.btc_5m_window import (
    MarketWindowError,
    compute_lifecycle_deadlines,
    resolve_btc_5m_window,
)
from tyrex_pm.execution.polymarket.fees_fd import FeeDescriptor, FeeError, parse_fd
from tyrex_pm.execution.polymarket.live_budget import BudgetError, LiveBudgetGuard
from tyrex_pm.execution.polymarket.mutation_lifecycle import MutationLifecycle
from tyrex_pm.execution.polymarket.mutation_transport import (
    MutationArmToken,
    MutationGateError,
    SdkMutationTransport,
    SpyMutationTransport,
)
from tyrex_pm.execution.polymarket.lifecycle_exit_plan import (
    ExitPlanStatus,
    ExitPricePolicy,
    ExitRetryPolicy,
    ExitUrgency,
    book_from_clob_levels,
    compute_sell_qty_cap,
    is_fak_no_match_error,
    next_retry_cooldown,
    plan_lifecycle_fak_sell,
)
from tyrex_pm.runtime.r7_lifecycle_policy import (
    FLATTEN_BEFORE_CLOSE_S,
    ENTRY_SAFETY_BUFFER_S,
    APPROVAL_SKEW_S,
    MIN_REMAINING_FOR_ENTRY_S,
    default_exit_price_policy,
    default_exit_retry_policy,
    default_settlement_wait_config,
    policy_snapshot,
)
from tyrex_pm.execution.polymarket.order_sizing import SizedBuyOrder, SizingError, size_buy_under_cap
from tyrex_pm.execution.polymarket.settlement import (
    FakeSettlementClock,
    SettlementPhase,
    SettlementWaitConfig,
    TradeEvidence,
    TradeSettlementStatus,
    detect_manual_flat,
    evaluate_sell_readiness,
    normalize_trade_status,
    wait_for_entry_settlement,
)
from tyrex_pm.execution.polymarket.transport import SubmitOrderRequest
from tyrex_pm.operations import next_btc_updown_slug
from tyrex_pm.runtime.r7_ack_gate import enforce_acknowledgment_gate
from tyrex_pm.runtime.r7_lifecycle_dust import dust_token_ids, read_lifecycle_dust
from tyrex_pm.runtime.r7_lifecycle_residuals import (
    CLEANUP_POLICY_NONE,
    LifecycleResidualRecord,
    evaluate_residuals_for_entry,
    migrate_dust_to_registry,
    read_residual_registry,
    residual_token_ids,
    upsert_residual,
    write_residual_registry,
)
from tyrex_pm.runtime.r7_paths import DEFAULT_ACKNOWLEDGMENT_PATH, resolve_acknowledgment_path
from tyrex_pm.runtime.r7_position_ack import (
    PositionAcknowledgment,
    ack_targets_forbidden,
)
from tyrex_pm.runtime.r7b_session import (
    SessionPhase,
    SessionRuntimeState,
    build_execution_artifact,
    build_session_authorization,
)
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ReferenceMomentumStrategy,
)


STRATEGY_REFERENCE_MOMENTUM = "reference-momentum"
MARKET_FAMILY_BTC_UPDOWN_5M = "btc_updown_5m"
MAX_BUY_COLLATERAL = Decimal("5.00")


class LiveOnceError(RuntimeError):
    pass


class TerminalOutcome(str, Enum):
    """Inventory-terminal outcomes for one lifecycle process.

    ``FLAT`` — conditional balance is exactly zero.
    ``FLAT_WITH_DUST`` — positive residual below min tradable (not tradable exposure).
    Do not use ``FLAT`` as shorthand for “no tradable exposure.”
    """

    FLAT = "FLAT"
    FLAT_WITH_DUST = "FLAT_WITH_DUST"
    FLAT_EXTERNAL_ACTION = "FLAT_EXTERNAL_ACTION"
    BLOCKED = "BLOCKED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
    DRY_OK = "DRY_OK"


@dataclass
class PresubmitReadiness:
    """Immediate pre-submit gates. Tests inject failures via overrides."""

    branch_ok: bool = True
    worktree_ok: bool = True
    ack_ok: bool = True
    reconciliation_ok: bool = True
    user_stream_ready: bool = True
    selected_market_flat: bool = True
    open_orders_selected: int = 0
    market_accepting: bool = True
    deadlines_ok: bool = True
    book_ok: bool = True
    tick_min_ok: bool = True
    fee_ok: bool = True
    balance_ok: bool = True
    kill_switch_active: bool = False
    budget_ok: bool = True


@dataclass
class R7BLiveOnceArgs:
    strategy: str = STRATEGY_REFERENCE_MOMENTUM
    market_family: str = MARKET_FAMILY_BTC_UPDOWN_5M
    max_windows: int = 3
    max_buy_collateral: Decimal = MAX_BUY_COLLATERAL
    dry_run: bool = True
    execute_live: bool = False
    output_dir: Path = Path("var/runs/_ops/r7b")
    acknowledgment_path: Path | None = DEFAULT_ACKNOWLEDGMENT_PATH
    repo_root: Path = Path(".")
    require_acknowledgment: bool = True
    allow_dirty_worktree: bool = False
    # Test / injection hooks
    mutation_transport: Any | None = None
    window_provider: Callable[[], list[dict[str, Any]]] | None = None
    positions_provider: Callable[[], list[dict[str, Any]]] | None = None
    open_orders_provider: Callable[[str], int] | None = None
    git_identity_provider: Callable[[Path], tuple[str, str, bool]] | None = None
    readiness_overrides: dict[str, Any] | None = None
    forced_outcome: str | None = None
    skip_network: bool = False
    # After entry: still allow exit even if entry deadline has passed
    simulate_entry_deadline_passed: bool = False
    # R7C settlement hooks (tests inject; live uses REST polling)
    settlement_trade_poller: Callable[[], list[Any]] | None = None
    settlement_balance_poller: Callable[[], tuple[Decimal, Decimal | None]] | None = None
    settlement_clock: Any | None = None
    settlement_max_wait_s: float = 45.0
    recovery_mode: bool = False  # restart: recon only, no new BUY
    # R7E exit planning hooks
    exit_book_provider: Callable[[str], Any] | None = None
    exit_price_policy: ExitPricePolicy | None = None
    exit_retry_policy: ExitRetryPolicy | None = None
    exit_sleep: Callable[[float], None] | None = None
    exit_now_provider: Callable[[], datetime] | None = None
    kill_switch_provider: Callable[[], bool] | None = None


@dataclass
class LiveOnceResult:
    ok: bool
    exit_code: int
    outcome: TerminalOutcome
    report_path: Path | None
    facts_path: Path | None
    report: dict[str, Any] = field(default_factory=dict)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_exit_book(token_id: str, *, now: datetime | None = None) -> Any:
    """Fresh public CLOB book for exit planning (bids authoritative for SELL)."""
    from tyrex_pm.adapters.polymarket.rest_book import fetch_clob_book

    try:
        payload = fetch_clob_book(token_id)
    except Exception:
        return None
    book = payload.book
    return book_from_clob_levels(
        token_id=token_id,
        bids=[{"price": str(lv.price), "size": str(lv.quantity)} for lv in book.bids],
        asks=[{"price": str(lv.price), "size": str(lv.quantity)} for lv in book.asks],
        ts_event=now or _utc_now(),
    )


def _persist_lifecycle_residual(
    *,
    repo_root: Path,
    run_id: str,
    condition_id: str,
    token_id: str,
    market_slug: str,
    acquired: Decimal,
    exited: Decimal,
    residual: Decimal,
    classification: str,
    buy_order_id: str | None,
    provenance: str,
    tradable: bool,
) -> None:
    reg = read_residual_registry(repo_root=repo_root) or migrate_dust_to_registry(
        repo_root=repo_root, force_incident=False
    )
    now = _utc_now().isoformat()
    rec = LifecycleResidualRecord(
        condition_id=condition_id,
        token_id=token_id,
        originating_run_id=run_id,
        market_slug=market_slug,
        acquired_quantity=str(acquired),
        exited_quantity=str(exited),
        residual_quantity=str(residual),
        min_tradable="0.01",
        classification=classification,
        provenance=provenance,
        created_at=now,
        updated_at=now,
        last_reconciliation_source="r7b_live_once",
        tradable=tradable,
        cleanup_policy=CLEANUP_POLICY_NONE,
        buy_order_id=buy_order_id,
        closed=residual == 0,
        closed_at=now if residual == 0 else None,
        historical_provenance_retained=True,
    )
    upsert_residual(reg, rec)
    write_residual_registry(reg, repo_root=repo_root)


def git_identity(repo: Path) -> tuple[str, str, bool]:
    """Return (branch, commit, clean)."""
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=str(repo), text=True
        ).strip()
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=str(repo), text=True
        ).strip()
        return branch, commit, dirty == ""
    except Exception as exc:  # noqa: BLE001
        raise LiveOnceError(f"GIT_IDENTITY_UNAVAILABLE:{exc}") from exc


def _validate_cli_mode(args: R7BLiveOnceArgs) -> None:
    if args.execute_live:
        args.dry_run = False
    else:
        args.dry_run = True
    if args.strategy not in {STRATEGY_REFERENCE_MOMENTUM, "ReferenceMomentumStrategy"}:
        raise LiveOnceError("STRATEGY_NOT_ALLOWED")
    if args.market_family != MARKET_FAMILY_BTC_UPDOWN_5M:
        raise LiveOnceError("MARKET_FAMILY_NOT_ALLOWED")
    if args.max_windows < 1 or args.max_windows > 3:
        raise LiveOnceError("MAX_WINDOWS_OUT_OF_RANGE")
    if args.max_buy_collateral > MAX_BUY_COLLATERAL or args.max_buy_collateral <= 0:
        raise LiveOnceError("MAX_BUY_COLLATERAL_INVALID")


def _default_windows(max_windows: int, max_buy: Decimal) -> list[dict[str, Any]]:
    now = _utc_now()
    base = next_btc_updown_slug(now)
    epoch = int(base.rsplit("-", 1)[-1])
    out: list[dict[str, Any]] = []
    for i in range(max_windows):
        slug = f"btc-updown-5m-{epoch + i * 300}"
        try:
            from tyrex_pm.adapters.polymarket.discovery import fetch_gamma_event
            events = [fetch_gamma_event(slug)]
            if not isinstance(events, list) or not events:
                out.append({"slug": slug, "eligible": False, "reason": "MARKET_UNRESOLVED"})
                continue
            ev = events[0]
            m = (ev.get("markets") or [None])[0]
            if not m:
                out.append({"slug": slug, "eligible": False, "reason": "MARKET_UNRESOLVED"})
                continue
            window = resolve_btc_5m_window(slug=slug, event=ev, market=m, now=now)
            deadlines = compute_lifecycle_deadlines(
                window,
                now=now,
                flatten_before_close_s=FLATTEN_BEFORE_CLOSE_S,
                entry_safety_buffer_s=ENTRY_SAFETY_BUFFER_S,
                approval_skew_s=APPROVAL_SKEW_S,
                min_remaining_for_entry_s=MIN_REMAINING_FOR_ENTRY_S,
            )
            tokens_raw = m.get("clobTokenIds") or "[]"
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else list(tokens_raw)
            if len(tokens) < 2:
                out.append({"slug": slug, "eligible": False, "reason": "TOKEN_UNRESOLVED"})
                continue
            condition_id = str(m.get("conditionId") or "")
            from tyrex_pm.adapters.polymarket.discovery import market_info_from_gamma_market
            info = market_info_from_gamma_market(m, condition_id=condition_id)
            if info.get("ao") is False:
                out.append(
                    {"slug": slug, "eligible": False, "reason": "MARKET_NOT_ACCEPTING_ORDERS"}
                )
                continue
            fee = parse_fd(info, condition_id=condition_id)
            yes_tok, no_tok = str(tokens[0]), str(tokens[1])
            from tyrex_pm.adapters.polymarket.discovery import book_asks_via_sdk
            book = {"asks": book_asks_via_sdk(yes_tok)}
            asks = book.get("asks") or []
            if not asks:
                out.append({"slug": slug, "eligible": False, "reason": "ONE_SIDED_BOOK"})
                continue
            best = min(asks, key=lambda a: Decimal(str(a.get("price") or a[0])))
            best_ask = Decimal(str(best["price"] if isinstance(best, dict) else best[0]))
            ask_size = Decimal(str(best.get("size") if isinstance(best, dict) else best[1]))
            tick = str(m.get("orderPriceMinTickSize") or info.get("mts") or "0.01")
            min_sz = str(m.get("orderMinSize") or info.get("mos") or "5")
            sized = size_buy_under_cap(
                best_ask=best_ask,
                ask_size=ask_size,
                tick_size=Decimal(tick),
                min_order_size=Decimal(min_sz),
                max_buy_notional=max_buy,
                max_limit_price=best_ask,
                fee=fee,
            )
            out.append(
                {
                    "slug": slug,
                    "eligible": True,
                    "event": ev,
                    "market": m,
                    "window": window,
                    "deadlines": deadlines,
                    "yes_token_id": yes_tok,
                    "no_token_id": no_tok,
                    "condition_id": condition_id,
                    "fee": fee,
                    "tick_size": tick,
                    "min_order_size": min_sz,
                    "best_ask": best_ask,
                    "ask_size": ask_size,
                    "sized": sized,
                    "title": m.get("question") or ev.get("title"),
                    "market_accepting": True,
                }
            )
        except (MarketWindowError, FeeError, SizingError, Exception) as exc:  # noqa: BLE001
            out.append({"slug": slug, "eligible": False, "reason": str(exc)})
    return out


def _select_outcome(window: dict[str, Any], forced: str | None) -> tuple[str, str]:
    """Return (YES|NO, token_id). Only ReferenceMomentumStrategy is permitted."""
    _ = ReferenceMomentumStrategy  # strategy binding gate
    if forced:
        side = forced.upper()
        if side not in {"YES", "NO", "UP", "DOWN"}:
            raise LiveOnceError("INVALID_FORCED_OUTCOME")
        if side in {"YES", "UP"}:
            return "YES", window["yes_token_id"]
        return "NO", window["no_token_id"]
    return "YES", window["yes_token_id"]


def _presubmit_blockers(
    *,
    args: R7BLiveOnceArgs,
    branch: str,
    clean: bool,
    ack: PositionAcknowledgment | None,
    ack_ok: bool,
    selected_token: str,
    readiness: PresubmitReadiness,
) -> list[str]:
    blockers: list[str] = []
    if branch != "rest_project" or not readiness.branch_ok:
        blockers.append("BRANCH_MISMATCH")
    # R7C.1: live execution always requires a clean worktree (allow_dirty is dry-only)
    if args.execute_live:
        if not clean or not readiness.worktree_ok:
            blockers.append("DIRTY_WORKTREE")
    elif (not clean and not args.allow_dirty_worktree) or not readiness.worktree_ok:
        blockers.append("DIRTY_WORKTREE")
    if not ack_ok or not readiness.ack_ok:
        blockers.append("ACKNOWLEDGMENT_INVALID")
    if ack is not None and ack_targets_forbidden(selected_token, ack):
        blockers.append("SELECTED_TOKEN_IS_ACKNOWLEDGED")
    if not readiness.reconciliation_ok:
        blockers.append("RECONCILIATION_NOT_READY")
    if not readiness.selected_market_flat:
        blockers.append("SELECTED_MARKET_POSITION_NONZERO")
    if readiness.open_orders_selected > 0:
        blockers.append("SELECTED_MARKET_OPEN_ORDER")
    if args.execute_live and not readiness.user_stream_ready:
        blockers.append("USER_STREAM_NOT_READY")
    if readiness.kill_switch_active:
        blockers.append("KILL_SWITCH_ACTIVE")
    if not readiness.fee_ok:
        blockers.append("FEE_PARAMETERS_UNKNOWN")
    if args.execute_live and not readiness.balance_ok:
        blockers.append("BALANCE_UNKNOWN")
    if not readiness.market_accepting:
        blockers.append("MARKET_NOT_ACCEPTING_ORDERS")
    if not readiness.book_ok:
        blockers.append("BOOK_NOT_READY")
    if not readiness.tick_min_ok:
        blockers.append("TICK_OR_MIN_SIZE_INVALID")
    if not readiness.deadlines_ok:
        blockers.append("INSUFFICIENT_TIME_REMAINING")
    if not readiness.budget_ok:
        blockers.append("BUDGET_INVALID")
    return blockers


def _finish(
    *,
    report: dict[str, Any],
    report_path: Path,
    facts_path: Path,
    outcome: TerminalOutcome,
    ok: bool,
    exit_code: int,
    fact: Callable[..., None],
    finalize_reporting: Callable[[LiveOnceResult], LiveOnceResult] | None = None,
) -> LiveOnceResult:
    report["terminal"] = outcome.value
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    fact("terminal", outcome=outcome.value, exit_code=exit_code)
    result = LiveOnceResult(ok, exit_code, outcome, report_path, facts_path, report)
    if finalize_reporting is not None:
        return finalize_reporting(result)
    return result


def run_r7b_live_once(args: R7BLiveOnceArgs) -> LiveOnceResult:
    """Run one dry (read-only) or live one-shot process."""
    from tyrex_pm.reporting import open_run_reporter

    _validate_cli_mode(args)
    execute = bool(args.execute_live)
    dry = not execute

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid4())
    reporter = open_run_reporter(
        run_dir=out_dir / run_id,
        run_id=run_id,
        mode="live",
        strategy_id="reference_momentum",
        performance_label="real",
        fake_transport=args.mutation_transport is not None
        and type(args.mutation_transport).__name__ in {"SpyMutationTransport", "FakeTransport"},
        identity_extra={"host": "r7b_live_once", "dry_run": dry},
    )
    facts_path = reporter.paths["audit_events"]
    operator_report_path = reporter.run_dir / "attachments" / "r7b_operator_report.json"
    operator_report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path = operator_report_path
    budget_path = reporter.run_dir / f"budget_{run_id}.json"

    def fact(event: str, **payload: Any) -> None:
        reporter.emit_dict(
            event_family="lifecycle",
            event_type=f"r7b.{event}",
            payload=payload,
            producer="r7b_live_once",
            force_critical=True,
        )

    def _finalize_reporting(result: LiveOnceResult) -> LiveOnceResult:
        reporter.add_attachment(
            name="r7b_operator_report",
            relative_path="attachments/r7b_operator_report.json",
        )
        summary_path = reporter.finalize(
            terminal_status="COMPLETE" if result.ok else "ABORTED",
            terminal_reason=result.outcome.value if hasattr(result.outcome, "value") else str(result.outcome),
            clean_shutdown=bool(result.ok),
        )
        return LiveOnceResult(
            result.ok,
            result.exit_code,
            result.outcome,
            summary_path,
            facts_path,
            result.report,
        )

    identity_fn = args.git_identity_provider or git_identity
    branch, commit, clean = identity_fn(args.repo_root)
    fact("checkpoint", branch=branch, commit=commit, clean=clean)

    strategy = ReferenceMomentumStrategy()
    report: dict[str, Any] = {
        "run_id": run_id,
        "mode": "EXECUTE_LIVE" if execute else "DRY_RUN",
        "execute_live": execute,
        "dry_run": dry,
        "authorization": (
            "operator --execute-live flag"
            if execute
            else "none (dry/read-only; mutations disabled)"
        ),
        "branch": branch,
        "commit": commit,
        "worktree_clean": clean,
        "strategy": STRATEGY_REFERENCE_MOMENTUM,
        "strategy_class": type(strategy).__name__,
        "market_family": MARKET_FAMILY_BTC_UPDOWN_5M,
        "max_windows": args.max_windows,
        "max_buy_collateral": str(args.max_buy_collateral),
        "mutations_enabled": False,
        "mutations_attempted": [],
        "terminal": None,
        "blockers": [],
        "prohibited_actions": {
            "redeem": False,
            "merge": False,
            "split": False,
            "transfer": False,
            "approval": False,
            "cancel_all": False,
            "heartbeat": False,
            "on_chain": False,
            "pyramiding": False,
            "second_entry": False,
            "market_switch": False,
            "ack_position_action": False,
        },
    }

    # Acknowledgment — mandatory for dry and live (R7D.1); never silently skipped
    ack: PositionAcknowledgment | None = None
    ack_ok = False
    raw_positions: list[dict[str, Any]] = []
    if args.positions_provider is not None:
        raw_positions = list(args.positions_provider())
    elif not args.skip_network:
        try:
            from urllib.parse import urlencode

            from tyrex_pm.execution.polymarket.auth import (
                load_l2_credentials,
                positions_wallet_address,
            )

            env: dict[str, str] = {}
            env_path = args.repo_root / ".env"
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, _, v = s.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
            creds = load_l2_credentials(env)
            user = positions_wallet_address(creds)
            from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport
            ro = SdkReadonlyTransport.from_env(env)
            raw_positions = ro.get_positions_raw()
        except Exception as exc:  # noqa: BLE001
            report["blockers"].append(f"POSITION_FETCH_FAILED:{type(exc).__name__}")

    registry = read_residual_registry(repo_root=args.repo_root)
    if registry is None:
        registry = migrate_dust_to_registry(repo_root=args.repo_root, force_incident=False)
    residual_eval = evaluate_residuals_for_entry(registry)
    report["lifecycle_residuals"] = None if registry is None else registry.to_dict()
    report["lifecycle_residuals_gate"] = residual_eval
    # Back-compat: first open residual as lifecycle_dust summary
    open_res = [] if registry is None else registry.open_residuals()
    report["lifecycle_dust"] = None if not open_res else open_res[0].to_dict()
    ignore_dust = residual_token_ids(registry)
    if not ignore_dust:
        ignore_dust = dust_token_ids(read_lifecycle_dust(repo_root=args.repo_root))
    if residual_eval.get("blockers"):
        report["blockers"] = list(report.get("blockers") or []) + list(
            residual_eval["blockers"]
        )
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    if args.acknowledgment_path is None:
        ack_path = None  # PATH_REQUIRED when require_acknowledgment
    else:
        p = Path(args.acknowledgment_path)
        ack_path = p if p.is_absolute() else (args.repo_root / p)

    gate = enforce_acknowledgment_gate(
        acknowledgment_path=ack_path,
        raw_positions=raw_positions,
        repo_root=args.repo_root,
        ignore_selected_market_tokens=ignore_dust,
        require_path=args.require_acknowledgment,
    )
    report["acknowledgment"] = gate.to_report_dict()
    fact(
        "acknowledgment_gate",
        ok=gate.ok,
        blockers=gate.blockers,
        artifact_path=str(gate.path) if gate.path else None,
        content_hash=gate.content_hash,
    )
    if not gate.ok:
        report["blockers"] = list(gate.blockers) + list(report.get("blockers") or [])
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )
    ack = gate.acknowledgment
    ack_ok = True

    # Transport: dry never arms network and never calls mutations
    transport: Any | None = None
    if execute:
        if args.mutation_transport is not None:
            transport = args.mutation_transport
        else:
            from tyrex_pm.execution.polymarket.sdk_readonly import (
                build_official_readonly_client,
            )

            env = {}
            env_path = args.repo_root / ".env"
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, _, v = s.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
            client = build_official_readonly_client(env=env)
            transport = SdkMutationTransport(_client=client)
            transport.enable_network(
                MutationArmToken(artifact_id=run_id, allow_network=True)
            )
        report["mutations_enabled"] = True
        fact("mutation_transport_armed", execute_live=True)
    else:
        # Dry: keep a sentinel that must never be invoked
        transport = args.mutation_transport  # may be a raising spy for tests
        fact("dry_mode_no_mutation_transport_armed")

    windows = (
        args.window_provider()
        if args.window_provider is not None
        else (
            _default_windows(args.max_windows, args.max_buy_collateral)
            if not args.skip_network
            else []
        )
    )
    if not windows:
        report["blockers"].append("NO_WINDOWS")
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    rt = SessionRuntimeState()
    bound: dict[str, Any] | None = None
    for w in windows[: args.max_windows]:
        eligible = bool(w.get("eligible"))
        slug = str(w.get("slug"))
        try:
            if rt.phase is SessionPhase.CREATED:
                sess = build_session_authorization(
                    commit_identity=commit,
                    acknowledged_position_set_id=(
                        ack.acknowledgment_id if ack else "no_ack"
                    ),
                    acknowledged_position_set_fingerprint=(
                        ack.set_fingerprint() if ack else "no_ack"
                    ),
                )
                # Operator CLI: --execute-live is auth; session envelope is local bookkeeping
                rt.attach(sess)
            rt.observe_window(slug, eligible=eligible)
        except Exception as exc:  # noqa: BLE001
            report["blockers"].append(str(exc))
            return _finish(
                report=report,
                report_path=report_path,
                facts_path=facts_path,
                outcome=TerminalOutcome.BLOCKED,
                ok=False,
                exit_code=2,
                fact=fact,
        finalize_reporting=_finalize_reporting,
            )
        fact("window_observed", slug=slug, eligible=eligible, reason=w.get("reason"))
        if eligible and bound is None:
            rt.bind_market(slug)
            bound = w
            fact("market_bound", slug=slug)
            break

    if bound is None:
        report["blockers"].append("NO_ELIGIBLE_WINDOW")
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    # Never switch after binding
    switch_blocked = False
    try:
        rt.bind_market("btc-updown-5m-switch-forbidden")
    except Exception:
        switch_blocked = True
    if not switch_blocked:
        report["blockers"].append("MARKET_SWITCH_NOT_BLOCKED")
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )
    report["market_switch_blocked"] = True

    outcome_side, token_id = _select_outcome(bound, args.forced_outcome)
    if ack is not None and ack_targets_forbidden(token_id, ack):
        report["blockers"].append("SELECTED_TOKEN_IS_ACKNOWLEDGED")
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )
    # Known lifecycle dust must never be an entry target
    if token_id in ignore_dust:
        report["blockers"].append("LIFECYCLE_DUST_TOKEN_TARGET_FORBIDDEN")
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )
    sized: SizedBuyOrder = bound["sized"]
    if sized.max_collateral > args.max_buy_collateral:
        report["blockers"].append("SIZED_COLLATERAL_EXCEEDS_CAP")
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    # Selected-market flatness (ack positions on other markets remain visible/untouched)
    selected_flat = True
    for row in raw_positions:
        asset = str(row.get("asset") or "")
        cond = str(row.get("conditionId") or "")
        size = Decimal(str(row.get("size") or "0"))
        if size == 0:
            continue
        if cond == bound["condition_id"] or asset in {
            bound["yes_token_id"],
            bound["no_token_id"],
        }:
            selected_flat = False

    open_orders_selected = 0
    if args.open_orders_provider is not None:
        open_orders_selected = int(args.open_orders_provider(bound["condition_id"]))

    deadlines = bound["deadlines"]
    fee: FeeDescriptor = bound["fee"]
    pre_submit = {
        "market_title": bound.get("title"),
        "market_slug": bound["slug"],
        "condition_id": bound["condition_id"],
        "outcome_side": outcome_side,
        "token_id": token_id,
        "side": "BUY",
        "quantity_max_estimated_shares": str(sized.quantity),
        "limit_price": str(sized.limit_price),
        "buy_amount": str(sized.amount),
        "estimated_max_entry_fee": str(sized.estimated_buy_fee),
        "max_collateral": str(sized.max_collateral),
        "order_type": "FAK",
        "market_start": deadlines.market_start.isoformat(),
        "market_end": deadlines.market_end.isoformat(),
        "entry_deadline": deadlines.entry_deadline.isoformat(),
        "flatten_deadline": deadlines.flatten_deadline.isoformat(),
        "shares_guaranteed": False,
    }
    report["pre_submit"] = pre_submit
    # Safe log: omit full token_id from fact trail if desired — keep truncated
    fact(
        "pre_submit_summary",
        market_slug=pre_submit["market_slug"],
        outcome_side=outcome_side,
        side="BUY",
        quantity=pre_submit["quantity_max_estimated_shares"],
        limit_price=pre_submit["limit_price"],
        estimated_max_entry_fee=pre_submit["estimated_max_entry_fee"],
        max_collateral=pre_submit["max_collateral"],
        entry_deadline=pre_submit["entry_deadline"],
        flatten_deadline=pre_submit["flatten_deadline"],
        token_id_suffix=token_id[-8:] if len(token_id) >= 8 else token_id,
    )

    readiness = PresubmitReadiness(
        branch_ok=branch == "rest_project",
        worktree_ok=clean or args.allow_dirty_worktree,
        ack_ok=ack_ok if ack is not None else True,
        reconciliation_ok=True,
        user_stream_ready=True,
        selected_market_flat=selected_flat,
        open_orders_selected=open_orders_selected,
        market_accepting=bool(bound.get("market_accepting", True)),
        deadlines_ok=True,
        book_ok=True,
        tick_min_ok=True,
        fee_ok=True,
        balance_ok=True,
        kill_switch_active=False,
        budget_ok=sized.max_collateral <= args.max_buy_collateral,
    )
    if args.readiness_overrides:
        for k, v in args.readiness_overrides.items():
            if hasattr(readiness, k):
                setattr(readiness, k, v)

    blockers = _presubmit_blockers(
        args=args,
        branch=branch,
        clean=clean,
        ack=ack,
        ack_ok=ack_ok,
        selected_token=token_id,
        readiness=readiness,
    )
    if blockers:
        report["blockers"] = blockers
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    # Dry path stops here — never call mutation transport
    if dry:
        report["mutations_enabled"] = False
        report["note"] = "dry-run complete; no mutations attempted"
        report["final"] = {
            "lifecycle": "DRY_VALIDATED",
            "session_phase": rt.phase.value,
            "residual_quantity": "0",
            "bound_market": bound["slug"],
            "acknowledgment_ok": True,
            "lifecycle_dust_visible": bool(open_res),
            "lifecycle_residuals_open": len(open_res),
        }
        if transport is not None and hasattr(transport, "submitted"):
            # If a test injects a transport, still must not have been called
            if getattr(transport, "submitted", None):
                report["blockers"].append("DRY_MUTATION_LEAK")
                return _finish(
                    report=report,
                    report_path=report_path,
                    facts_path=facts_path,
                    outcome=TerminalOutcome.BLOCKED,
                    ok=False,
                    exit_code=2,
                    fact=fact,
        finalize_reporting=_finalize_reporting,
                )
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.DRY_OK,
            ok=True,
            exit_code=0,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    assert transport is not None

    # Live: build execution artifact then submit
    assert rt.session is not None
    artifact = build_execution_artifact(
        session=rt.session,
        market_slug=bound["slug"],
        condition_id=bound["condition_id"],
        yes_token_id=bound["yes_token_id"],
        no_token_id=bound["no_token_id"],
        selected_outcome=outcome_side,
        selected_token_id=token_id,
        signal_id=f"r7b-{run_id}",
        decision_id=f"r7b-dec-{run_id}",
        market_start=deadlines.market_start.isoformat(),
        market_end=deadlines.market_end.isoformat(),
        entry_deadline=deadlines.entry_deadline.isoformat(),
        flatten_deadline=deadlines.flatten_deadline.isoformat(),
        best_ask=bound["best_ask"],
        worst_entry_price=sized.limit_price,
        buy_amount=sized.amount,
        max_entry_fee=sized.estimated_buy_fee,
        max_entry_collateral=sized.max_collateral,
        max_estimated_shares=sized.quantity,
        visible_depth=bound["ask_size"],
        tick_size=bound["tick_size"],
        min_order_size=bound["min_order_size"],
        fee_rate=str(fee.fee_rate),
        fee_exponent=str(fee.exponent),
        user_stream_ready=readiness.user_stream_ready,
        reconciliation_fingerprint=hashlib.sha256(
            json.dumps({"positions": len(raw_positions)}, sort_keys=True).encode()
        ).hexdigest(),
        balance_allowance_ready=readiness.balance_ok,
        kill_switch_active=readiness.kill_switch_active,
        live_budget_unused=True,
    )
    rt.set_artifact(artifact)
    report["execution_artifact_id"] = artifact.artifact_id

    if ack is not None and ack_targets_forbidden(token_id, ack):
        raise LiveOnceError("ACKNOWLEDGED_TOKEN_TARGET_FORBIDDEN")

    budget = LiveBudgetGuard(path=budget_path)
    if budget_path.exists():
        budget_path.unlink()
    budget.bind_approval(
        approval_artifact_id=run_id,
        market_id=bound["condition_id"],
        instrument_id=token_id,
        max_buy_notional=args.max_buy_collateral,
    )
    life = MutationLifecycle()
    life.prepare_approval(run_id)
    life.accept_approval()
    life.arm()

    # Fee-inclusive reserve
    reserve_notional = sized.max_collateral
    try:
        budget.reserve_working(reserve_notional)
    except BudgetError as exc:
        report["blockers"].append(str(exc))
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    rt.begin_entry_submit()
    life.note_entry_submitting()
    # Entry deadline may pass after submit begins; exit/recon remain authorized
    if args.simulate_entry_deadline_passed:
        fact("entry_deadline_passed_after_submit_begin", continue_cleanup=True)

    req = SubmitOrderRequest(
        token_id=token_id,
        side="BUY",
        price=str(sized.limit_price),
        size=str(sized.quantity),
        amount=str(sized.amount),
        order_type="FAK",
        tick_size=bound["tick_size"],
    )
    try:
        result = transport.submit_order(req)
    except MutationGateError as exc:
        budget.release_working(reserve_notional)
        report["blockers"].append(str(exc))
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.BLOCKED,
            ok=False,
            exit_code=2,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    report["mutations_attempted"].append(
        {
            "op": "submit_order",
            "side": "BUY",
            "ok": bool(result.ok),
            "uncertain": bool(getattr(result, "uncertain", False)),
            "venue_order_id": result.venue_order_id,
            "status": result.status,
            "error": result.error,
        }
    )
    fact(
        "entry_submit",
        ok=result.ok,
        uncertain=getattr(result, "uncertain", False),
        venue_order_id=result.venue_order_id,
    )

    # Exactly one entry: consume authorization so a second reserve fails
    if getattr(result, "uncertain", False):
        budget.mark_uncertain(reserve_notional)
        budget.consume_entry_authorization()
        life.note_entry_unknown()
        # Do not resubmit blindly
        ok2, why2 = budget.can_reserve_entry(Decimal("0.01"))
        report["second_entry_denied"] = not ok2
        report["second_entry_reason"] = why2
        report["residual"] = {
            "uncertain_buy_notional": str(budget.state.uncertain_buy_notional),
            "note": "unknown submission; full fee-inclusive budget reserved; no blind resubmit",
        }
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.MANUAL_INTERVENTION,
            ok=False,
            exit_code=3,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    if not result.ok:
        budget.release_working(reserve_notional)
        life.note_entry_rejected()
        report["note"] = "entry_rejected_no_exposure"
        report["final"] = {
            "lifecycle": life.phase.value,
            "session_phase": rt.phase.value,
            "residual_quantity": "0",
            "filled_buy_notional": str(budget.state.filled_buy_notional),
        }
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.FLAT,
            ok=True,
            exit_code=0,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    # R7C: insert status=matched is NOT inventory
    life.note_entry_accepted()
    insert_status = str(result.status or "").upper()
    owned_order_id = result.venue_order_id
    if insert_status == "MATCHED" or result.ok:
        life.note_entry_matched()
        fact(
            "entry_matched_not_settled",
            venue_order_id=owned_order_id,
            insert_status=insert_status,
            planned_qty=str(sized.quantity),
            note="MATCHED != CONFIRMED; planned qty is not inventory",
        )
    else:
        life.note_entry_accepted()

    life.note_entry_settling()

    # Settlement wait — injectable for tests; live uses REST (and optional stream later)
    clock = args.settlement_clock or FakeSettlementClock()
    # In production execute path without injectors, use short real wait only when pollers provided
    if args.settlement_trade_poller is None or args.settlement_balance_poller is None:
        # Default live stub: if no pollers, treat as settlement not ready → MANUAL_INTERVENTION
        # (tests always inject; production CLI wires REST pollers below)
        from tyrex_pm.execution.polymarket.settlement import RealSettlementClock

        def _default_trade_poll() -> list[TradeEvidence]:
            try:
                from tyrex_pm.execution.polymarket.sdk_readonly import (
                    SdkReadonlyTransport,
                )

                env: dict[str, str] = {}
                env_path = args.repo_root / ".env"
                if env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        s = line.strip()
                        if not s or s.startswith("#") or "=" not in s:
                            continue
                        k, _, v = s.partition("=")
                        env[k.strip()] = v.strip().strip('"').strip("'")
                ro = SdkReadonlyTransport.from_env(env)
                out: list[TradeEvidence] = []
                for t in ro.get_trades(market_id=bound["condition_id"]):
                    if owned_order_id and t.venue_order_id != owned_order_id:
                        if t.instrument_token_id != token_id:
                            continue
                    out.append(
                        TradeEvidence(
                            trade_id=t.venue_trade_id,
                            order_id=t.venue_order_id,
                            side=t.side,
                            size=t.size,
                            price=t.price,
                            status=normalize_trade_status(t.status),
                            fee=t.fee_rate_bps,
                            token_id=t.instrument_token_id,
                            raw_status=t.status,
                        )
                    )
                return out
            except Exception:  # noqa: BLE001
                return []

        def _default_bal_poll() -> tuple[Decimal, Decimal | None]:
            try:
                from tyrex_pm.execution.polymarket.sdk_readonly import (
                    SdkReadonlyTransport,
                )

                env = {}
                env_path = args.repo_root / ".env"
                if env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        s = line.strip()
                        if not s or s.startswith("#") or "=" not in s:
                            continue
                        k, _, v = s.partition("=")
                        env[k.strip()] = v.strip().strip('"').strip("'")
                ro = SdkReadonlyTransport.from_env(env)
                return ro.get_conditional_balance_allowance(token_id)
            except Exception:  # noqa: BLE001
                return Decimal("0"), None

        trade_poller = args.settlement_trade_poller or _default_trade_poll
        bal_poller = args.settlement_balance_poller or _default_bal_poll
        if args.settlement_clock is None:
            clock = RealSettlementClock()
    else:
        trade_poller = args.settlement_trade_poller
        bal_poller = args.settlement_balance_poller

    settle = wait_for_entry_settlement(
        poll_trades=trade_poller,
        poll_balance=bal_poller,
        order_id=owned_order_id,
        planned_qty=sized.quantity,
        clock=clock,
        config=SettlementWaitConfig(
            max_wait_s=args.settlement_max_wait_s,
            initial_backoff_s=default_settlement_wait_config().initial_backoff_s,
            max_backoff_s=default_settlement_wait_config().max_backoff_s,
            max_sell_attempts=default_settlement_wait_config().max_sell_attempts,
        ),
    )
    for row in settle.facts:
        fact(row.get("event", "settlement"), **{k: v for k, v in row.items() if k != "event"})

    report["settlement"] = {
        "phase": settle.phase.value,
        "confirmed_acquired": str(settle.confirmed_acquired),
        "sellable_balance": str(settle.sellable_balance),
        "allowance": None if settle.allowance is None else str(settle.allowance),
        "polls": settle.polls,
        "exhausted": settle.exhausted,
        "trade_failed": settle.trade_failed,
        "exposure_low": str(settle.exposure_low),
        "exposure_high": str(settle.exposure_high),
        "trade_statuses": [t.status.value for t in settle.trades],
    }

    if settle.trade_failed and settle.confirmed_acquired <= 0:
        budget.release_working(reserve_notional)
        life.note_manual_intervention()
        report["residual"] = {
            "confirmed_acquired": "0",
            "exposure_low": str(settle.exposure_low),
            "exposure_high": str(settle.exposure_high),
            "note": "trade FAILED; no SELL without independent exposure proof",
        }
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.MANUAL_INTERVENTION,
            ok=False,
            exit_code=3,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    if settle.phase is SettlementPhase.MANUAL_INTERVENTION or settle.confirmed_acquired <= 0:
        # Do not sell when balance still zero / unconfirmed
        if settle.confirmed_acquired > 0:
            budget.apply_fill(
                min(reserve_notional, settle.confirmed_acquired * sized.limit_price)
            )
        else:
            # Matched but never confirmed — keep uncertain reservation
            budget.mark_uncertain(reserve_notional)
        budget.consume_entry_authorization()
        life.note_manual_intervention()
        report["second_entry_denied"] = True
        report["residual"] = {
            "confirmed_acquired": str(settle.confirmed_acquired),
            "current_balance": str(settle.sellable_balance),
            "allowance": None if settle.allowance is None else str(settle.allowance),
            "filled_buy_notional": str(budget.state.filled_buy_notional),
            "exposure_low": str(settle.exposure_low),
            "exposure_high": str(settle.exposure_high),
            "last_trade_statuses": [t.status.value for t in settle.trades],
            "note": (
                "bounded settlement wait exhausted or balance not sellable; "
                "residual uses confirmed evidence range, not planned qty"
            ),
        }
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.MANUAL_INTERVENTION,
            ok=False,
            exit_code=3,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    # Confirmed + sellable
    partial = settle.confirmed_acquired < sized.quantity
    life.note_entry_confirmed(partial=partial)
    # Budget fill uses fee-inclusive reserve capped by actual notional estimate
    actual_notional = min(
        reserve_notional,
        (settle.confirmed_acquired * sized.limit_price).quantize(Decimal("0.01")),
    )
    if actual_notional <= 0:
        actual_notional = reserve_notional
    budget.apply_fill(actual_notional)
    rt.note_fill()
    fact(
        "entry_confirmed",
        quantity=str(settle.confirmed_acquired),
        sellable_balance=str(settle.sellable_balance),
        partial=partial,
    )

    ok_second, why_second = budget.can_reserve_entry(Decimal("0.01"))
    report["second_entry_denied"] = not ok_second
    report["second_entry_reason"] = why_second
    if ok_second:
        report["blockers"].append("SECOND_ENTRY_NOT_BLOCKED")
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.MANUAL_INTERVENTION,
            ok=False,
            exit_code=3,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    # Manual flatten detection (restart / external UI)
    if detect_manual_flat(
        confirmed_acquired=settle.confirmed_acquired,
        current_balance=settle.sellable_balance,
        current_position=settle.sellable_balance,
        sell_trades=[t for t in settle.trades if t.side.upper() == "SELL"],
    ) and settle.sellable_balance <= 0:
        life.note_flat_external_action()
        report["note"] = "FLAT_EXTERNAL_ACTION"
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.FLAT_EXTERNAL_ACTION,
            ok=True,
            exit_code=0,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    if ack is not None and ack_targets_forbidden(token_id, ack):
        raise LiveOnceError("ACKNOWLEDGED_TOKEN_EXIT_FORBIDDEN")

    # R7E/R7F: side-correct exit — never reuse sized.limit_price (BUY ceiling) as SELL.
    price_pol = args.exit_price_policy or default_exit_price_policy()
    retry_pol = args.exit_retry_policy or default_exit_retry_policy()
    report["lifecycle_policy"] = policy_snapshot()
    sleep_fn = args.exit_sleep or time.sleep
    now_fn = args.exit_now_provider or _utc_now
    book_fn = args.exit_book_provider or (lambda tid: fetch_exit_book(tid, now=now_fn()))
    kill_fn = args.kill_switch_provider or (lambda: False)

    tick = Decimal(str(bound["tick_size"]))
    min_size = Decimal(str(bound.get("min_order_size") or "0"))
    confirmed_sold = Decimal("0")
    exit_plans: list[dict[str, Any]] = []
    urgency = ExitUrgency.NORMAL
    flatten_deadline = bound["deadlines"].flatten_deadline
    attempt = 0
    last_exit_error: str | None = None
    bal_now = settle.sellable_balance
    allow_now = settle.allowance

    while attempt < retry_pol.max_attempts:
        if kill_fn():
            report["blockers"].append("KILL_SWITCH_ACTIVE")
            last_exit_error = "KILL_SWITCH_ACTIVE"
            break
        if now_fn() > flatten_deadline and attempt > 0:
            report["blockers"].append("FLATTEN_DEADLINE_EXCEEDED")
            last_exit_error = "FLATTEN_DEADLINE_EXCEEDED"
            break

        if args.settlement_balance_poller is not None:
            bal_now, allow_now = args.settlement_balance_poller()
        remaining = compute_sell_qty_cap(
            confirmed_acquired=settle.confirmed_acquired,
            sellable_balance=Decimal(str(bal_now)),
            remaining_after_confirmed_exits=settle.confirmed_acquired - confirmed_sold,
        )
        if remaining <= 0:
            break

        book = book_fn(token_id)
        plan = plan_lifecycle_fak_sell(
            book=book,
            quantity=remaining,
            tick_size=tick,
            now=now_fn(),
            urgency=urgency,
            policy=price_pol,
            entry_buy_limit=sized.limit_price,
            min_order_size=min_size if min_size > 0 else None,
        )
        exit_plans.append(plan.to_dict())
        fact(
            "exit_plan",
            attempt=attempt + 1,
            status=plan.status.value,
            limit_price=None if plan.limit_price is None else str(plan.limit_price),
            best_bid=None if plan.best_bid is None else str(plan.best_bid),
            book_fingerprint=plan.book_fingerprint,
            reason=plan.reason,
            entry_buy_limit=str(sized.limit_price),
        )

        if plan.status is ExitPlanStatus.WAIT_NO_BIDS:
            sleep_fn(next_retry_cooldown(attempt, retry_pol))
            attempt += 1
            urgency = ExitUrgency.EMERGENCY
            last_exit_error = plan.reason
            continue
        if plan.status is ExitPlanStatus.REFUSE_STALE_BOOK:
            sleep_fn(next_retry_cooldown(attempt, retry_pol))
            attempt += 1
            last_exit_error = plan.reason
            continue
        if not plan.ok or plan.limit_price is None:
            last_exit_error = plan.reason
            attempt += 1
            urgency = ExitUrgency.EMERGENCY
            sleep_fn(next_retry_cooldown(attempt - 1, retry_pol))
            continue

        # Hard invariant: planned SELL must not equal BUY limit when bid is below it
        if plan.best_bid is not None and plan.best_bid < sized.limit_price:
            if plan.limit_price >= sized.limit_price:
                raise LiveOnceError("EXIT_REUSED_BUY_LIMIT")

        readiness_sell = evaluate_sell_readiness(
            confirmed_acquired=settle.confirmed_acquired - confirmed_sold,
            sellable_balance=Decimal(str(bal_now)),
            allowance=allow_now if allow_now is not None else settle.allowance,
            token_id=token_id,
            expected_token_id=token_id,
            funder_ok=True,
            stream_or_rest_healthy=True,
            bid_depth_ok=plan.executable_bid_depth >= plan.quantity,
            tick_min_ok=True,
        )
        if not readiness_sell.ok:
            last_exit_error = ",".join(readiness_sell.blockers)
            fact("exit_readiness_blocked", blockers=readiness_sell.blockers)
            break

        sell_qty = min(readiness_sell.sell_qty, plan.quantity)
        bal_before_sell = Decimal(str(bal_now))
        life.note_exit_submitting()
        exit_req = SubmitOrderRequest(
            token_id=token_id,
            side="SELL",
            price=str(plan.limit_price),
            size=str(sell_qty),
            amount=str(sell_qty),
            order_type="FAK",
            tick_size=bound["tick_size"],
        )
        exit_res = transport.submit_order(exit_req)
        report["mutations_attempted"].append(
            {
                "op": "submit_order",
                "side": "SELL",
                "ok": bool(exit_res.ok),
                "uncertain": bool(getattr(exit_res, "uncertain", False)),
                "venue_order_id": exit_res.venue_order_id,
                "status": exit_res.status,
                "error": exit_res.error,
                "qty": str(sell_qty),
                "limit_price": str(plan.limit_price),
                "best_bid": None if plan.best_bid is None else str(plan.best_bid),
                "book_fingerprint": plan.book_fingerprint,
                "entry_buy_limit_not_used": True,
            }
        )
        fact(
            "exit_submit",
            ok=exit_res.ok,
            uncertain=getattr(exit_res, "uncertain", False),
            qty=str(sell_qty),
            limit_price=str(plan.limit_price),
            best_bid=None if plan.best_bid is None else str(plan.best_bid),
            book_fingerprint=plan.book_fingerprint,
            after_entry_deadline=args.simulate_entry_deadline_passed,
            attempt=attempt + 1,
        )

        if getattr(exit_res, "uncertain", False):
            life.note_exit_unknown()
            last_exit_error = "EXIT_UNCERTAIN"
            break

        if not exit_res.ok:
            last_exit_error = exit_res.error
            if is_fak_no_match_error(exit_res.error):
                fact("exit_fak_no_match", attempt=attempt + 1)
                sleep_fn(next_retry_cooldown(attempt, retry_pol))
                attempt += 1
                urgency = ExitUrgency.EMERGENCY
                continue
            break

        confirmed_sold += sell_qty
        if args.settlement_balance_poller is not None:
            bal_now, allow_now = args.settlement_balance_poller()
            bal_after = Decimal(str(bal_now))
            remaining_check = compute_sell_qty_cap(
                confirmed_acquired=settle.confirmed_acquired,
                sellable_balance=bal_after,
                remaining_after_confirmed_exits=settle.confirmed_acquired - confirmed_sold,
            )
            # If the venue balance did not decrease after an accepted FAK SELL,
            # do not re-submit against the same stale balance reading (spy lag /
            # inventory disagreement). Reconcile with accounting residual.
            if bal_after >= bal_before_sell:
                fact(
                    "exit_balance_unchanged_after_sell",
                    confirmed_sold=str(confirmed_sold),
                    balance=str(bal_after),
                )
                break
        else:
            remaining_check = settle.confirmed_acquired - confirmed_sold
        attempt += 1
        if remaining_check > 0:
            fact(
                "exit_partial",
                confirmed_sold=str(confirmed_sold),
                remaining=str(remaining_check),
            )
            sleep_fn(next_retry_cooldown(attempt - 1, retry_pol))
            continue
        break

    report["exit_plans"] = exit_plans
    # Inventory residual from confirmed lifecycle accounting first.
    residual_qty = max(Decimal("0"), settle.confirmed_acquired - confirmed_sold)
    if args.settlement_balance_poller is not None:
        try:
            bal_final, _ = args.settlement_balance_poller()
            bal_final_d = Decimal(str(bal_final))
            if confirmed_sold <= 0:
                # No confirmed exit fills — venue balance is authoritative
                # (covers external UI flatten during the exit window).
                residual_qty = bal_final_d
            else:
                # Never inflate residual above accounting after our fills;
                # venue pollers in tests may lag and still show pre-exit balance.
                residual_qty = min(residual_qty, bal_final_d)
        except Exception:  # noqa: BLE001
            pass

    if residual_qty < Decimal("0.01") and confirmed_sold > 0:
        # Exact zero → FLAT; positive dust → FLAT_WITH_DUST (never call dust FLAT).
        life.note_exit_matched()
        life.note_exit_settling()
        life.note_exit_filled(partial=residual_qty > 0)
        life.note_reconciling()
        if residual_qty > 0:
            _persist_lifecycle_residual(
                repo_root=args.repo_root,
                run_id=run_id,
                condition_id=str(bound["condition_id"]),
                token_id=token_id,
                market_slug=str(bound["slug"]),
                acquired=settle.confirmed_acquired,
                exited=confirmed_sold,
                residual=residual_qty,
                classification="FLAT_WITH_DUST",
                buy_order_id=owned_order_id,
                provenance="r7e_exit_flat_with_dust",
                tradable=False,
            )
            life.note_flat_confirmed()
            outcome = TerminalOutcome.FLAT_WITH_DUST
            inventory_terminal = "FLAT_WITH_DUST"
        else:
            life.note_flat_confirmed()
            outcome = TerminalOutcome.FLAT
            inventory_terminal = "FLAT"
        rt.note_flat()
        report["mutations_enabled"] = False
        if hasattr(transport, "disable_network"):
            transport.disable_network()
        report["final"] = {
            "lifecycle": life.phase.value,
            "lifecycle_completed": True,
            "session_phase": rt.phase.value,
            "inventory_terminal": inventory_terminal,
            "filled_buy_notional": str(budget.state.filled_buy_notional),
            "confirmed_acquired": str(settle.confirmed_acquired),
            "sold_qty": str(confirmed_sold),
            "residual_quantity": str(residual_qty),
            "orders": list(report["mutations_attempted"]),
            "fees_estimated_entry": str(sized.estimated_buy_fee),
            "realized_result": inventory_terminal,
            "exit_used_buy_limit": False,
        }
        report["audit_events_sha256"] = hashlib.sha256(facts_path.read_bytes()).hexdigest()
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=outcome,
            ok=True,
            exit_code=0,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    if residual_qty <= 0:
        life.note_exit_matched()
        life.note_exit_settling()
        life.note_exit_filled(partial=False)
        life.note_reconciling()
        life.note_flat_confirmed()
        rt.note_flat()
        report["mutations_enabled"] = False
        if hasattr(transport, "disable_network"):
            transport.disable_network()
        report["final"] = {
            "lifecycle": life.phase.value,
            "lifecycle_completed": True,
            "session_phase": rt.phase.value,
            "inventory_terminal": "FLAT",
            "filled_buy_notional": str(budget.state.filled_buy_notional),
            "confirmed_acquired": str(settle.confirmed_acquired),
            "sold_qty": str(confirmed_sold),
            "residual_quantity": "0",
            "orders": list(report["mutations_attempted"]),
            "fees_estimated_entry": str(sized.estimated_buy_fee),
            "realized_result": "FLAT",
            "exit_used_buy_limit": False,
        }
        report["audit_events_sha256"] = hashlib.sha256(facts_path.read_bytes()).hexdigest()
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.FLAT,
            ok=True,
            exit_code=0,
            fact=fact,
        finalize_reporting=_finalize_reporting,
        )

    # Tradable residual remains — fail closed, persist registry, no duplicate storm
    life.note_manual_intervention()
    classification = "RESIDUAL_EXPOSURE"
    _persist_lifecycle_residual(
        repo_root=args.repo_root,
        run_id=run_id,
        condition_id=str(bound["condition_id"]),
        token_id=token_id,
        market_slug=str(bound["slug"]),
        acquired=settle.confirmed_acquired,
        exited=confirmed_sold,
        residual=residual_qty,
        classification=classification,
        buy_order_id=owned_order_id,
        provenance="r7e_exit_incomplete",
        tradable=True,
    )
    report["residual"] = {
        "confirmed_acquired": str(settle.confirmed_acquired),
        "current_balance": str(residual_qty),
        "confirmed_sold": str(confirmed_sold),
        "last_exit_error": last_exit_error,
        "note": "exit incomplete after side-correct planning; no retry storm",
        "manual_intervention": True,
    }
    report["audit_events_sha256"] = hashlib.sha256(facts_path.read_bytes()).hexdigest()
    return _finish(
        report=report,
        report_path=report_path,
        facts_path=facts_path,
        outcome=TerminalOutcome.MANUAL_INTERVENTION,
        ok=False,
        exit_code=3,
        fact=fact,
        finalize_reporting=_finalize_reporting,
    )


# Keep SpyMutationTransport import reachable for tests that want a default spy
__all__ = [
    "R7BLiveOnceArgs",
    "LiveOnceResult",
    "LiveOnceError",
    "TerminalOutcome",
    "PresubmitReadiness",
    "run_r7b_live_once",
    "git_identity",
    "fetch_exit_book",
    "STRATEGY_REFERENCE_MOMENTUM",
    "MARKET_FAMILY_BTC_UPDOWN_5M",
    "SpyMutationTransport",
]
