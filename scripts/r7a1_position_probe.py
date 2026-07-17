"""Read-only R7A.1 position probe — never mutates."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tyrex_pm.execution.polymarket.auth import load_l2_credentials, positions_wallet_address


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> None:
    env = _load_dotenv(Path(".env"))
    creds = load_l2_credentials(env)
    user = positions_wallet_address(creds)
    url = f"https://data-api.polymarket.com/positions?{urlencode({'user': user})}"
    req = Request(url, headers={"User-Agent": "tyrex-r7a1"}, method="GET")
    with urlopen(req, timeout=20) as resp:
        rows = json.loads(resp.read().decode())
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        size = float(r.get("size") or 0)
        if size == 0:
            continue
        out.append(
            {
                "title": r.get("title"),
                "slug": r.get("slug") or r.get("eventSlug"),
                "outcome": r.get("outcome"),
                "size": r.get("size"),
                "avgPrice": r.get("avgPrice"),
                "curPrice": r.get("curPrice"),
                "cashPnl": r.get("cashPnl"),
                "percentPnl": r.get("percentPnl"),
                "redeemable": r.get("redeemable"),
                "mergeable": r.get("mergeable"),
                "negativeRisk": r.get("negativeRisk"),
                "endDate": r.get("endDate"),
                "closed": r.get("closed"),
                "conditionId_prefix": (str(r.get("conditionId") or "")[:18] + "…")
                if r.get("conditionId")
                else None,
                "asset_prefix": (str(r.get("asset") or "")[:18] + "…")
                if r.get("asset")
                else None,
                "keys": sorted(r.keys()),
            }
        )
    print(json.dumps({"nonzero_count": len(out), "positions": out}, indent=2))


if __name__ == "__main__":
    main()
