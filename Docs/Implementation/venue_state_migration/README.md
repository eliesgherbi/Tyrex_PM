# VenueState migration — historical material

This directory contains **migration-era** design notes, step plans, and inventory spreadsheets from the VenueState / WalletSync rollout.

**It is not authoritative** for current behavior. For production architecture, operators, and YAML truth:

- **[LIVE_ARCHITECTURE.md](../../LIVE_ARCHITECTURE.md)**
- **[road_map.md](../road_map.md)** (split Tier A / Tier B governance)
- **Code:** `src/tyrex_pm/runtime/venue_state.py`, `wallet_sync.py`, `state_readers.py`, `guru_compose.py`

Documents here may still mention removed flags (`venue_state_reads_enabled`) or deleted position-reconciliation paths; treat those as **historical context** only.
