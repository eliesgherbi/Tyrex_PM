"""Investigate live_test_reservation_life_cycle: validate the in-flight reservation patch.

Goals:
 1. Run envelope (lifetime, fact mix, any crash trace).
 2. Risk decisions: approve/deny breakdown by reason; new evidence fields populated?
 3. OMS rejects: did the 'not enough balance / allowance' burst go away?
 4. Reservation lifecycle: does in_flight_reserved_usd_total ramp up then drop to 0?
 5. Cross-check timing: when reservations are non-zero, do approves stop / denies start?
 6. Reconcile health (drift, tombstones, blocking).
 7. Any unexpected anomalies.
"""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

P = Path("var/reporting/runs/live_test_reservation_life_cycle/facts.jsonl")
rows = [json.loads(l) for l in P.read_text(encoding="utf-8").splitlines()]
by_type: dict[str, list[dict]] = defaultdict(list)
for r in rows:
    by_type[r.get("fact_type", "?")].append(r)


def t(s): return (s or "")[11:23]


def to_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


print("=== RUN ENVELOPE ===")
print(f"  total facts: {len(rows)}")
print(f"  first ts:    {rows[0]['ts']}")
print(f"  last  ts:    {rows[-1]['ts']}")
dt_range = (to_dt(rows[-1]['ts']) - to_dt(rows[0]['ts'])).total_seconds()
print(f"  duration:    {dt_range:.1f} s")
print(f"  fact types:  {dict(Counter(r['fact_type'] for r in rows).most_common())}")

print("\n=== RISK DECISIONS ===")
ok = [r for r in by_type["risk_decision"] if (r.get("payload") or {}).get("approved")]
no = [r for r in by_type["risk_decision"] if not (r.get("payload") or {}).get("approved")]
print(f"  approved={len(ok)}  denied={len(no)}")
ctr = Counter(tuple(r.get("payload", {}).get("reason_codes") or ()) for r in no)
for k, n in ctr.most_common():
    print(f"    {n:3d}  {k}")

# Are the new in-flight evidence fields populated?
print("\n=== NEW IN-FLIGHT EVIDENCE FIELDS COVERAGE ===")
buy_decisions = [
    r for r in by_type["risk_decision"]
    if (r.get("payload") or {}).get("notional_max_usd") is not None  # cheap proxy: BUY-style payload
]
have_total = sum(1 for r in buy_decisions if "in_flight_reserved_usd_total" in (r.get("payload") or {}))
have_count = sum(1 for r in buy_decisions if "in_flight_reservation_count" in (r.get("payload") or {}))
have_per_token = sum(1 for r in buy_decisions if "in_flight_reserved_usd_by_token" in (r.get("payload") or {}))
have_eff_balance = sum(1 for r in buy_decisions if "effective_free_balance_usd" in (r.get("payload") or {}))
print(f"  BUY-ish risk_decisions: {len(buy_decisions)}")
print(f"    in_flight_reserved_usd_total present:    {have_total}")
print(f"    in_flight_reservation_count present:     {have_count}")
print(f"    in_flight_reserved_usd_by_token present: {have_per_token}")
print(f"    effective_free_balance_usd present:      {have_eff_balance}")

print("\n=== OMS REJECT CLUSTER ===")
print(f"  total oms_reject: {len(by_type['oms_reject'])}")
ctr = Counter()
for r in by_type["oms_reject"]:
    p = r.get("payload") or {}
    em = p.get("error_msg") or {}
    if isinstance(em, dict):
        s = em.get("error", "?")
    else:
        s = str(em)
    # Bucket on prefix
    bucket = s.split(":")[0][:60]
    ctr[bucket] += 1
for k, n in ctr.most_common():
    print(f"    {n:3d}  {k}")

print("\n=== OMS SUBMITS ===")
print(f"  count: {len(by_type['oms_submit'])}")
for r in by_type["oms_submit"]:
    p = r.get("payload") or {}
    raw = p.get("oms_result") or "{}"
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        body = {}
    print(f"  {t(r['ts'])}  vid={(body.get('orderID') or '')[:18]}  status={body.get('status')}")

print("\n=== RESERVATION LIFECYCLE TIMELINE ===")
# For every BUY risk_decision in chronological order, print the reservation total & count.
events: list[tuple[str, str, str, int, str, list]] = []
for r in by_type["risk_decision"]:
    p = r.get("payload") or {}
    if "in_flight_reserved_usd_total" not in p:
        continue
    approved = p.get("approved")
    rcs = p.get("reason_codes") or []
    total = p.get("in_flight_reserved_usd_total")
    cnt = p.get("in_flight_reservation_count")
    eff = p.get("effective_free_balance_usd")
    events.append((r["ts"], "approve" if approved else "deny", total, cnt, eff or "-", rcs))

# Bucket: how many approves/denies fired with non-zero reservations?
nonzero = [e for e in events if e[2] and Decimal(e[2]) > 0]
zero = [e for e in events if not e[2] or Decimal(e[2]) == 0]
print(f"  total BUY decisions w/ in-flight evidence: {len(events)}")
print(f"    fired with reservations > 0: {len(nonzero)}")
print(f"    fired with reservations == 0: {len(zero)}")
print()
print("  First 25 BUY decisions chronologically (ts | approve/deny | reserved_usd | count | effective_free | reasons):")
for e in events[:25]:
    print(f"    {t(e[0])}  {e[1]:7s}  reserved={str(e[2])[:8]:<8s}  cnt={e[3]:>2}  eff_free={str(e[4])[:8]:<8s}  reasons={e[5]}")

# Peak reservation total
if events:
    peak = max(events, key=lambda e: Decimal(e[2]) if e[2] else Decimal(0))
    print(f"\n  Peak reservation observed: total={peak[2]}  count={peak[3]} at {peak[0]}")

print("\n=== TIMING: did reservations actually get released? ===")
# Look for transitions: high reservation -> 0 reservation = release
prev_total = None
transitions = 0
for e in events:
    cur = Decimal(e[2]) if e[2] else Decimal(0)
    if prev_total is not None and prev_total > 0 and cur == 0:
        transitions += 1
    prev_total = cur
print(f"  release events (reserved>0 → reserved=0): {transitions}")

# Examine the 'capital_deny_kind' field for any insufficient_capital denies
print("\n=== CAPITAL DENIES (new local gate vs. venue gate) ===")
cap_denies = [r for r in no if "insufficient_capital" in (r.get("payload",{}).get("reason_codes") or ())]
print(f"  insufficient_capital denies (local, NEW): {len(cap_denies)}")
for r in cap_denies[:6]:
    p = r.get("payload") or {}
    print(f"    {t(r['ts'])}  bal={p.get('wallet_usdc_balance')} resv={p.get('in_flight_reserved_usd_total')} eff={p.get('effective_free_balance_usd')} need={p.get('intent_need_usd')}")

print("\n=== RECONCILE HEALTH ===")
df = Counter()
bdf = Counter()
tomb_count = 0
for r in by_type["reconcile"]:
    p = r.get("payload") or {}
    for f in (p.get("drift_flags") or []):
        df[f] += 1
    for f in (p.get("blocking_drift_flags") or []):
        bdf[f] += 1
    if p.get("tombstoned_rest_vids"):
        tomb_count += 1
print(f"  reconcile facts: {len(by_type['reconcile'])}")
print(f"  drift_flags: {dict(df)}")
print(f"  blocking_drift_flags: {dict(bdf)}")
print(f"  reconciles with tombstoned_rest_vids: {tomb_count}")

print("\n=== STRATEGY SKIPS ===")
for k, n in Counter((r.get("payload") or {}).get("reason") or "?" for r in by_type["strategy_skip"]).most_common():
    print(f"  {n:3d}  {k}")

print("\n=== HEALTH ===")
for r in by_type["health"]:
    print(f"  {t(r['ts'])}  {r['payload']}")

print("\n=== Last 6 facts (chronological) ===")
for r in rows[-6:]:
    print(f"  {t(r['ts'])}  {r.get('fact_type')}  payload_keys={list((r.get('payload') or {}).keys())[:6]}")
