# N6 live tools

## Deterministic Scope A (FakeTransport)

```powershell
python tools/n6_live/run_n6_fixture_acceptance.py --out-dir var/reporting/n6/fixture_manual
```

## Authenticated read-only reconciliation

```powershell
python tools/n6_live/run_n6_readonly_recon.py --out-dir var/reporting/n6/readonly_manual
```

Mutations remain disabled. No submit/cancel/redeem. Historical acknowledged
positions are classified as visible and untouched.
