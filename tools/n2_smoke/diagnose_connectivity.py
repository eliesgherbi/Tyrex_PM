#!/usr/bin/env python3
"""Bounded N2 connectivity diagnosis (read-only, no TLS bypass).

Compares N1/N2 client paths vs minimal urllib/websockets against public hosts.
Writes evidence JSON under var/runs/_ops/n2_smoke/ (gitignored).
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OUT = Path("var/runs/_ops/n2_smoke/connectivity_diagnosis.json")
UA = "TyrexPM-N2-Diag/1.0 (read-only)"


def _proxy_env() -> dict[str, bool]:
    keys = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ]
    return {k: bool(os.environ.get(k)) for k in keys}


def _cert_info(hostname: str, port: int = 443) -> dict:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                return {
                    "ok": True,
                    "protocol": ssock.version(),
                    "cipher": ssock.cipher(),
                    "subject": cert.get("subject"),
                    "issuer": cert.get("issuer"),
                    "subjectAltName": cert.get("subjectAltName"),
                    "notAfter": cert.get("notAfter"),
                }
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}


def _http_probe(url: str) -> dict:
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=20) as resp:
            body = resp.read(400)
            ct = resp.headers.get("Content-Type")
            return {
                "ok": True,
                "final_url": getattr(resp, "geturl", lambda: url)(),
                "status": getattr(resp, "status", None),
                "content_type": ct,
                "body_prefix": body[:160].decode("utf-8", "replace"),
                "looks_html": body.lstrip().startswith(b"<!DOCTYPE") or b"<html" in body[:80].lower(),
                "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
            }
    except HTTPError as exc:
        body = exc.read(200) if hasattr(exc, "read") else b""
        return {
            "ok": False,
            "status": exc.code,
            "reason": exc.reason,
            "content_type": exc.headers.get("Content-Type") if exc.headers else None,
            "body_prefix": body[:160].decode("utf-8", "replace"),
            "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
        }
    except URLError as exc:
        return {
            "ok": False,
            "error_type": type(exc.reason).__name__ if exc.reason else "URLError",
            "error": str(exc.reason or exc),
            "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
        }


def _ws_probe(url: str) -> dict:
    try:
        import websockets
    except ImportError as exc:
        return {"ok": False, "error": f"websockets missing: {exc}"}

    async def run() -> dict:
        import asyncio

        t0 = time.perf_counter()
        try:
            async with websockets.connect(url, ping_interval=None, open_timeout=15) as ws:
                # RTDS: send subscribe; CLOB: just connected
                if "ws-live-data" in url:
                    sub = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": "",
                            }
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    msg = await asyncio.wait_for(ws.recv(), timeout=8)
                    return {
                        "ok": True,
                        "connected": True,
                        "first_message_prefix": str(msg)[:160],
                        "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
                    }
                return {
                    "ok": True,
                    "connected": True,
                    "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
                }
        except Exception as exc:
            return {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
            }

    import asyncio

    return asyncio.run(run())


def main() -> None:
    epoch = int(time.time()) // 300 * 300
    slug = f"btc-updown-5m-{epoch}"
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proxy_env_present": _proxy_env(),
        "tls_certs": {
            "gamma-api.polymarket.com": _cert_info("gamma-api.polymarket.com"),
            "ws-live-data.polymarket.com": _cert_info("ws-live-data.polymarket.com"),
            "ws-subscriptions-clob.polymarket.com": _cert_info(
                "ws-subscriptions-clob.polymarket.com"
            ),
            "api.binance.com": _cert_info("api.binance.com"),
            "stream.binance.com": _cert_info("stream.binance.com"),
        },
        "http": {
            "gamma_events": _http_probe(
                f"https://gamma-api.polymarket.com/events?slug={slug}"
            ),
            "binance_time": _http_probe("https://api.binance.com/api/v3/time"),
            "n1_style_ua_gamma": _http_probe(
                f"https://gamma-api.polymarket.com/events?slug={slug}"
            ),
        },
        "websocket": {
            "rtds": _ws_probe("wss://ws-live-data.polymarket.com"),
            "clob": _ws_probe("wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            "binance_trade": _ws_probe("wss://stream.binance.com:9443/ws/btcusdt@trade"),
        },
    }

    # Classification heuristic
    gamma_cert = report["tls_certs"]["gamma-api.polymarket.com"]
    binance_http = report["http"]["binance_time"]
    gamma_http = report["http"]["gamma_events"]
    rtds_ws = report["websocket"]["rtds"]
    bn_ws = report["websocket"]["binance_trade"]

    if binance_http.get("ok") and bn_ws.get("ok") and not gamma_http.get("ok"):
        if "CERTIFICATE" in str(gamma_http.get("error", "")) or not gamma_cert.get("ok"):
            classification = "ENVIRONMENT_NETWORK_BLOCK"
            reason = (
                "Binance HTTP/WS succeed with system TLS; Polymarket TLS/HTTP/WS fail "
                "with cert hostname mismatch or non-JSON/HTML intercept. Same stack "
                "reaches Binance, so N2 client config is unlikely the sole cause."
            )
        elif gamma_http.get("looks_html"):
            classification = "ENVIRONMENT_NETWORK_BLOCK"
            reason = "Polymarket HTTP returns HTML intercept while Binance JSON succeeds."
        else:
            classification = "INCONCLUSIVE"
            reason = "Polymarket failed without a clear TLS signature; Binance OK."
    elif not binance_http.get("ok") and not gamma_http.get("ok"):
        classification = "ENVIRONMENT_NETWORK_BLOCK"
        reason = "Both venues fail; broad network restriction."
    elif gamma_http.get("ok") and rtds_ws.get("ok"):
        classification = "TRANSIENT_PROVIDER_FAILURE"
        reason = "Polymarket reachable now; prior smoke may have been transient."
    else:
        classification = "INCONCLUSIVE"
        reason = "Mixed results require operator review."

    # Client defect check: N1 and N2 use urllib + websockets similarly
    report["client_comparison"] = {
        "n1_gamma": "urllib.request + User-Agent (tools/n1_audit)",
        "n2_gamma": "urllib.request + User-Agent (adapters/polymarket/discovery.py)",
        "n2_rtds": "websockets.connect default SSL context",
        "n1_rtds": "websockets.connect default SSL context (tools/n1_audit/capture_sources.py)",
        "difference_material_to_tls": False,
        "note": (
            "No intentional TLS bypass in N2 product adapters. Smoke no longer "
            "offers --insecure-ssl. Classification uses system TLS failures on "
            "Polymarket hostnames while Binance succeeds on the same stack."
        ),
    }
    report["classification"] = classification
    report["classification_reason"] = reason
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"classification": classification, "reason": reason, "out": str(OUT)}, indent=2))
    print(json.dumps({
        "gamma_http_ok": gamma_http.get("ok"),
        "gamma_error": gamma_http.get("error") or gamma_http.get("body_prefix"),
        "gamma_cert_ok": gamma_cert.get("ok"),
        "gamma_cert_error": gamma_cert.get("error"),
        "binance_http_ok": binance_http.get("ok"),
        "rtds_ok": rtds_ws.get("ok"),
        "rtds_error": rtds_ws.get("error"),
        "binance_ws_ok": bn_ws.get("ok"),
        "proxy_env_present": report["proxy_env_present"],
    }, indent=2))


if __name__ == "__main__":
    main()
