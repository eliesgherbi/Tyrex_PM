# Shared execution-session engine (deferred)

**Status:** deferred architectural task (not scheduled for current LIVE work)  
**Branch:** `rest_project` (tracking `origin/rest_project`)  
**Date recorded:** 2026-08-08

## Purpose

Record an explicitly deferred architectural task so it is not forgotten:

**SHADOW and LIVE should eventually use one shared execution-session engine,
differing primarily at the execution backend.**

This document is the project memory for that direction. It does **not** authorize
implementing the shared engine as part of current real-LIVE execution-lifecycle
work.

---

## Current architectural problem

The runtime layer connects strategy evaluation, planning, risk, execution,
venue evidence, settlement, reconciliation, persistence, and reporting.

Runtime / orchestration is currently the weakest architectural area. It contains
approximately **71 Python modules** and several generations of runtime paths,
including **N4, N5, N6, N7, R7**, and **continuous-SHADOW** wrappers.

Many individual abstractions are useful, but the **orchestration is fragmented**.
That fragmentation is where **SHADOW and LIVE materially diverge**.

Consequently, maintaining a feature across **OBSERVE**, **SHADOW**, and **LIVE**
can require duplicated orchestration changes and mode-specific validation.

---

## Target model

```text
    Shared strategy and lifecycle engine
                 |
           Execution port
            +----+----+
            |         |
            v         v
      Shadow backend  Live backend
      simulated fills real venue + reconciliation
```

One shared session owns strategy evaluation and lifecycle state. Backends differ
at order execution and reconciliation evidence, not at the lifecycle model.

---

## Architectural decision

**Desired long-term direction:** one shared execution-session engine for SHADOW
and LIVE, with backend-specific execution and reconciliation behind a common
execution port.

**Intentional deferral:** this work is deferred until after the next real
tiny-live execution-lifecycle validation.

**Do not** propose or implement the shared engine as part of current LIVE work.

### Current priority: reliable real-LIVE execution lifecycle

The immediate priority is to make the real-LIVE execution lifecycle reliable:

1. submit the entry;
2. determine the actual venue outcome;
3. supervise acquired exposure;
4. submit the exit;
5. follow exit matching and settlement;
6. reconcile against authoritative venue state;
7. terminate only with confirmed flatness, confirmed no-fill, or an explicit
   persisted manual-recovery state.

---

## Temporary validation policy

Near-term project policy for real-LIVE execution-lifecycle work:

1. **Execution-lifecycle changes are validated first with focused unit tests**,
   including tests that exercise the actual production LIVE composition through
   deterministic fakes at external SDK / network boundaries.

2. **After unit tests pass**, the repository owner runs **one monitored real
   tiny-live experiment** and evaluates its structured report and run logs.

3. **OBSERVE and SHADOW are not required acceptance gates** for the current
   real-LIVE execution-lifecycle work.

4. **Agents must not spend implementation scope** extending, repairing, or
   synchronizing SHADOW / OBSERVE behavior unless the owner explicitly requests
   it.

5. **Existing OBSERVE and SHADOW functionality must not be deleted or
   intentionally broken.** It is simply out of scope for current lifecycle
   development.

6. **Normal pre-submit safety checks**, authenticated read-only checks, mutation
   admission, risk limits, and terminal reconciliation **remain required inside
   the LIVE runtime**. They are safety controls, not separate OBSERVE / SHADOW
   test phases.

7. **This policy is temporary.** It does not declare SHADOW obsolete. Mode
   synchronization will be revisited through this shared execution-session task.

---

## Revisit triggers

Revisit the shared execution-session engine when any of the following holds:

- a second production strategy is added;
- multiple strategies or sessions must run concurrently;
- the same lifecycle feature repeatedly requires separate LIVE and SHADOW work;
- SHADOW must become a required production acceptance gate;
- duplicated orchestration causes inconsistent lifecycle reports or behavior;
- the current real-LIVE lifecycle has been demonstrated successfully and is
  stable enough to extract.

---

## Future acceptance direction

A future shared-session refactor should aim for:

- one strategy-to-intent path;
- one lifecycle state model;
- one execution port;
- one event / evidence reducer;
- one terminal classification model;
- backend-specific order execution and reconciliation;
- deterministic parity tests showing that the same intent sequence drives both
  backends.

**LIVE backend:** real Polymarket SDK, authenticated account evidence, settlement
tracking, persistence, and reconciliation.

**SHADOW backend:** simulate submission and fills while emitting the same
domain-level lifecycle events.

---

## Explicit non-goals (current phase)

- Implementing the shared execution-session engine now
- Forcing SHADOW / OBSERVE parity as an acceptance gate for LIVE lifecycle work
- Deleting or intentionally breaking existing OBSERVE / SHADOW paths
- Treating this document as an implementation plan or milestone schedule
