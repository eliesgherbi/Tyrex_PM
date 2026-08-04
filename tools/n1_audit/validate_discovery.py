#!/usr/bin/env python3
"""Read-only discovery validation for BTC 5m Up/Down markets (N1)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

UA = "TyrexPM-N1-Audit/1.0 (read-only research)"
GAMMA = "https://gamma-api.polymarket.com/events"
WINDOW_S = 300
OUT = Path("var/recordings/n1/discovery_validation.json")


def http_json(url: str):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def map_outcomes(outcomes, token_ids):
    """Label-based mapping only. Reject positional assumptions."""
    if not outcomes or not token_ids:
        return {"ok": False, "reason": "missing_outcomes_or_tokens"}
    if len(outcomes) != len(token_ids):
        return {"ok": False, "reason": "token_count_mismatch", "outcomes": outcomes, "n_tokens": len(token_ids)}
    labels = [str(o) for o in outcomes]
    lowered = [x.lower() for x in labels]
    if lowered.count("up") != 1 or lowered.count("down") != 1:
        return {"ok": False, "reason": "missing_or_duplicate_up_down", "outcomes": labels}
    unknown = [x for x in labels if x.lower() not in ("up", "down")]
    if unknown:
        return {"ok": False, "reason": "unknown_label", "unknown": unknown, "outcomes": labels}
    up_i = lowered.index("up")
    down_i = lowered.index("down")
    # Reject if someone would map by position [0]=Up assumption when reversed
    positional_assumes_0_up = labels[0].lower() == "up"
    return {
        "ok": True,
        "mapping": {"UP": str(token_ids[up_i]), "DOWN": str(token_ids[down_i])},
        "outcomes": labels,
        "positional_0_is_up": positional_assumes_0_up,
        "note": "mapping is label-index based; never array-position without labels",
    }


def synthetic_rejection_cases():
    cases = []
    cases.append(("missing_up", map_outcomes(["Down", "Sideways"], ["1", "2"])))
    cases.append(("duplicate_up", map_outcomes(["Up", "Up"], ["1", "2"])))
    cases.append(("unknown_label", map_outcomes(["Up", "Maybe"], ["1", "2"])))
    cases.append(("token_count_mismatch", map_outcomes(["Up", "Down"], ["1"])))
    cases.append(("reversed_labels_ok", map_outcomes(["Down", "Up"], ["downTok", "upTok"])))
    # ambiguous: yes/no instead of up/down for this series
    cases.append(("wrong_labels_yes_no", map_outcomes(["Yes", "No"], ["1", "2"])))
    return [{"case": k, "result": v} for k, v in cases]


def main() -> None:
    now = int(time.time())
    base = (now // WINDOW_S) * WINDOW_S
    epochs = [base - 600, base - 300, base, base + 300]
    live = []
    for e in epochs:
        slug = f"btc-updown-5m-{e}"
        t0 = time.perf_counter()
        try:
            data = http_json(f"{GAMMA}?slug={slug}")
            latency_ms = (time.perf_counter() - t0) * 1000.0
        except Exception as exc:
            live.append({"slug": slug, "ok": False, "error": f"{type(exc).__name__}:{exc}"})
            continue
        if not data:
            live.append(
                {
                    "slug": slug,
                    "ok": False,
                    "reason": "empty_gamma",
                    "latency_ms": latency_ms,
                    "note": "future/unlisted window may be empty — do not bind prior window",
                }
            )
            continue
        ev = data[0]
        m = (ev.get("markets") or [None])[0]
        if not m:
            live.append({"slug": slug, "ok": False, "reason": "no_markets"})
            continue
        outcomes = m.get("outcomes")
        tokens = m.get("clobTokenIds")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        mapping = map_outcomes(outcomes, tokens)
        start = m.get("eventStartTime") or ev.get("startTime")
        end = m.get("endDate") or ev.get("endDate")
        # window identity checks
        slug_epoch = int(slug.rsplit("-", 1)[-1])
        start_ok = None
        if start:
            start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            start_ok = abs(start_dt.timestamp() - slug_epoch) < 1.0
        live.append(
            {
                "slug": slug,
                "ok": mapping.get("ok") and bool(start_ok),
                "latency_ms": latency_ms,
                "event_id": ev.get("id"),
                "market_id": m.get("id"),
                "condition_id": m.get("conditionId"),
                "title": ev.get("title") or m.get("question"),
                "resolution_source": m.get("resolutionSource"),
                "event_start": start,
                "event_end": end,
                "slug_epoch_matches_event_start": start_ok,
                "mapping": mapping,
                "description_excerpt": str(m.get("description") or "")[:240],
            }
        )

    # active-event fallback sketch: series search must still verify slug epoch
    fallback = {
        "policy": (
            "Primary: deterministic slug btc-updown-5m-{epoch} exact Gamma lookup. "
            "Fallback: active/series listing may be used only to discover candidate slugs; "
            "binding requires exact slug epoch == requested window start, matching conditionId, "
            "and label-based Up/Down token map. Never bind the chronologically nearest other window."
        )
    }

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live_lookups": live,
        "synthetic_rejection_cases": synthetic_rejection_cases(),
        "fallback_policy": fallback,
        "up_down_mapping_rule": {"Up": "UP", "Down": "DOWN", "method": "label_index_only"},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2)[:6000])


if __name__ == "__main__":
    main()
