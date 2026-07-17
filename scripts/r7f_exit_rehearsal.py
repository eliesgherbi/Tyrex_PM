"""R7F read-only exit-planner rehearsal against public CLOB books.

Zero mutations. Never submits or cancels orders.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]


def _get_json(url: str) -> object:
    req = Request(url, headers={"User-Agent": "tyrex-pm-r7f-rehearsal/1.0"}, method="GET")
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    sys.path.insert(0, str(REPO / "src"))
    from tyrex_pm.execution.polymarket.lifecycle_exit_plan import (
        ExitPlanStatus,
        ExitUrgency,
        book_from_clob_levels,
        plan_lifecycle_fak_sell,
    )
    from tyrex_pm.runtime.r7_lifecycle_policy import (
        default_exit_price_policy,
        policy_snapshot,
    )

    now = datetime.now(timezone.utc)
    # Current + prior 5m windows
    epoch = int(now.timestamp()) // 300 * 300
    slugs = [f"btc-updown-5m-{epoch + i * 300}" for i in range(-1, 3)]
    report: dict = {
        "schema": "r7f_exit_rehearsal_v1",
        "ts": now.isoformat(),
        "mutations_attempted": False,
        "mutations_enabled": False,
        "policy": policy_snapshot(),
        "rehearsals": [],
        "synthetic_cases": [],
    }

    for slug in slugs:
        try:
            events = _get_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
        except Exception as exc:  # noqa: BLE001
            report["rehearsals"].append({"slug": slug, "error": f"{type(exc).__name__}:{exc}"})
            continue
        if not isinstance(events, list) or not events:
            report["rehearsals"].append({"slug": slug, "error": "NO_EVENT"})
            continue
        ev = events[0]
        markets = ev.get("markets") or []
        if not markets:
            # try nested
            report["rehearsals"].append({"slug": slug, "error": "NO_MARKETS", "closed": ev.get("closed")})
            continue
        m = markets[0] if isinstance(markets[0], dict) else {}
        # token ids may be JSON strings
        raw_tokens = m.get("clobTokenIds") or m.get("clob_token_ids")
        if isinstance(raw_tokens, str):
            try:
                tokens = json.loads(raw_tokens)
            except json.JSONDecodeError:
                tokens = []
        else:
            tokens = list(raw_tokens or [])
        if not tokens:
            report["rehearsals"].append({"slug": slug, "error": "NO_TOKENS"})
            continue
        yes_tok = str(tokens[0])
        book_raw = _get_json(f"https://clob.polymarket.com/book?token_id={yes_tok}")
        if not isinstance(book_raw, dict):
            report["rehearsals"].append({"slug": slug, "error": "BOOK_FETCH_FAILED"})
            continue
        ts = datetime.now(timezone.utc)
        book = book_from_clob_levels(
            token_id=yes_tok,
            bids=book_raw.get("bids") or [],
            asks=book_raw.get("asks") or [],
            ts_event=ts,
        )
        # Hypothetical entry at best ask (or 0.51 fallback)
        asks = book.asks
        entry_buy = asks[0].price if asks else Decimal("0.51")
        qty = Decimal("9.47")
        plan = plan_lifecycle_fak_sell(
            book=book,
            quantity=qty,
            tick_size=Decimal("0.01"),
            now=ts,
            policy=default_exit_price_policy(),
            entry_buy_limit=entry_buy,
        )
        # Second snapshot for “moved book”
        book2_raw = _get_json(f"https://clob.polymarket.com/book?token_id={yes_tok}")
        ts2 = datetime.now(timezone.utc)
        book2 = book_from_clob_levels(
            token_id=yes_tok,
            bids=(book2_raw.get("bids") or []) if isinstance(book2_raw, dict) else [],
            asks=(book2_raw.get("asks") or []) if isinstance(book2_raw, dict) else [],
            ts_event=ts2,
        )
        plan2 = plan_lifecycle_fak_sell(
            book=book2,
            quantity=qty,
            tick_size=Decimal("0.01"),
            now=ts2,
            policy=default_exit_price_policy(),
            entry_buy_limit=entry_buy,
        )
        row = {
            "slug": slug,
            "token_suffix": yes_tok[-8:],
            "closed": ev.get("closed"),
            "hypothetical_entry_buy_limit": str(entry_buy),
            "exit_plan_1": plan.to_dict(),
            "exit_plan_2": plan2.to_dict(),
            "exit_used_entry_buy_limit": (
                plan.limit_price == entry_buy
                if plan.limit_price is not None
                else False
            ),
            "fingerprints_differ_or_equal": {
                "fp1": plan.book_fingerprint,
                "fp2": plan2.book_fingerprint,
                "same": plan.book_fingerprint == plan2.book_fingerprint,
            },
            "derived_from_bids": plan.evidence.get("bid_levels_used") is True,
        }
        report["rehearsals"].append(row)

    # Deterministic synthetic cases (captured-book style)
    pol = default_exit_price_policy()
    base_ts = datetime.now(timezone.utc)

    def case(name: str, **kwargs: object) -> None:
        plan = plan_lifecycle_fak_sell(policy=pol, **kwargs)  # type: ignore[arg-type]
        report["synthetic_cases"].append({"name": name, "plan": plan.to_dict()})

    def bk(bids, asks, age_ms=0):
        return book_from_clob_levels(
            token_id="rehearsal",
            bids=bids,
            asks=asks,
            ts_event=base_ts - timedelta(milliseconds=age_ms),
        )

    case(
        "stable_bid",
        book=bk([("0.50", "100")], [("0.51", "100")]),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
    )
    case(
        "bid_below_entry",
        book=bk([("0.49", "100")], [("0.51", "100")]),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
    )
    case(
        "multi_level_depth",
        book=bk([("0.50", "4"), ("0.49", "10")], [("0.51", "100")]),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
    )
    case(
        "insufficient_depth",
        book=bk([("0.50", "1")], [("0.51", "100")]),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
    )
    case(
        "empty_bids",
        book=bk([], [("0.51", "100")]),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
    )
    case(
        "stale_book",
        book=bk([("0.50", "100")], [("0.51", "100")], age_ms=5000),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
    )
    case(
        "normal_floor_fail",
        book=bk([("0.005", "100")], [("0.51", "100")]),
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
        urgency=ExitUrgency.NORMAL,
    )
    case(
        "emergency_escalation",
        book=bk([("0.02", "100")], [("0.51", "100")]),
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
        urgency=ExitUrgency.EMERGENCY,
    )
    # Partial fill replan remaining
    first = plan_lifecycle_fak_sell(
        book=bk([("0.50", "100")], [("0.51", "100")]),
        quantity=Decimal("9.47"),
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
        policy=pol,
    )
    remaining = Decimal("9.47") - Decimal("5.00")
    second = plan_lifecycle_fak_sell(
        book=bk([("0.49", "100")], [("0.51", "100")]),
        quantity=remaining,
        tick_size=Decimal("0.01"),
        now=base_ts,
        entry_buy_limit=Decimal("0.51"),
        policy=pol,
    )
    report["synthetic_cases"].append(
        {
            "name": "partial_then_replan_remaining",
            "first": first.to_dict(),
            "remaining_qty": str(remaining),
            "second": second.to_dict(),
            "fingerprints_differ": first.book_fingerprint != second.book_fingerprint,
        }
    )

    out = REPO / "var" / "reporting" / "r7f" / "exit_rehearsal.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    # Save synthetic books as fixtures
    fix_dir = REPO / "tests" / "fixtures" / "r7f_books"
    fix_dir.mkdir(parents=True, exist_ok=True)
    (fix_dir / "stable_bid.json").write_text(
        json.dumps({"bids": [["0.50", "100"]], "asks": [["0.51", "100"]]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (fix_dir / "bid_below_entry.json").write_text(
        json.dumps({"bids": [["0.49", "100"]], "asks": [["0.51", "100"]]}, indent=2) + "\n",
        encoding="utf-8",
    )

    ok_syn = all(
        c.get("plan", {}).get("status")
        in {
            ExitPlanStatus.PLANNED.value,
            ExitPlanStatus.WAIT_NO_BIDS.value,
            ExitPlanStatus.REFUSE_DEPTH.value,
            ExitPlanStatus.REFUSE_STALE_BOOK.value,
            ExitPlanStatus.REFUSE_FLOOR.value,
            ExitPlanStatus.REFUSE_SLIPPAGE.value,
            ExitPlanStatus.REFUSE_SPREAD.value,
        }
        or "first" in c
        for c in report["synthetic_cases"]
    )
    print(json.dumps({
        "wrote": str(out),
        "live_windows": len(report["rehearsals"]),
        "synthetic": len(report["synthetic_cases"]),
        "mutations_attempted": False,
        "ok_synthetic": ok_syn,
    }, indent=2))
    return 0 if ok_syn else 1


if __name__ == "__main__":
    raise SystemExit(main())
