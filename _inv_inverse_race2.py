"""Deep-dive into the live_test_inverse_race run after the AttributeError fix.

Goals:
  1. Confirm zero AttributeError trace (run did not crash mid-flight)
  2. Verify each tombstoned_rest_vids reconcile fact also has clean drift/blocking
  3. Find the single local_open_not_on_venue blocking event — when/why
  4. Audit the 8 successful submits vs the 48 rejects — what got accepted, what got cancelled/filled
  5. Cross-reference tombstoned vids back to their oms_submit + venue lifecycle
"""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from pathlib import Path

P = Path("var/reporting/runs/live_test_inverse_race/facts.jsonl")
rows = [json.loads(l) for l in P.read_text(encoding="utf-8").splitlines()]
by_type: dict[str, list[dict]] = defaultdict(list)
for r in rows:
    by_type[r.get("fact_type", "?")].append(r)


def t(s): return (s or "")[11:23]


print("=== RUN ENVELOPE ===")
print(f"  total facts: {len(rows)}")
print(f"  first ts:    {rows[0]['ts']}")
print(f"  last  ts:    {rows[-1]['ts']}")
print(f"  fact types:  {dict(Counter(r['fact_type'] for r in rows).most_common())}")

# 1. Reconcile facts with tombstones — what was the drift state?
print("\n=== TOMBSTONE EFFECTIVENESS ===")
print("  Per-tombstone-fact: drift, blocking, severity")
for r in by_type["reconcile"]:
    p = r.get("payload") or {}
    tomb = p.get("tombstoned_rest_vids")
    if not tomb:
        continue
    print(f"  {t(r['ts'])}  tomb={[v[:18] for v in tomb]}  "
          f"drift={p.get('drift_flags') or []}  "
          f"blocking={p.get('blocking_drift_flags') or []}  "
          f"sev={p.get('reconcile_severity')}  "
          f"blocks_live={p.get('reconcile_blocks_live')}")

# 2. Find the lone local_open_not_on_venue event
print("\n=== LOCAL_OPEN_NOT_ON_VENUE event ===")
for r in by_type["reconcile"]:
    p = r.get("payload") or {}
    if "local_open_not_on_venue" in (p.get("drift_flags") or []):
        print(f"  ts={r['ts']}")
        print(f"    drift={p.get('drift_flags')}")
        print(f"    blocking={p.get('blocking_drift_flags')}")
        print(f"    severity={p.get('reconcile_severity')}")
        # Get the comparison row
        comps = p.get("order_comparisons") or []
        for c in comps:
            if "local_open_not_on_venue" in (c.get("flags") or []):
                print(f"    comparison: {json.dumps(c, default=str)[:600]}")
        # Repair decisions context
        rd = p.get("provisional_repair_decisions") or []
        for d in rd:
            if d.get("blocking"):
                print(f"    repair_decision: {json.dumps(d, default=str)[:600]}")

# 3. Map every tombstoned vid back to its lifecycle
print("\n=== TOMBSTONED VID LIFECYCLE ===")
all_tombs = set()
for r in by_type["reconcile"]:
    p = r.get("payload") or {}
    for v in (p.get("tombstoned_rest_vids") or []):
        all_tombs.add(v)
print(f"  unique tombstoned vids: {len(all_tombs)}")
for vid in sorted(all_tombs):
    print(f"\n  vid={vid[:24]}…")
    # find oms_submit that produced this vid
    for sub in by_type["oms_submit"]:
        sp = sub.get("payload") or {}
        raw = sp.get("oms_result") or "{}"
        try:
            body = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            body = {}
        if (body.get("orderID") or "") == vid:
            print(f"    {t(sub['ts'])}  oms_submit  status={body.get('status')}  matchedAmount={body.get('matchedAmount')}")
    # find user_ws events that mention this vid
    ws_events = by_type.get("user_ws_event", []) + by_type.get("ws_order_update", [])
    for ev in ws_events:
        ep = ev.get("payload") or {}
        if str(ep.get("venue_order_id") or ep.get("id") or "") == vid:
            print(f"    {t(ev['ts'])}  ws_event  type={ep.get('type') or ep.get('event_type')}  remaining={ep.get('remaining_size') or ep.get('remaining')}")
    # first time this vid appears in a tombstone
    for r in by_type["reconcile"]:
        if vid in ((r.get("payload") or {}).get("tombstoned_rest_vids") or []):
            print(f"    {t(r['ts'])}  reconcile  TOMBSTONE_ACTIVE  drift={(r.get('payload') or {}).get('drift_flags') or []}")
            break

# 4. Quick audit: which approved intents reached the venue vs rejected
print("\n=== APPROVED → SUBMIT/REJECT FUNNEL ===")
approved = [r for r in by_type["risk_decision"] if (r.get("payload") or {}).get("approved")]
print(f"  approved: {len(approved)}")
print(f"  oms_submit (any status): {len(by_type['oms_submit'])}")
print(f"  oms_reject (400 etc): {len(by_type['oms_reject'])}")
# Are submit + reject = approved?
print(f"  submits + rejects = {len(by_type['oms_submit']) + len(by_type['oms_reject'])}")

# 5. Burst window analysis: were all the 'not enough balance' rejects in the first ~10s?
print("\n=== REJECT TIMING ===")
rej_times = sorted(r["ts"] for r in by_type["oms_reject"])
if rej_times:
    print(f"  first reject: {rej_times[0]}")
    print(f"  last  reject: {rej_times[-1]}")
    print(f"  rejects in first 15s after first approved:")
    first_approved = approved[0]["ts"] if approved else rej_times[0]
    from datetime import datetime
    fa = datetime.fromisoformat(first_approved.replace("Z", "+00:00"))
    burst = 0
    later = 0
    for ts in rej_times:
        td = (datetime.fromisoformat(ts.replace("Z", "+00:00")) - fa).total_seconds()
        if td <= 15:
            burst += 1
        else:
            later += 1
    print(f"    in burst (<=15s): {burst}")
    print(f"    later (>15s):    {later}")

# 6. Successful submits — what happened to them?
print("\n=== SUCCESSFUL SUBMITS (≠ reject) ===")
for sub in by_type["oms_submit"]:
    sp = sub.get("payload") or {}
    raw = sp.get("oms_result") or "{}"
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        body = {}
    vid = body.get("orderID") or ""
    in_tomb = vid in all_tombs
    print(f"  {t(sub['ts'])}  vid={vid[:24]}…  status={body.get('status')}  matched={body.get('matchedAmount')}  → tombstoned_later={in_tomb}")
