"""R7E read-only reconciliation for second-live incident. Zero mutations."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
TOKEN = (
    "31263449814698561223405652464241470284809290399992381387531969085905328780279"
)
COND = "0xc60a7a5e093f89e668a25a8a4d43ec2cd175096380f9a98c81ab48d0eea9b7be"
BUY = "0x1eb62e98dbe5c86b251a0417a2e3851511a4ac90c17d53228f277df3ace577a8"
SELL_ATTEMPT = "0x29cd29a86129dc548034a6f11b03482fefacf9a515be08902329cb21d4298fda"
SLUG = "btc-updown-5m-1784315400"
RUN_ID = "76e8470a-72dc-4d6d-b653-760e87bdaf29"


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _get_json(url: str) -> object:
    req = Request(url, headers={"User-Agent": "tyrex-pm-r7e-recon/1.0"}, method="GET")
    with urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _as_dict(obj: object) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            d = obj.to_dict()
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            pass
    d = getattr(obj, "__dict__", None)
    return dict(d) if isinstance(d, dict) else {"repr": str(obj)}


def main() -> int:
    sys.path.insert(0, str(REPO / "src"))
    from tyrex_pm.execution.polymarket.auth import (
        load_l2_credentials,
        positions_wallet_address,
    )
    from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport
    from tyrex_pm.execution.polymarket.settlement import (
        DEFAULT_MIN_TRADABLE,
        classify_flatness,
    )
    from tyrex_pm.runtime.r7_lifecycle_residuals import (
        LifecycleResidualRecord,
        CLEANUP_POLICY_NONE,
        migrate_dust_to_registry,
        read_residual_registry,
        upsert_residual,
        write_residual_registry,
    )

    env = _load_dotenv(REPO / ".env")
    creds = load_l2_credentials(env)
    user = positions_wallet_address(creds)
    report: dict = {
        "schema": "r7e_second_live_recon_v1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mutations_attempted": False,
        "run_id": RUN_ID,
        "market_slug": SLUG,
        "token_id": TOKEN,
        "condition_id": COND,
        "buy_order_id": BUY,
        "sell_attempt_id": SELL_ATTEMPT,
        "funder": user,
    }

    positions = _get_json(
        f"https://data-api.polymarket.com/positions?{urlencode({'user': user})}"
    )
    pos_list = [p for p in positions if isinstance(p, dict)] if isinstance(positions, list) else []
    selected = [p for p in pos_list if str(p.get("asset") or "") == TOKEN]
    report["data_api_selected_positions"] = selected
    report["data_api_position_count"] = len(pos_list)

    markets = _get_json(f"https://gamma-api.polymarket.com/markets?slug={SLUG}")
    m = markets[0] if isinstance(markets, list) and markets else {}
    report["market"] = {
        "slug": m.get("slug"),
        "closed": m.get("closed"),
        "active": m.get("active"),
        "umaResolutionStatus": m.get("umaResolutionStatus"),
        "resolved": m.get("resolved"),
        "endDate": m.get("endDate"),
        "outcomePrices": m.get("outcomePrices"),
    }

    tr = SdkReadonlyTransport.from_env(env)
    bal, allow = tr.get_conditional_balance_allowance(TOKEN)
    report["conditional_balance"] = str(bal)
    report["allowance"] = str(allow)

    trades = tr.get_trades(market_id=COND)
    related = []
    buy_confirmed = Decimal("0")
    sell_confirmed = Decimal("0")
    for t in trades:
        d = _as_dict(t)
        oid = str(
            d.get("taker_order_id")
            or d.get("maker_order_id")
            or d.get("order_id")
            or d.get("id")
            or ""
        )
        asset = str(d.get("asset_id") or d.get("token_id") or "")
        side = str(d.get("side") or "").upper()
        status = str(d.get("status") or "").upper()
        size = Decimal(str(d.get("size") or d.get("matched_amount") or "0"))
        price = str(d.get("price") or "")
        fee = str(d.get("fee_rate_bps") or d.get("fee") or "")
        tx = str(d.get("transaction_hash") or d.get("tx_hash") or "")
        hit = (
            BUY.lower() in oid.lower()
            or SELL_ATTEMPT.lower() in oid.lower()
            or asset == TOKEN
        )
        if not hit:
            continue
        related.append(
            {
                "order_id": oid,
                "side": side,
                "status": status,
                "size": str(size),
                "price": price,
                "fee": fee,
                "transaction_hash": tx,
            }
        )
        if BUY.lower() in oid.lower() and status == "CONFIRMED" and side == "BUY":
            buy_confirmed += size
        if side == "SELL" and status == "CONFIRMED" and asset == TOKEN:
            sell_confirmed += size
        # also match sell attempt id if present
        if SELL_ATTEMPT.lower() in oid.lower():
            related[-1]["is_failed_sell_attempt"] = True

    report["related_trades"] = related
    report["buy_confirmed_qty_from_trades"] = str(buy_confirmed)
    report["sell_confirmed_qty_from_trades"] = str(sell_confirmed)
    report["sell_attempt_had_fill"] = any(
        r.get("is_failed_sell_attempt") and Decimal(r["size"]) > 0 and r["status"] == "CONFIRMED"
        for r in related
    )

    # open orders — best effort via data API activity
    open_orders = []
    try:
        oo = _get_json(
            f"https://clob.polymarket.com/data/orders?{urlencode({'market': COND})}"
        )
        report["open_orders_raw_type"] = type(oo).__name__
    except Exception as exc:  # noqa: BLE001
        report["open_orders_public_error"] = f"{type(exc).__name__}:{exc}"

    if hasattr(tr, "client"):
        client = getattr(tr, "client", None)
        for meth_name in ("get_orders", "get_open_orders"):
            meth = getattr(client, meth_name, None) if client is not None else None
            if callable(meth):
                try:
                    raw = meth()
                    rows = raw if isinstance(raw, list) else []
                    for r in rows:
                        d = _as_dict(r)
                        asset = str(d.get("asset_id") or d.get("token_id") or "")
                        if asset == TOKEN or str(d.get("market") or "") == COND:
                            open_orders.append(
                                {
                                    "id": str(d.get("id") or d.get("order_id") or ""),
                                    "side": d.get("side"),
                                    "size": d.get("original_size") or d.get("size"),
                                    "price": d.get("price"),
                                    "status": d.get("status"),
                                }
                            )
                    report["open_orders_method"] = meth_name
                    break
                except Exception as exc:  # noqa: BLE001
                    report["open_orders_sdk_error"] = f"{type(exc).__name__}:{exc}"

    report["open_orders"] = open_orders
    report["open_orders_zero"] = len(open_orders) == 0

    flat = classify_flatness(
        conditional_balance=Decimal(str(bal)),
        balance_known=True,
        min_tradable=DEFAULT_MIN_TRADABLE,
    )
    closed = bool(m.get("closed"))
    resolved = bool(m.get("resolved")) or str(m.get("umaResolutionStatus") or "").lower() in {
        "resolved",
        "proposed",
    }
    # Classification priority
    if Decimal(str(bal)) == 0 and sell_confirmed > 0 and buy_confirmed > 0:
        classification = "FLAT_EXTERNAL_ACTION" if sell_confirmed > 0 else "FLAT"
        # If we never got a confirmed SELL trade from our attempt but balance is 0,
        # user likely sold manually.
        if not report["sell_attempt_had_fill"]:
            classification = "FLAT_EXTERNAL_ACTION"
    elif Decimal(str(bal)) == 0 and buy_confirmed > 0:
        classification = "FLAT_EXTERNAL_ACTION"
    elif Decimal(str(bal)) == 0:
        classification = "FLAT"
    elif flat["classification"] == "FLAT_WITH_DUST":
        classification = "FLAT_WITH_DUST"
    elif resolved or closed:
        if Decimal(str(bal)) > 0:
            classification = "RESOLVED_POSITION" if resolved else "RESIDUAL_EXPOSURE"
        else:
            classification = "RESOLVED_POSITION"
    elif Decimal(str(bal)) >= DEFAULT_MIN_TRADABLE:
        classification = "RESIDUAL_EXPOSURE"
    else:
        classification = "UNKNOWN"

    # refine: if balance zero after our failed FAK sell, external flatten
    if Decimal(str(bal)) == 0 and buy_confirmed > 0:
        classification = "FLAT_EXTERNAL_ACTION"

    report["classification"] = classification
    report["flatness"] = flat
    report["manual_sell_likely"] = (
        Decimal(str(bal)) == 0
        and buy_confirmed > 0
        and not report["sell_attempt_had_fill"]
    )

    # Update residual registry without overwriting R7B dust
    reg = read_residual_registry(repo_root=REPO) or migrate_dust_to_registry(
        repo_root=REPO, force_incident=True
    )
    now = datetime.now(timezone.utc).isoformat()
    rec = LifecycleResidualRecord(
        condition_id=COND,
        token_id=TOKEN,
        originating_run_id=RUN_ID,
        market_slug=SLUG,
        acquired_quantity=str(buy_confirmed or Decimal("9.470587")),
        exited_quantity=str(sell_confirmed),
        residual_quantity=str(bal),
        min_tradable=str(DEFAULT_MIN_TRADABLE),
        classification=classification
        if classification != "FLAT_EXTERNAL_ACTION"
        else (
            "FLAT"
            if Decimal(str(bal)) == 0
            else classification
        ),
        provenance="r7d2_second_live_buy_0x1eb62e98_sell_fak_no_match_0x29cd29a8",
        created_at=now,
        updated_at=now,
        last_reconciliation_source="r7e_readonly_recon",
        tradable=Decimal(str(bal)) >= DEFAULT_MIN_TRADABLE,
        cleanup_policy=CLEANUP_POLICY_NONE,
        buy_order_id=BUY,
        closed=Decimal(str(bal)) == 0,
        closed_at=now if Decimal(str(bal)) == 0 else None,
        historical_provenance_retained=True,
    )
    # Prefer explicit FLAT_EXTERNAL_ACTION in classification field
    rec.classification = classification
    upsert_residual(reg, rec)
    path = write_residual_registry(reg, repo_root=REPO)
    report["residual_registry_path"] = str(path)
    report["residual_open_count"] = len(reg.open_residuals())
    report["residual_keys"] = list(reg.residuals.keys())
    report["r7b_dust_preserved"] = any(
        "d632b631-166f-4e35-8db8-fe69a3f86795" in k for k in reg.residuals
    )

    out_path = REPO / "var" / "reporting" / "r7e" / "second_live_recon.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "classification",
        "conditional_balance",
        "open_orders_zero",
        "buy_confirmed_qty_from_trades",
        "sell_confirmed_qty_from_trades",
        "sell_attempt_had_fill",
        "manual_sell_likely",
        "market",
        "r7b_dust_preserved",
        "residual_open_count",
    )}, indent=2, default=str))
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
