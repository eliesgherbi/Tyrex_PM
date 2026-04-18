"""Drill-down on the single oms_reject and the reservation-release lifecycle."""
from __future__ import annotations
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

P = Path("var/reporting/runs/live_test_reservation_life_cycle/facts.jsonl")
rows = [json.loads(l) for l in P.read_text(encoding="utf-8").splitlines()]


def t(s): return (s or "")[11:23]


def to_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


print("=== THE LONE OMS_REJECT (full payload) ===")
for r in rows:
    if r.get("fact_type") == "oms_reject":
        print(f"  ts={r['ts']}")
        print(f"  payload={json.dumps(r.get('payload') or {}, indent=2, default=str)[:1200]}")
        print(f"  correlation_id={r.get('correlation_id')}")

print("\n=== Approves (10) vs Submits (9): which approve never reached venue? ===")
approves = [r for r in rows if r.get("fact_type") == "risk_decision" and (r.get("payload") or {}).get("approved")]
submits = [r for r in rows if r.get("fact_type") == "oms_submit"]
rejects = [r for r in rows if r.get("fact_type") == "oms_reject"]
print(f"  approve corr_ids: {[r.get('correlation_id','?')[:14] for r in approves]}")
print(f"  submit  corr_ids: {[r.get('correlation_id','?')[:14] for r in submits]}")
print(f"  reject  corr_ids: {[r.get('correlation_id','?')[:14] for r in rejects]}")
sub_ids = {r.get("correlation_id") for r in submits}
rej_ids = {r.get("correlation_id") for r in rejects}
for r in approves:
    cid = r.get("correlation_id")
    if cid not in sub_ids and cid not in rej_ids:
        print(f"  ORPHAN APPROVE (no submit, no reject): {t(r['ts'])} corr={cid}")
        # find next fact for this correlation
        ts = r["ts"]
        for r2 in rows:
            if r2.get("correlation_id") == cid and r2["ts"] > ts and r2.get("fact_type") not in ("intent_created","risk_decision","guru_signal"):
                print(f"     ↳ next fact: {t(r2['ts'])}  {r2.get('fact_type')}  payload={json.dumps(r2.get('payload') or {}, default=str)[:400]}")
                break

print("\n=== RESERVATION TIMELINE: every transition (zero <-> non-zero) ===")
prev = Decimal("0")
prev_ts = None
events = []
for r in rows:
    if r.get("fact_type") != "risk_decision":
        continue
    p = r.get("payload") or {}
    if "in_flight_reserved_usd_total" not in p:
        continue
    cur = Decimal(p["in_flight_reserved_usd_total"])
    if prev_ts is not None and cur != prev:
        delta = cur - prev
        print(f"  {t(r['ts'])}  reserved {prev} -> {cur}  (delta={delta:+}, count {p.get('in_flight_reservation_count')})  approved={p.get('approved')}")
    prev = cur
    prev_ts = r["ts"]

print("\n=== RESERVATION CLEAR LATENCY (approve -> next zero-reservation decision) ===")
# For each "approve while reserved=0", find the time until the NEXT decision shows reserved=0 again.
last_approve_ts = None
for r in rows:
    if r.get("fact_type") != "risk_decision":
        continue
    p = r.get("payload") or {}
    if "in_flight_reserved_usd_total" not in p:
        continue
    res = Decimal(p["in_flight_reserved_usd_total"])
    if p.get("approved") and res == 0:
        last_approve_ts = r["ts"]
    elif last_approve_ts is not None and res == 0:
        latency = (to_dt(r["ts"]) - to_dt(last_approve_ts)).total_seconds()
        print(f"  approve at {t(last_approve_ts)} -> next zero-reservation at {t(r['ts'])}  latency={latency:.2f}s  approved_now={p.get('approved')}")
        last_approve_ts = None

print("\n=== Each oms_submit: was there a corresponding 'reserved -> 0' transition shortly after? ===")
sub_times = [(r["ts"], (json.loads(r['payload'].get('oms_result','{}')) if isinstance(r['payload'].get('oms_result'),str) else (r['payload'].get('oms_result') or {})).get('orderID')) for r in submits]
for sts, vid in sub_times:
    sdt = to_dt(sts)
    # Find next risk_decision with reserved=0 within 30s
    found = None
    for r in rows:
        if r.get("fact_type") != "risk_decision":
            continue
        rdt = to_dt(r["ts"])
        if rdt <= sdt:
            continue
        p = r.get("payload") or {}
        if "in_flight_reserved_usd_total" not in p:
            continue
        if Decimal(p["in_flight_reserved_usd_total"]) == 0:
            found = (r["ts"], (rdt - sdt).total_seconds())
            break
    print(f"  submit {t(sts)} vid={(vid or '')[:18]}  -> next reserved=0 decision at {t(found[0]) if found else 'NEVER'}  latency={f'{found[1]:.2f}s' if found else '?'}")

print("\n=== STARTUP local_open_not_on_venue (recurring edge case) ===")
for r in rows:
    if r.get("fact_type") != "reconcile":
        continue
    p = r.get("payload") or {}
    if "local_open_not_on_venue" in (p.get("drift_flags") or []):
        print(f"  {r['ts']}")
        rd = p.get("provisional_repair_decisions") or []
        for d in rd:
            if d.get("blocking"):
                print(f"    repair: cid={d.get('client_order_id')} vid={d.get('venue_order_id')} ack_status={d.get('ack_status')} age={d.get('ack_age_s')} reason={d.get('decision_reason')}")
