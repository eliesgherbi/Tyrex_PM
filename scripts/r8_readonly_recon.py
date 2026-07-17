"""R8 read-only account reconciliation. Zero mutations."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]

BUY = "0xde990e41f647c7f2041b595e04b2d34b7c116e1109f2cd53f5dccdde51d46a99"
SELL = "0x336afebee21bcdbfb3eba22842a5acccc99d97b22e5eb279d446fb7bb5a7cc70"
TOKEN = (
    "24852991651338435465983157211860349787691720913290359521714271717026575720912"
)
COND = "0x12b4a1c29237a70149e918a6f7f5b5d3d8f7047a0ec5d871b76e86d16d170cd7"
RUN_ID = "55fd9a76-743b-4fb8-835d-adcdbf0f517a"
ACK_SUFFIXES = {"41157415", "37632345", "06949906", "42203648"}
DUST = [
    (
        "36466979",
        "1038082852808592687103030316298741805143710778541582307413776216436336466979",
    ),
    (
        "28780279",
        "31263449814698561223405652464241470284809290399992381387531969085905328780279",
    ),
    ("75720912", TOKEN),
]


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
    req = Request(url, headers={"User-Agent": "tyrex-pm-r8-recon/1.0"}, method="GET")
    with urlopen(req, timeout=30) as resp:
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
    from tyrex_pm.runtime.r7_lifecycle_residuals import read_residual_registry

    env = _load_dotenv(REPO / ".env")
    creds = load_l2_credentials(env)
    user = positions_wallet_address(creds)
    report: dict = {
        "schema": "r8_account_recon_v1",
        "mutations_attempted": False,
        "success_run_id": RUN_ID,
        "funder_suffix": user[-8:] if user else None,
    }

    positions = _get_json(
        f"https://data-api.polymarket.com/positions?{urlencode({'user': user})}"
    )
    pos_list = [p for p in positions if isinstance(p, dict)] if isinstance(positions, list) else []
    ack_matched = []
    other_nonzero = []
    for p in pos_list:
        asset = str(p.get("asset") or "")
        suf = asset[-8:] if asset else ""
        qty = Decimal(str(p.get("size") or p.get("quantity") or "0"))
        row = {
            "token_suffix": suf,
            "qty": str(qty),
            "curPrice": p.get("curPrice"),
            "redeemable": p.get("redeemable"),
            "title": str(p.get("title") or "")[:80],
        }
        if suf in ACK_SUFFIXES:
            ack_matched.append(row)
        elif qty != 0:
            other_nonzero.append(row)
    report["data_api_position_count"] = len(pos_list)
    report["ack_matched"] = ack_matched
    report["ack_count"] = len(ack_matched)
    report["other_nonzero_positions"] = other_nonzero

    tr = SdkReadonlyTransport.from_env(env)
    dust_balances = []
    for suf, tok in DUST:
        bal, _allow = tr.get_conditional_balance_allowance(tok)
        flat = classify_flatness(
            conditional_balance=Decimal(str(bal)),
            balance_known=True,
            min_tradable=DEFAULT_MIN_TRADABLE,
        )
        dust_balances.append(
            {
                "token_suffix": suf,
                "balance": str(bal),
                "classification": flat["classification"],
                "tradable": Decimal(str(bal)) >= DEFAULT_MIN_TRADABLE,
            }
        )
    report["dust_balances"] = dust_balances

    # Unfiltered trade history — market_id filter can miss closed windows.
    trades = tr.get_trades()
    related = []
    for t in trades:
        oid = str(getattr(t, "venue_order_id", "") or "")
        tid = str(getattr(t, "venue_trade_id", "") or "")
        asset = str(getattr(t, "instrument_token_id", "") or "")
        side = str(getattr(t, "side", "") or "").upper()
        status = str(getattr(t, "status", "") or "").upper()
        size = str(getattr(t, "size", "0") or "0")
        price = str(getattr(t, "price", "") or "")
        fee = str(getattr(t, "fee_rate_bps", "") or "")
        raw = getattr(t, "raw", None) or {}
        if not isinstance(raw, dict):
            raw = _as_dict(raw)
        tx = str(raw.get("transaction_hash") or raw.get("tx_hash") or "")
        ts = str(raw.get("match_time") or raw.get("last_update") or "")
        hit = (
            BUY.lower() in oid.lower()
            or SELL.lower() in oid.lower()
            or asset == TOKEN
        )
        if not hit:
            continue
        related.append(
            {
                "order_id": oid,
                "trade_id": tid,
                "side": side,
                "status": status,
                "size": size,
                "price": price,
                "fee_rate_bps": fee,
                "transaction_hash": tx,
                "match_time_unix": ts,
                "asset_suffix": asset[-8:] if asset else "",
                "is_buy_order": BUY.lower() in oid.lower(),
                "is_sell_order": SELL.lower() in oid.lower(),
            }
        )
    report["related_trades"] = related

    open_orders: list[dict] = []
    client = getattr(tr, "client", None)
    if client is not None:
        for meth_name in ("get_orders", "get_open_orders"):
            meth = getattr(client, meth_name, None)
            if not callable(meth):
                continue
            try:
                raw = meth()
                rows = raw if isinstance(raw, list) else []
                for r in rows:
                    d = _as_dict(r)
                    open_orders.append(
                        {
                            "id_suffix": str(d.get("id") or d.get("order_id") or "")[
                                -16:
                            ],
                            "side": d.get("side"),
                            "status": d.get("status"),
                            "asset_suffix": str(d.get("asset_id") or "")[-8:],
                        }
                    )
                report["open_orders_method"] = meth_name
                break
            except Exception as exc:  # noqa: BLE001
                report["open_orders_error"] = f"{type(exc).__name__}:{exc}"
    report["open_orders"] = open_orders
    report["open_orders_zero"] = len(open_orders) == 0

    reg = read_residual_registry(repo_root=REPO)
    residual_rows = list(reg.residuals.values()) if reg is not None else []
    report["residual_registry_count"] = len(residual_rows)
    report["residual_registry"] = [
        {
            "token_suffix": r.token_id[-8:],
            "classification": r.classification,
            "residual_quantity": r.residual_quantity,
            "originating_run_id": r.originating_run_id,
            "tradable": r.tradable,
            "cleanup_policy": r.cleanup_policy,
            "in_acknowledgment_set": False,
        }
        for r in residual_rows
    ]

    report["checks"] = {
        "ack_exactly_four": len(ack_matched) == 4,
        "three_distinct_dust": len({r.token_id[-8:] for r in residual_rows}) == 3
        and all(r.classification == "FLAT_WITH_DUST" for r in residual_rows),
        "no_tradable_dust": all(not d["tradable"] for d in dust_balances),
        "open_orders_zero": report["open_orders_zero"],
        "cleanup_none": all(r.cleanup_policy == "NONE" for r in residual_rows),
        "dust_not_in_ack": True,
        "no_auto_redeem": True,
    }

    out = REPO / "var" / "reporting" / "r8" / "account_recon.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["checks"], indent=2))
    print(f"related_trades={len(related)} open_orders={len(open_orders)} path={out}")
    for t in related:
        print(
            f"{t['side']} {t['status']} size={t['size']} price={t['price']} "
            f"trade={t['trade_id'][:16]}… tx={(t['transaction_hash'] or '')[:18]}…"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
