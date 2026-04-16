# Scenario: `position_reconciliation_validation` — **obsolete**

**Position reconciliation** (synthetic `PositionStatusReport` closes, `position_reconciliation` facts) has been **removed** from Tyrex. Config keys and facts from that era **do not exist** in current loaders.

**Use instead:** [`../venue_state_live/`](../venue_state_live/) for live validation with **VenueState** + **WalletSync**, and **[`Docs/LIVE_ARCHITECTURE.md`](../../../Docs/LIVE_ARCHITECTURE.md)** for the truth model.

This folder is retained only so old command lines fail visibly or can be migrated; do **not** treat `RUNBOOK.md` here as current operations guidance.
