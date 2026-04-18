from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import VenueOrderId
from tyrex_pm.core.models import OpenOrderView, TradeFillRecord
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.wallet_store import WalletStore

#: Operator-facing summary of provisional repair + drift policy.
RECONCILE_POLICY_SUMMARY = (
    "user_ws=primary; rest=repair_backstop; local_oms=provisional_only; "
    "provisional_pending_venue=non_blocking_within_submit_grace_s_when_ws_fresh; "
    "provisional_filled_resolved=ws_trade_evidence_covers_original_size→drop_with_audit; "
    "provisional_unknown_terminal=absent_past_unknown_terminal_timeout_s_when_ws_fresh_and_no_restart→drop_with_audit_non_blocking; "
    "provisional_absent_blocked=age>submit_grace_s_and_below_unknown_terminal_timeout_s→blocking_local_open_not_on_venue; "
    "provisional_absent_ws_stale_or_restart=NEVER_auto_resolve→stay_blocking; "
    "venue_confirmed_absent_when_ws_fresh=pruned_no_drift_for_that_id (UI cancel/full fill); "
    "venue_confirmed_drift=strict_remaining_vs_remaining_original_vs_original; "
    "venue_unmatched_adoption=strong_match_token+side+size+price_within_adoption_grace_s→adopt_vid_onto_no_vid_provisional; "
    "venue_unmatched_defer=weak_candidate_token+side_no_size_or_price_match_within_grace→non_blocking_until_grace_expires; "
    "venue_unmatched_blocked=no_recent_local_candidate_OR_grace_expired→blocking_venue_open_not_tracked_locally"
)


#: Default tolerances for venue-adoption matching.
ADOPTION_SIZE_REL_TOL = Decimal("0.01")  # 1% relative size tolerance (covers cap rounding etc.)
ADOPTION_SIZE_ABS_TOL = Decimal("0.5")   # absolute size floor (Decimal contracts vs venue rounding)
ADOPTION_PRICE_ABS_TOL = Decimal("0.005")  # 0.5¢ on a 0–1 probability venue is generous


@dataclass
class ReconcileResult:
    #: All drift / informational flags (facts / observability).
    drift_flags: tuple[str, ...]
    #: Subset that should fail-closed new live risk (excludes provisional grace / repair-resolved).
    blocking_drift_flags: tuple[str, ...]
    #: Worst case across **blocking** flags: none | transient_venue_lag_candidate | size_mismatch | structural.
    reconcile_severity: str
    order_comparisons: tuple[dict[str, Any], ...] = ()
    #: Venue-confirmed ids dropped because they vanished from a fresh merged book (UI cancel / fill).
    pruned_terminal_venue_order_ids: tuple[str, ...] = ()
    #: Per-provisional-row repair decisions (observability for live runs).
    provisional_repair_decisions: tuple[dict[str, Any], ...] = ()
    #: Back-compat alias: provisional rows that were terminalized this pass.
    provisional_timeout_resolutions: tuple[dict[str, Any], ...] = ()
    #: Per-venue-row adoption decisions for venue ids not yet linked to a local OMS row
    #: (REST-ahead-of-local-registration race). Each entry records the candidate considered,
    #: matching attributes, and the decision (adopted / deferred / blocked).
    venue_adoption_decisions: tuple[dict[str, Any], ...] = ()
    policy_summary: str = RECONCILE_POLICY_SUMMARY


def remaining_sizes_equivalent(a: Decimal, b: Decimal) -> bool:
    """
    Venue strings + local Decimal math can differ at the last ULP; treat near-equal as match.
    Still fails on materially different rests (fail-closed for real drift).
    """
    if a == b:
        return True
    diff = abs(a - b)
    scale = max(abs(a), abs(b), Decimal("1"))
    abs_tol = Decimal("1e-10")
    rel_tol = Decimal("1e-9")
    return diff <= max(abs_tol, rel_tol * scale)


def _severity_for_blocking(flags: frozenset[str]) -> str:
    if not flags:
        return "none"
    if flags <= frozenset({"local_open_not_on_venue"}):
        return "transient_venue_lag_candidate"
    if "open_order_size_mismatch" in flags or "open_order_original_mismatch" in flags:
        return "size_mismatch"
    return "structural"


def _ack_age_s(lo: LocalOrder, now: datetime) -> float | None:
    if lo.submit_ack_utc is None:
        return None
    ack = lo.submit_ack_utc
    if ack.tzinfo is None:
        ack = ack.replace(tzinfo=timezone.utc)
    return (now - ack).total_seconds()


def _trade_fill_evidence(
    lo: LocalOrder,
    trades: list[TradeFillRecord],
) -> tuple[bool, Decimal, dict[str, Any]]:
    """Cumulative size from CONFIRMED user-WS trades on same token+side after submit ack."""
    matched = Decimal("0")
    last_status: str | None = None
    last_ts: datetime | None = None
    if lo.submit_ack_utc is None or lo.original_size is None:
        return False, matched, {
            "matched_size": "0",
            "matched_status": None,
            "matched_last_ts_utc": None,
        }
    ack = lo.submit_ack_utc
    if ack.tzinfo is None:
        ack = ack.replace(tzinfo=timezone.utc)
    for t in trades:
        if t.token_id != lo.token_id or t.side != lo.side:
            continue
        ts = t.ts_utc
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < ack:
            continue
        if t.status not in ("MATCHED", "MINED", "CONFIRMED"):
            continue
        matched += t.size
        last_status = t.status
        last_ts = ts
    covered = matched >= lo.original_size and matched > 0
    return covered, matched, {
        "matched_size": str(matched),
        "matched_status": last_status,
        "matched_last_ts_utc": last_ts.isoformat() if last_ts else None,
    }


def _resolve_provisional_repair(
    order_store: OrderStore,
    wallet: WalletStore,
    venue_views: dict[str, OpenOrderView],
    *,
    venue_user_ws_stale: bool,
    venue_restart_suspected: bool,
    submit_grace_s: float,
    unknown_terminal_timeout_s: float,
    now: datetime,
) -> list[dict[str, Any]]:
    """Repair state machine for provisional rows (run before drift comparisons).

    For each provisional ``LocalOrder``, decide one of:
      * ``confirmed_open_order``     — venue snapshot has the id (no row change here; later compare loop confirms).
      * ``filled_resolved``          — WS trade evidence covers original size → drop row + audit.
      * ``unknown_terminal``         — ack age ≥ ``unknown_terminal_timeout_s``, ws fresh, no restart,
                                       still absent from venue → drop row + audit (non-blocking).
      * ``pending_within_grace``     — ack age ≤ ``submit_grace_s`` (no decision; non-blocking later).
      * ``blocked_absent``           — past grace but below terminal timeout (still blocking later).
      * ``blocked_unsafe_to_resolve``— WS stale OR venue restart suspected (never auto-resolve).
    """
    decisions: list[dict[str, Any]] = []
    for cid, lo in list(order_store.orders.items()):
        if lo.confirmation != "provisional":
            continue
        vid = str(lo.venue_order_id) if lo.venue_order_id is not None else None
        ws_seen = vid in venue_views and (
            venue_views[vid].venue_state_source in (None, "user_ws")
        ) if vid is not None else False
        rest_seen = vid in venue_views and (
            venue_views[vid].venue_state_source == "rest"
        ) if vid is not None else False
        present_in_book = vid is not None and vid in venue_views

        age_s = _ack_age_s(lo, now)

        covered, matched_size, trade_meta = _trade_fill_evidence(
            lo, wallet.trade_fill_records
        )

        decision: str
        decision_reason: str
        blocking = False
        terminal_dropped = False

        if present_in_book:
            decision = "confirmed_open_order"
            decision_reason = "merged_book_has_id_will_align_in_compare"
        elif covered:
            decision = "filled_resolved"
            decision_reason = "ws_trade_evidence_covers_original_size"
            terminal_dropped = True
        elif venue_user_ws_stale or venue_restart_suspected:
            decision = "blocked_unsafe_to_resolve"
            decision_reason = (
                "ws_stale_or_venue_restart_suspected_skip_auto_resolution"
            )
            blocking = True
        elif age_s is None:
            # Conservative: no ack timestamp = no age signal. Never auto-terminalize without one;
            # treat as past submit_grace_s so it surfaces as ``local_open_not_on_venue`` (blocking).
            decision = "blocked_absent"
            decision_reason = (
                "no_submit_ack_timestamp_so_age_unknown_block_until_explicit_evidence"
            )
            blocking = True
        elif age_s <= submit_grace_s:
            decision = "pending_within_grace"
            decision_reason = "within_submit_grace_s_non_blocking"
        elif age_s >= unknown_terminal_timeout_s:
            decision = "unknown_terminal"
            decision_reason = (
                "absent_from_ws_and_rest_past_unknown_terminal_timeout_s_with_healthy_venue_truth"
            )
            terminal_dropped = True
        else:
            decision = "blocked_absent"
            decision_reason = (
                "past_submit_grace_s_below_unknown_terminal_timeout_s_local_open_not_on_venue"
            )
            blocking = True

        position_after = wallet.positions.get(lo.token_id)
        # NOTE: WalletPosition uses ``qty`` (see core/models.py); ``size`` was a typo that
        # stayed dormant while wallet.positions was usually empty in LIVE. The Fix-2 REST
        # positions safety net now reliably populates this map, so any provisional row whose
        # token has a held position would crash here on AttributeError until corrected.
        position_size = (
            str(position_after.qty) if position_after is not None else None
        )

        record: dict[str, Any] = {
            "client_order_id": str(cid),
            "venue_order_id": vid,
            "submit_fingerprint": lo.submit_fingerprint,
            "ack_status": lo.ack_status,
            "ack_age_s": round(age_s, 3) if age_s is not None else None,
            "user_ws_fresh": (not venue_user_ws_stale),
            "venue_restart_suspected": venue_restart_suspected,
            "ws_order_seen": ws_seen,
            "rest_open_order_found": rest_seen,
            "ws_trade_seen": matched_size > 0,
            "ws_trade_matched_size": trade_meta["matched_size"],
            "ws_trade_status": trade_meta["matched_status"],
            "ws_trade_last_ts_utc": trade_meta["matched_last_ts_utc"],
            "rest_get_order_found": None,
            "rest_recent_trade_found": None,
            "position_size_after": position_size,
            "balance_after": (
                str(wallet.usdc_balance) if wallet.usdc_balance is not None else None
            ),
            "repair_attempt": lo.repair_attempts + 1,
            "submit_grace_s": submit_grace_s,
            "unknown_terminal_timeout_s": unknown_terminal_timeout_s,
            "decision": decision,
            "decision_reason": decision_reason,
            "blocking": blocking,
            "terminal_dropped": terminal_dropped,
            "resolved_at_utc": now.isoformat(),
        }

        if terminal_dropped:
            order_store.orders.pop(cid, None)
            if lo.submit_fingerprint:
                order_store.pending_repair_fingerprints.discard(lo.submit_fingerprint)
            order_store.record_terminal_audit(record)
        else:
            lo.repair_attempts += 1

        decisions.append(record)

    return decisions


def _row_age_s(lo: LocalOrder, now: datetime) -> float | None:
    """Best available age signal for a local row.

    Prefers ``submit_ack_utc`` (we know the venue accepted it). Falls back to ``register_utc``
    so adoption can still apply when ``ack_submit`` was bypassed (e.g. response parse missed
    the order id and the row never received an ack timestamp).
    """
    src = lo.submit_ack_utc or lo.register_utc
    if src is None:
        return None
    if src.tzinfo is None:
        src = src.replace(tzinfo=timezone.utc)
    return (now - src).total_seconds()


def _sizes_match_within_tol(local: Decimal, venue: Decimal) -> bool:
    if local == venue:
        return True
    diff = abs(local - venue)
    rel = diff / max(abs(local), abs(venue), Decimal("1"))
    return diff <= ADOPTION_SIZE_ABS_TOL or rel <= ADOPTION_SIZE_REL_TOL


def _prices_match_within_tol(local: Decimal | None, venue: Decimal | None) -> bool:
    """Return True iff both prices are present and within absolute tolerance.

    A missing local or venue price is treated as ``False`` so adoption never relies on
    a one-sided price guess.
    """
    if local is None or venue is None:
        return False
    return abs(local - venue) <= ADOPTION_PRICE_ABS_TOL


def _adopt_venue_id_into_no_vid_provisional(
    order_store: OrderStore,
    target_lo: LocalOrder,
    venue_view: OpenOrderView,
    now: datetime,
) -> None:
    """In-place: set venue_order_id on a no-vid provisional row + persist ack timestamp.

    Mirrors ``execution.order_lifecycle.attach_venue_order_id_to_local`` but kept inline so
    the reconcile pass has no cross-package import cycle. The row stays ``provisional`` —
    later ``sync_local_open_orders_from_venue_wallet`` upgrades it to ``venue_confirmed`` once
    the merged book confirms remaining/original sizes.
    """
    cid = target_lo.client_order_id
    vid = venue_view.venue_order_id
    if vid is None:
        return
    new_vid = VenueOrderId(str(vid))
    order_store.orders[cid] = LocalOrder(
        client_order_id=target_lo.client_order_id,
        venue_order_id=new_vid,
        token_id=target_lo.token_id,
        side=target_lo.side,
        remaining=target_lo.remaining,
        original_size=target_lo.original_size,
        size_matched=target_lo.size_matched,
        confirmation=target_lo.confirmation,
        submit_ack_utc=(
            target_lo.submit_ack_utc if target_lo.submit_ack_utc is not None else now
        ),
        last_local_source="venue_adoption",
        submit_fingerprint=target_lo.submit_fingerprint,
        ack_status=target_lo.ack_status,
        repair_attempts=target_lo.repair_attempts,
        limit_price=target_lo.limit_price,
        register_utc=target_lo.register_utc,
    )


def _resolve_venue_unmatched_orders(
    order_store: OrderStore,
    venue_views: dict[str, OpenOrderView],
    local_by_vid: dict[str, LocalOrder],
    no_vid_locals: list[LocalOrder],
    *,
    venue_user_ws_stale: bool,
    venue_restart_suspected: bool,
    adoption_grace_s: float,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Decide what to do with venue order ids that are not (yet) tracked locally.

    Returns:
        (decisions, decision_by_vid)
        - decisions: ordered list of per-venue-id observability records.
        - decision_by_vid: lookup so the caller's drift loop can apply the decision
          (adopt → no flag; defer → non-blocking flag; block → blocking flag).
    """
    decisions: list[dict[str, Any]] = []
    by_vid: dict[str, dict[str, Any]] = {}

    # Index no-vid provisional rows by token+side for cheap lookup.
    candidates_by_token_side: dict[tuple[str, str], list[LocalOrder]] = {}
    for lo in no_vid_locals:
        if lo.confirmation != "provisional":
            continue
        if lo.venue_order_id is not None:
            continue
        key = (str(lo.token_id), str(lo.side))
        candidates_by_token_side.setdefault(key, []).append(lo)

    for vid, vo in venue_views.items():
        if vid in local_by_vid:
            continue
        v_token = str(vo.token_id)
        v_side = str(vo.side)
        bucket = candidates_by_token_side.get((v_token, v_side), [])

        decision: str
        decision_reason: str
        blocking: bool
        adopted_cid: str | None = None
        match_basis: dict[str, Any] = {}

        # Strong adoption: try to find one no-vid candidate with matching size+price+age.
        strong: list[LocalOrder] = []
        weak: list[LocalOrder] = []
        for cand in bucket:
            age_s = _row_age_s(cand, now)
            if age_s is None or age_s > adoption_grace_s:
                continue
            cand_orig = cand.original_size if cand.original_size is not None else cand.remaining
            v_orig = vo.original_size if vo.original_size is not None else vo.remaining_size
            size_ok = _sizes_match_within_tol(cand_orig, v_orig)
            price_ok = _prices_match_within_tol(cand.limit_price, vo.limit_price)
            if size_ok and price_ok:
                strong.append(cand)
            else:
                weak.append(cand)

        if (
            not venue_user_ws_stale
            and not venue_restart_suspected
            and len(strong) == 1
        ):
            target = strong[0]
            _adopt_venue_id_into_no_vid_provisional(order_store, target, vo, now)
            # After adoption the row carries this vid; reflect in the index so the drift loop
            # treats it as a tracked match below.
            local_by_vid[vid] = order_store.orders[target.client_order_id]
            adopted_cid = str(target.client_order_id)
            decision = "adopted_no_vid_provisional"
            decision_reason = (
                "single_no_vid_provisional_with_matching_token_side_size_price_within_adoption_grace_s"
            )
            blocking = False
            match_basis = {
                "token_match": True,
                "side_match": True,
                "size_match_within_tol": True,
                "price_match_within_tol": True,
                "age_within_adoption_grace_s": True,
                "candidate_count_strong": 1,
                "candidate_count_weak": len(weak),
            }
        elif (
            not venue_user_ws_stale
            and not venue_restart_suspected
            and (len(strong) > 1 or len(weak) > 0)
        ):
            decision = "defer_within_adoption_grace"
            decision_reason = (
                "candidate_present_but_size_or_price_mismatch_or_ambiguous_keep_non_blocking_within_adoption_grace_s"
            )
            blocking = False
            match_basis = {
                "token_match": True,
                "side_match": True,
                "size_match_within_tol": False,
                "price_match_within_tol": False,
                "age_within_adoption_grace_s": True,
                "candidate_count_strong": len(strong),
                "candidate_count_weak": len(weak),
            }
        else:
            decision = "blocked_unmatched"
            decision_reason = (
                "no_recent_no_vid_provisional_candidate_or_grace_expired_or_venue_truth_unhealthy"
            )
            blocking = True
            match_basis = {
                "token_match": False,
                "side_match": False,
                "size_match_within_tol": False,
                "price_match_within_tol": False,
                "age_within_adoption_grace_s": False,
                "candidate_count_strong": 0,
                "candidate_count_weak": 0,
            }

        record = {
            "venue_order_id": vid,
            "venue_token_id": v_token,
            "venue_side": v_side,
            "venue_original_size": str(vo.original_size) if vo.original_size is not None else None,
            "venue_remaining": str(vo.remaining_size),
            "venue_limit_price": str(vo.limit_price) if vo.limit_price is not None else None,
            "venue_state_source": vo.venue_state_source,
            "adoption_grace_s": adoption_grace_s,
            "user_ws_fresh": (not venue_user_ws_stale),
            "venue_restart_suspected": venue_restart_suspected,
            "candidate_client_order_id": adopted_cid,
            "match_basis": match_basis,
            "decision": decision,
            "decision_reason": decision_reason,
            "blocking": blocking,
            "resolved_at_utc": now.isoformat(),
        }
        decisions.append(record)
        by_vid[vid] = record

    return decisions, by_vid


def _prune_terminal_confirmed_locals(
    order_store: OrderStore,
    venue_views: dict[str, OpenOrderView],
    *,
    venue_user_ws_stale: bool,
) -> list[str]:
    """Drop ``venue_confirmed`` locals absent from a fresh merged book (UI cancel / full fill)."""
    if venue_user_ws_stale:
        return []
    pruned: list[str] = []
    for cid, lo in list(order_store.orders.items()):
        if lo.venue_order_id is None or lo.confirmation != "venue_confirmed":
            continue
        vid = str(lo.venue_order_id)
        if vid in venue_views:
            continue
        order_store.orders.pop(cid, None)
        if lo.submit_fingerprint:
            order_store.pending_repair_fingerprints.discard(lo.submit_fingerprint)
        pruned.append(vid)
    return pruned


def reconcile_open_orders(
    wallet: WalletStore,
    order_store: OrderStore,
    *,
    venue_user_ws_stale: bool = False,
    venue_restart_suspected: bool = False,
    submit_grace_s: float = 15.0,
    unknown_terminal_timeout_s: float = 60.0,
    adoption_grace_s: float = 5.0,
    # Back-compat aliases — older callers still in the wild.
    provisional_grace_s: float | None = None,
    venue_confirm_provisional_timeout_s: float | None = None,
    now: datetime | None = None,
) -> ReconcileResult:
    """Compare local OMS rows to merged venue truth (user WS primary, REST repair).

    Repair state machine for provisional rows runs **before** drift comparisons; see
    :func:`_resolve_provisional_repair`. For ``venue_confirmed`` locals, strict drift logic still
    applies (size / original mismatch, missing on venue while WS fresh).
    """
    if provisional_grace_s is not None:
        submit_grace_s = float(provisional_grace_s)
    if venue_confirm_provisional_timeout_s is not None:
        unknown_terminal_timeout_s = float(venue_confirm_provisional_timeout_s)

    ts = now if now is not None else utc_now()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    venue_views: dict[str, OpenOrderView] = {}
    for o in wallet.open_orders:
        if o.venue_order_id is not None:
            venue_views[str(o.venue_order_id)] = o

    repair_decisions = _resolve_provisional_repair(
        order_store,
        wallet,
        venue_views,
        venue_user_ws_stale=venue_user_ws_stale,
        venue_restart_suspected=venue_restart_suspected,
        submit_grace_s=submit_grace_s,
        unknown_terminal_timeout_s=unknown_terminal_timeout_s,
        now=ts,
    )

    pruned = _prune_terminal_confirmed_locals(
        order_store,
        venue_views,
        venue_user_ws_stale=venue_user_ws_stale,
    )

    flags: list[str] = []
    blocking: list[str] = []
    comparisons: list[dict[str, Any]] = []

    if any(d["decision"] == "filled_resolved" for d in repair_decisions):
        flags.append("provisional_filled_resolved")
    if any(d["decision"] == "unknown_terminal" for d in repair_decisions):
        flags.append("provisional_unknown_terminal")

    local_by_vid: dict[str, LocalOrder] = {}
    no_vid: list[LocalOrder] = []
    for lo in order_store.orders.values():
        if lo.venue_order_id is not None:
            local_by_vid[str(lo.venue_order_id)] = lo
        else:
            no_vid.append(lo)

    adoption_decisions, adoption_by_vid = _resolve_venue_unmatched_orders(
        order_store,
        venue_views,
        local_by_vid,
        no_vid,
        venue_user_ws_stale=venue_user_ws_stale,
        venue_restart_suspected=venue_restart_suspected,
        adoption_grace_s=adoption_grace_s,
        now=ts,
    )
    # ``_resolve_venue_unmatched_orders`` may have adopted ids onto previously no-vid rows;
    # rebuild the no-vid list to reflect that and avoid double-counting them in the
    # ``local_orders_missing_venue_excess`` rule below.
    no_vid = [lo for lo in order_store.orders.values() if lo.venue_order_id is None]

    def _compare_row(
        vid: str,
        lo: LocalOrder | None,
        v: OpenOrderView | None,
        *,
        note: str | None = None,
        row_blocks_live: bool | None = None,
    ) -> None:
        vrem = v.remaining_size if v is not None else None
        vorig = v.original_size if v is not None else None
        vmatched = v.size_matched if v is not None else None
        venue_computed = None
        if vorig is not None and vmatched is not None:
            venue_computed = vorig - vmatched
        lrem = lo.remaining if lo is not None else None
        lorig = lo.original_size if lo is not None else None
        lmatched = lo.size_matched if lo is not None else None
        equiv_rem = (
            remaining_sizes_equivalent(lrem, vrem)
            if lrem is not None and v is not None and vrem is not None
            else False
        )
        equiv_orig = (
            remaining_sizes_equivalent(lorig, vorig)
            if lorig is not None and vorig is not None
            else True
        )
        comparisons.append(
            {
                "venue_order_id": vid,
                "local_confirmation": lo.confirmation if lo is not None else None,
                "local_provisional": (lo.confirmation == "provisional") if lo is not None else None,
                "local_original": str(lorig) if lorig is not None else None,
                "local_matched": str(lmatched) if lmatched is not None else None,
                "local_remaining": str(lrem) if lrem is not None else None,
                "venue_original_size": str(vorig) if vorig is not None else None,
                "venue_size_matched": str(vmatched) if vmatched is not None else None,
                "venue_remaining": str(vrem) if vrem is not None else None,
                "venue_remaining_computed": str(venue_computed) if venue_computed is not None else None,
                "venue_state_source": v.venue_state_source if v is not None else None,
                "remaining_match": equiv_rem,
                "original_match": equiv_orig,
                "row_blocks_live": row_blocks_live,
                "note": note,
            }
        )

    # Lookup local rows by stringified client_order_id (decisions store cid as str).
    locals_by_cid_str: dict[str, LocalOrder] = {
        str(cid): lo for cid, lo in order_store.orders.items()
    }

    # Provisional rows still in the store after repair: emit pending / blocking flags + comparisons.
    for d in repair_decisions:
        if d["terminal_dropped"]:
            continue
        vid = d["venue_order_id"] or "<no_venue_order_id>"
        lo = locals_by_cid_str.get(d["client_order_id"])
        if d["decision"] == "blocked_absent":
            flags.append("local_open_not_on_venue")
            blocking.append("local_open_not_on_venue")
            _compare_row(vid, lo, None, row_blocks_live=True, note="provisional_past_grace")
        elif d["decision"] == "blocked_unsafe_to_resolve":
            flags.append("local_open_not_on_venue")
            blocking.append("local_open_not_on_venue")
            _compare_row(
                vid,
                lo,
                None,
                row_blocks_live=True,
                note="ws_stale_or_restart_no_auto_resolution",
            )
        elif d["decision"] == "pending_within_grace":
            flags.append("provisional_pending_venue")
            _compare_row(
                vid,
                lo,
                None,
                row_blocks_live=False,
                note="within_submit_grace",
            )

    # Drift checks: present-on-venue size/original mismatch (provisional + venue_confirmed),
    # absent-on-venue handled by repair state machine for provisional and by prune for venue_confirmed.
    for vid, lo in local_by_vid.items():
        v = venue_views.get(vid)
        if v is None:
            if lo.confirmation == "venue_confirmed":
                # Stale WS path: prune was skipped, so still surface drift.
                flags.append("local_open_not_on_venue")
                blocking.append("local_open_not_on_venue")
                _compare_row(vid, lo, None, row_blocks_live=True, note="venue_confirmed_absent_ws_stale")
            # provisional absent rows already covered by repair_decisions emissions.
            continue
        if not remaining_sizes_equivalent(lo.remaining, v.remaining_size):
            flags.append("open_order_size_mismatch")
            blocking.append("open_order_size_mismatch")
            _compare_row(vid, lo, v, row_blocks_live=True)
            continue
        if lo.original_size is not None and v.original_size is not None:
            if not remaining_sizes_equivalent(lo.original_size, v.original_size):
                flags.append("open_order_original_mismatch")
                blocking.append("open_order_original_mismatch")
                _compare_row(vid, lo, v, row_blocks_live=True)
                continue

    # Venue rows we don't track locally: drive by the adoption state machine above.
    # ``adopted_no_vid_provisional`` rows are now in ``local_by_vid`` and will fall into the
    # confirmed/drift loop on the next pass — no flag emitted here.
    # ``defer_within_adoption_grace`` rows emit a non-blocking informational flag.
    # ``blocked_unmatched`` rows keep the historical fail-closed behavior.
    for vid in list(venue_views.keys()):
        dec = adoption_by_vid.get(vid)
        if dec is None:
            continue
        if dec["decision"] == "adopted_no_vid_provisional":
            flags.append("venue_adopted_into_local_provisional")
            _compare_row(
                vid,
                local_by_vid.get(vid),
                venue_views[vid],
                row_blocks_live=False,
                note="adopted_no_vid_provisional",
            )
        elif dec["decision"] == "defer_within_adoption_grace":
            flags.append("venue_open_not_tracked_locally_pending_adoption")
            _compare_row(
                vid,
                None,
                venue_views[vid],
                row_blocks_live=False,
                note="defer_within_adoption_grace",
            )
        else:  # blocked_unmatched
            flags.append("venue_open_not_tracked_locally")
            blocking.append("venue_open_not_tracked_locally")
            _compare_row(vid, None, venue_views[vid], row_blocks_live=True)

    if len(no_vid) > order_store.in_flight_order_count:
        flags.append("local_orders_missing_venue_excess")
        blocking.append("local_orders_missing_venue_excess")

    blocking_f = tuple(blocking)
    comp_out = tuple(comparisons) if comparisons else ()
    terminal_only = tuple(
        d for d in repair_decisions if d["terminal_dropped"]
    )

    return ReconcileResult(
        drift_flags=tuple(flags),
        blocking_drift_flags=blocking_f,
        reconcile_severity=_severity_for_blocking(frozenset(blocking_f)),
        order_comparisons=comp_out,
        pruned_terminal_venue_order_ids=tuple(pruned),
        provisional_repair_decisions=tuple(repair_decisions),
        provisional_timeout_resolutions=terminal_only,
        venue_adoption_decisions=tuple(adoption_decisions),
        policy_summary=RECONCILE_POLICY_SUMMARY,
    )
