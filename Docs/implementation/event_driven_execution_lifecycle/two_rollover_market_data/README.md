# Level 5 — Two-rollover operational market-data

**Status:** BLOCKED (external validation duration gate)

Public venue read probes from this host succeeded (`clob.polymarket.com/time`,
`gamma-api.polymarket.com`), but Level 5 requires a mutation-disabled
OBSERVE/SHADOW session across **at least two consecutive BTC 5-minute
rollovers** (~10+ minutes). That long-lived operator host run was **not**
executed in this Phase 9 agent session.

- `venue_mutations_attempted = 0`
- `tiny_live_admitted = false` (unchanged)
- Live admission remains disabled

See `blocker_note.json` for the exact blocker classification and operator
command to complete Level 5 on a venue-reachable host.
