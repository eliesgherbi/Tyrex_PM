# 08 — Validation report

## R1

| Check | Result |
|-------|--------|
| Branch `rest_project` | Pass |
| Pre-reset commit | `35f83fd986dfe91aa952787644d895bdeb1f75e4` |
| Historical tree under `old/` | Pass |
| `.env` SHA256 before/after | Match (checksum verified; value not logged here) |
| `var/` not archived | Pass |
| New package `tyrex-pm` 0.3.0 editable install | Pass — `src/tyrex_pm` |
| Distribution files contain `old/` | No (9 files; none under `old`) |
| CLI `tyrex-pm version` / `help` | Pass |
| Pytest | **10 passed** |
| NautilusTrader dependency | Absent |
| Root `RESET_PHASE_R0.md` | Removed; plan in `Docs/07_implementation_plan.md` |
| R2 started | **No** |

## Later phases

R2–R8 and Z1–Z4 results will be appended here when executed.
