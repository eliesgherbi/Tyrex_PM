"""R6C safe public connectivity diagnostics — no auth headers, no secrets."""

from __future__ import annotations

import json
import socket
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var" / "reporting" / "r6" / "connectivity_diag.json"


def main() -> int:
    results: list[dict] = []

    def rec(**row: object) -> None:
        results.append(dict(row))

    for host in (
        "clob.polymarket.com",
        "data-api.polymarket.com",
        "gamma-api.polymarket.com",
        "ws-subscriptions-clob.polymarket.com",
    ):
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            ips = sorted({i[4][0] for i in infos})
            rec(
                host=host,
                path="(dns)",
                method="DNS",
                category="infra",
                status="ok",
                ip_count=len(ips),
                has_ipv4=any("." in x for x in ips),
            )
        except OSError as exc:
            rec(
                host=host,
                path="(dns)",
                method="DNS",
                category="infra",
                status="error",
                error=type(exc).__name__,
            )

    for host in ("clob.polymarket.com", "data-api.polymarket.com"):
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    rec(
                        host=host,
                        path="(tls)",
                        method="TLS",
                        category="infra",
                        status="ok",
                        version=ssock.version(),
                    )
        except OSError as exc:
            rec(
                host=host,
                path="(tls)",
                method="TLS",
                category="infra",
                status="error",
                error=type(exc).__name__,
            )

    # Public GETs — no auth. token_id for /book is a synthetic invalid id (expect 400/404, not 403).
    public = [
        ("clob.polymarket.com", "/time", "public_market_data"),
        ("clob.polymarket.com", "/book?token_id=1", "public_market_data"),
        ("gamma-api.polymarket.com", "/markets?limit=1", "public_market_data"),
        ("data-api.polymarket.com", "/markets?limit=1", "public_data_api"),
    ]
    for host, path, cat in public:
        url = f"https://{host}{path}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "tyrex-pm-r6c-preflight/1.0"},
        )
        path_only = path.split("?", 1)[0]
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                rec(
                    host=host,
                    path=path_only,
                    method="GET",
                    category=cat,
                    status=int(resp.status),
                    cloudflare_error=None,
                    app_auth_validation_reached=False,
                )
        except HTTPError as exc:
            body = exc.read(200).decode("utf-8", errors="replace")
            cf = None
            if "1010" in body or "cloudflare" in body.lower():
                cf = "1010" if "1010" in body else "cloudflare"
            rec(
                host=host,
                path=path_only,
                method="GET",
                category=cat,
                status=int(exc.code),
                cloudflare_error=cf,
                app_auth_validation_reached=False,
                error_class=type(exc).__name__,
            )
        except URLError as exc:
            rec(
                host=host,
                path=path_only,
                method="GET",
                category=cat,
                status="network_error",
                error_class=type(exc.reason).__name__ if exc.reason else "URLError",
            )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "environment": "agent_runtime",
        "note": "No auth headers; no secrets; no POST/DELETE",
        "results": results,
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
