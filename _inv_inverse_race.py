"""Investigate live_test_inverse_race short run + crash."""
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


print("=== RISK DECISIONS ===")
ok = [r for r in by_type["risk_decision"] if (r.get("payload") or {}).get("approved")]
no = [r for r in by_type["risk_decision"] if not (r.get("payload") or {}).get("approved")]
print(f"  approved={len(ok)}  denied={len(no)}")
ctr = Counter(tuple(r.get("payload", {}).get("reason_codes") or ()) for r in no)
for k, n in ctr.most_common():
    print(f"    {n:3d}  {k}")

print("\n=== APPROVED INTENTS / SUBMITS ===")
for r in ok:
    print(f"  {t(r['ts'])}  approved  corr={r.get('correlation_id','')[:14]}")

print("\n=== OMS SUBMITS ===")
for r in by_type["oms_submit"]:
    p = r.get("payload") or {}
    raw = p.get("oms_result") or "{}"
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        body = {}
    print(f"  {t(r['ts'])}  vid={(body.get('orderID') or '')[:18]}  status={body.get('status')}  err={body.get('errorMsg') or '-'}")

print("\n=== OMS REJECTS ===")
for r in by_type["oms_reject"]:
    p = r.get("payload") or {}
    print(f"  {t(r['ts'])}  cid={str(p.get('client_order_id'))[:14]}  status_code={p.get('status_code')}  "
          f"err_msg={p.get('error_msg')}  fp={p.get('submit_fingerprint')}")

print("\n=== RECONCILE — drift breakdown ===")
df = Counter()
bdf = Counter()
tomb_count = 0
tomb_examples = []
for r in by_type["reconcile"]:
    p = r.get("payload") or {}
    for f in (p.get("drift_flags") or []):
        df[f] += 1
    for f in (p.get("blocking_drift_flags") or []):
        bdf[f] += 1
    if p.get("tombstoned_rest_vids"):
        tomb_count += 1
        if len(tomb_examples) < 5:
            tomb_examples.append((r.get("ts"), p.get("tombstoned_rest_vids")))
print("  drift_flags:")
for k, n in df.most_common():
    print(f"    {n:3d}  {k}")
print("  blocking_drift_flags:")
for k, n in bdf.most_common():
    print(f"    {n:3d}  {k}")
print(f"  reconcile facts with NEW tombstoned_rest_vids field: {tomb_count}")
for ts, v in tomb_examples:
    print(f"    {t(ts)}  {v}")

print("\n=== RECONCILE timing — last 10 ===")
for r in by_type["reconcile"][-10:]:
    p = r.get("payload") or {}
    print(f"  {t(r['ts'])}  drift={p.get('drift_flags')} blocks_live={p.get('reconcile_blocks_live')}  "
          f"tomb={p.get('tombstoned_rest_vids')}")

print("\n=== STRATEGY SKIPS ===")
for k, n in Counter((r.get("payload") or {}).get("reason") or "?" for r in by_type["strategy_skip"]).most_common():
    print(f"  {n:3d}  {k}")

print("\n=== HEALTH ===")
for r in by_type["health"]:
    print(f"  {t(r['ts'])}  {r['payload']}")

# When did the crash occur?
print("\n=== Last 5 facts (chronological) ===")
for r in rows[-5:]:
    print(f"  {t(r['ts'])}  {r.get('fact_type')}  payload_keys={list((r.get('payload') or {}).keys())[:6]}")
