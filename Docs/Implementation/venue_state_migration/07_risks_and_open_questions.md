# Risks and open questions

## Decisions resolved (team memo — do not reopen without new evidence)

| Topic | Resolution | Reference |
|-------|------------|-----------|
| **Filled deployment cost basis** | **Venue size × mark price**; missing mark → **fallback 0.5** + **`venue_state_missing_mark`** | Replaces `position_entry_deployment_usd` (`abs(signed_qty) * avg_px_open`, `deployment_budget.py` lines 47–57) when **`venue_state_reads_enabled`** is **true**; `03_venue_state_design.md` Decision 1 |
| **CLOB cash polling cadence** | Default **10 s**, **floor 3.0 s**, tunable | `03_venue_state_design.md` Decision 2; loaders |
| **Readiness gating** | **Two predicates**, **both required:** `wallet_sync_first_sync_complete` **and** `venue_state_cash_ready` | `03_venue_state_design.md`, `06_observability.md` Decision 3 |
| **Rollout / rollback** | **`venue_state_reads_enabled`**, default **`false`**; Step 4 flip **true**; **removed Step 5** | `02_architecture.md`, `04_migration.md` Decision 4 |
| **Fact schema** | Single **`venue_state`** with **`status`** fields + separate **`venue_state_missing_mark`** | `06_observability.md` Decision 5 |
| **Calendar** | **4–5 weeks** target; **full-length** soaks; **parallel** engineering during soaks | `00_overview.md`, `04_migration.md` Decision 6 |

---

## Rate limits and duplicate polling

**Risk:** CLOB poll every **10 s** (default) plus WalletSync **15 s** may still approach limits under retries.

**Mitigation:** Exponential backoff on 429; **one** HTTP per cash poll tick; avoid redundant balance calls inside the same tick.

**Judgment during implementation:** Confirm QPS vs ops if alerts fire — **not** a plan blocker.

---

## `cash_free` vs `cash_total`

**Fact:** Polymarket Nautilus adapter sets `locked=0`, `free=total` (`nautilus_trader` `adapters/polymarket/execution.py` ~287–291).

**Open:** Document whether `VenueState.cash_free()` **equals** `cash_total()` after py-clob parse — **implementation note**, not blocking.

---

## Data API vs mark source

**Open:** Verify whether Data API position rows expose any **average price** field worth logging for diagnostics — **Tier A math uses mark × size per Decision 1**, not Data API avg. Optional enhancement only.

---

## Health signal overlap

**Risk:** Operators may confuse `wallet_sync_stale` with `venue_state.status=stale`.

**Mitigation:** Runbook (`06_observability.md`) — correlate `wallet_sync` facts with `venue_state` fields.

---

## Engine position check vs Tier A

**Risk:** `exec_position_check_interval_seconds` may emit inferred fills; **does not** update `VenueState`.

**Open:** Whether to **disable** engine position checks operationally — **no upstream change**; **ops judgment**, not a code plan blocker.

---

## Step 1 stop-condition (charter)

Unchanged: Tier A site count did not exceed threshold; inventory centralizable.

**If** new Tier A reads appear during coding — update `01_inventory.md` (frozen unless changed by formal review).

---

## Timeline

**Target: 4–5 weeks** wall-clock via **parallel** work during **full** Step 2 and Step 4 soaks (`00_overview.md`, `04_migration.md`). Not a reopening of Decision 6.
