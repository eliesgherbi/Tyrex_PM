import json

with open(r"e:\polymarket\Tyrex_PM\var\reporting\runs\posi_recon5\facts.jsonl", encoding="utf-8") as f:
    facts = [json.loads(line) for line in f if line.strip()]

print(f"Total facts: {len(facts)}")
print()

types = {}
for d in facts:
    ft = d.get("fact_type", "?")
    types[ft] = types.get(ft, 0) + 1
for ft, ct in sorted(types.items(), key=lambda x: -x[1]):
    print(f"  {ft}: {ct}")

print()
print("=== TIMELINE (key events) ===")
for d in facts:
    ft = d.get("fact_type", "")
    ts = d.get("recorded_at_utc", "")[-12:]
    if ft == "deployment_budget":
        pd_val = d.get("portfolio_deploy_usd", "?")
        pf_val = d.get("portfolio_filled_usd", "?")
        pp_val = d.get("portfolio_pending_usd", "?")
        print(f"{ts}  BUDGET: portfolio_deploy={pd_val}  filled={pf_val}  pending={pp_val}")
    elif ft == "position_reconciliation":
        iid = d["instrument_id"][-20:]
        vq = d["venue_qty"]
        cq = d["cache_qty"]
        dd = d["diff_direction"]
        rs = d["reconciliation_sent"]
        dc = d.get("defer_count", 0)
        print(f"{ts}  RECON: ...{iid}  venue={vq}  cache={cq}  dir={dd}  sent={rs}  defer={dc}")
    elif ft == "risk_decision" and not d.get("allowed"):
        gate = d.get("gate", "")
        pd_val = d.get("portfolio_deploy_at_eval", "?")
        print(f"{ts}  RISK_DENY: gate={gate}  portfolio_deploy={pd_val}")
    elif ft == "risk_decision" and d.get("allowed"):
        pd_val = d.get("portfolio_deploy_at_eval", "?")
        print(f"{ts}  RISK_OK: portfolio_deploy={pd_val}")
    elif ft == "fill":
        iid = d.get("instrument_id", "")[-30:]
        qty = d.get("last_qty", "?")
        px = d.get("last_px", "?")
        side = d.get("order_side", "?")
        print(f"{ts}  FILL: ...{iid}  qty={qty}  px={px}  side={side}")
    elif ft == "execution_outcome":
        out = d.get("outcome", "")
        stg = d.get("stage", "")
        print(f"{ts}  EXEC: outcome={out} stage={stg}")
    elif ft == "order_submit":
        iid = d.get("instrument_id", "")[-30:]
        side = d.get("order_side", "?")
        qty = d.get("quantity", "?")
        px = d.get("price", "?")
        print(f"{ts}  SUBMIT: ...{iid}  side={side}  qty={qty}  px={px}")
