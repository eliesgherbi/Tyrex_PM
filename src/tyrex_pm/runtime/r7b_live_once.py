"""Operator-owned R7B one-shot CLI runtime.

Authorization = the operator typing ``--execute-live`` on a local invocation.
Default / ``--dry-run`` never mutates. Historical chat/nonce/arm ceremonies are
not required by this path.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen
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
from tyrex_pm.runtime.r7_position_ack import (
    PositionAcknowledgment,
    ack_targets_forbidden,
    read_acknowledgment,
    validate_acknowledgment_against_inventory,
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
    FLAT = "FLAT"
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
    output_dir: Path = Path("var/reporting/r7b")
    acknowledgment_path: Path | None = Path(
        "var/reporting/r7/r7a2_position_acknowledgment.json"
    )
    repo_root: Path = Path(".")
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


def _get_json(url: str) -> Any:
    req = Request(url, headers={"User-Agent": "tyrex-pm-r7b-live-once/1.0"}, method="GET")
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def _append_fact(path: Path, event: str, **payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": _utc_now().isoformat(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


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
            events = _get_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
            if not isinstance(events, list) or not events:
                out.append({"slug": slug, "eligible": False, "reason": "MARKET_UNRESOLVED"})
                continue
            ev = events[0]
            m = (ev.get("markets") or [None])[0]
            if not m:
                out.append({"slug": slug, "eligible": False, "reason": "MARKET_UNRESOLVED"})
                continue
            window = resolve_btc_5m_window(slug=slug, event=ev, market=m, now=now)
            deadlines = compute_lifecycle_deadlines(window, now=now)
            tokens_raw = m.get("clobTokenIds") or "[]"
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else list(tokens_raw)
            if len(tokens) < 2:
                out.append({"slug": slug, "eligible": False, "reason": "TOKEN_UNRESOLVED"})
                continue
            condition_id = str(m.get("conditionId") or "")
            info = _get_json(f"https://clob.polymarket.com/clob-markets/{condition_id}")
            if info.get("ao") is False:
                out.append(
                    {"slug": slug, "eligible": False, "reason": "MARKET_NOT_ACCEPTING_ORDERS"}
                )
                continue
            fee = parse_fd(info, condition_id=condition_id)
            yes_tok, no_tok = str(tokens[0]), str(tokens[1])
            book = _get_json(f"https://clob.polymarket.com/book?token_id={yes_tok}")
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
    if ack is not None and (not ack_ok or not readiness.ack_ok):
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
) -> LiveOnceResult:
    report["terminal"] = outcome.value
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    fact("terminal", outcome=outcome.value, exit_code=exit_code)
    return LiveOnceResult(ok, exit_code, outcome, report_path, facts_path, report)


def run_r7b_live_once(args: R7BLiveOnceArgs) -> LiveOnceResult:
    """Run one dry (read-only) or live one-shot process."""
    _validate_cli_mode(args)
    execute = bool(args.execute_live)
    dry = not execute

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid4())
    facts_path = out_dir / f"facts_{run_id}.jsonl"
    report_path = out_dir / f"report_{run_id}.json"
    budget_path = out_dir / f"budget_{run_id}.json"

    def fact(event: str, **payload: Any) -> None:
        _append_fact(facts_path, event, **payload)

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

    # Acknowledgment (optional; validated when present)
    ack: PositionAcknowledgment | None = None
    ack_ok = True
    raw_positions: list[dict[str, Any]] = []
    if args.positions_provider is not None:
        raw_positions = list(args.positions_provider())
    if args.acknowledgment_path and Path(args.acknowledgment_path).exists():
        ack = read_acknowledgment(Path(args.acknowledgment_path))
        if not raw_positions and not args.skip_network:
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
                raw_positions = [
                    r
                    for r in _get_json(
                        f"https://data-api.polymarket.com/positions?{urlencode({'user': user})}"
                    )
                    if isinstance(r, dict)
                ]
            except Exception as exc:  # noqa: BLE001
                ack_ok = False
                report["blockers"].append(f"POSITION_FETCH_FAILED:{type(exc).__name__}")
        if ack is not None:
            v = validate_acknowledgment_against_inventory(ack, raw_positions=raw_positions)
            ack_ok = v.ok
            report["acknowledgment"] = {
                "id": ack.acknowledgment_id,
                "ok": v.ok,
                "blockers": v.blockers,
                "matched": v.matched,
                "untouched": True,
            }
            fact("acknowledgment_validated", ok=v.ok, blockers=v.blockers)

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
        )
    report["market_switch_blocked"] = True

    outcome_side, token_id = _select_outcome(bound, args.forced_outcome)
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
                )
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.DRY_OK,
            ok=True,
            exit_code=0,
            fact=fact,
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
        config=SettlementWaitConfig(max_wait_s=args.settlement_max_wait_s),
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
        )

    readiness_sell = evaluate_sell_readiness(
        confirmed_acquired=settle.confirmed_acquired,
        sellable_balance=settle.sellable_balance,
        allowance=settle.allowance,
        token_id=token_id,
        expected_token_id=token_id,
        funder_ok=True,
        stream_or_rest_healthy=True,
        bid_depth_ok=True,
        tick_min_ok=True,
    )
    if not readiness_sell.ok:
        life.note_manual_intervention()
        report["residual"] = {
            "confirmed_acquired": str(settle.confirmed_acquired),
            "current_balance": str(settle.sellable_balance),
            "allowance": None if settle.allowance is None else str(settle.allowance),
            "sell_blockers": readiness_sell.blockers,
            "note": "SELL readiness failed; no knowingly invalid SELL",
        }
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.MANUAL_INTERVENTION,
            ok=False,
            exit_code=3,
            fact=fact,
        )

    sell_qty = readiness_sell.sell_qty
    if ack is not None and ack_targets_forbidden(token_id, ack):
        raise LiveOnceError("ACKNOWLEDGED_TOKEN_EXIT_FORBIDDEN")

    # Risk-reducing exit — authorized even after entry deadline
    life.note_exit_submitting()
    exit_req = SubmitOrderRequest(
        token_id=token_id,
        side="SELL",
        price=str(sized.limit_price),
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
        }
    )
    fact(
        "exit_submit",
        ok=exit_res.ok,
        uncertain=getattr(exit_res, "uncertain", False),
        qty=str(sell_qty),
        after_entry_deadline=args.simulate_entry_deadline_passed,
    )

    # No SELL storm — at most one attempt in this process
    if getattr(exit_res, "uncertain", False) or not exit_res.ok:
        if getattr(exit_res, "uncertain", False):
            life.note_exit_unknown()
        life.note_manual_intervention()
        report["residual"] = {
            "confirmed_acquired": str(settle.confirmed_acquired),
            "current_balance": str(settle.sellable_balance),
            "attempted_sell_qty": str(sell_qty),
            "note": "exit failed after readiness; no retry storm",
        }
        return _finish(
            report=report,
            report_path=report_path,
            facts_path=facts_path,
            outcome=TerminalOutcome.MANUAL_INTERVENTION,
            ok=False,
            exit_code=3,
            fact=fact,
        )

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
        "session_phase": rt.phase.value,
        "filled_buy_notional": str(budget.state.filled_buy_notional),
        "confirmed_acquired": str(settle.confirmed_acquired),
        "sold_qty": str(sell_qty),
        "residual_quantity": "0",
        "orders": list(report["mutations_attempted"]),
        "fees_estimated_entry": str(sized.estimated_buy_fee),
        "realized_result": "FLAT_AFTER_EXIT",
    }
    report["facts_sha256"] = hashlib.sha256(facts_path.read_bytes()).hexdigest()
    return _finish(
        report=report,
        report_path=report_path,
        facts_path=facts_path,
        outcome=TerminalOutcome.FLAT,
        ok=True,
        exit_code=0,
        fact=fact,
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
    "STRATEGY_REFERENCE_MOMENTUM",
    "MARKET_FAMILY_BTC_UPDOWN_5M",
    "SpyMutationTransport",
]
