# R7D.2 — Operator handoff preparation

**Prerequisite:** R7D.1 on `696ba94`.  
**Scope:** architecture + documentation + read-only readiness. **No live trade by the agent.**

## 1. Acknowledgment policy source

After `position_acknowledgment.json` is deleted, `r7-ack-regenerate` does **not** invent identities from every resolved account position.

| Path | Role |
|------|------|
| `config/r7/acknowledgment_policy.json` | **Sealed source of truth** (committed) |
| `var/state/r7/acknowledgment_policy.json` | Durable runtime mirror |
| `var/state/r7/position_acknowledgment.json` | Regenerated artifact (disposable only if policy remains) |
| `var/reporting/**` | Disposable reports — never the policy source |

Regeneration matches inventory rows to the exact four `condition_id|token_id` identities. Proof properties:

- only those four identities can be regenerated into the ack set;
- an unexpected fifth resolved position is never auto-acknowledged;
- a changed token or condition ID → `ACK_POLICY_IDENTITY_MISSING_OR_CHANGED`;
- selection cannot broaden the policy (`ACK_POLICY_BROADEN_OR_SHRINK_FORBIDDEN`);
- missing sealed policy → fail closed (`ACK_POLICY_MISSING`).

Module: `src/tyrex_pm/runtime/r7_ack_policy.py`.

## 2. Lifecycle residual registry

Replaces single-purpose `lifecycle_dust.json` modeling.

| Path | Role |
|------|------|
| `var/state/r7/lifecycle_residuals.json` | Multi-record residual registry |
| `var/state/r7/lifecycle_dust.json` | Legacy scalar; migrated into registry |

Identity key: `condition_id|token_id|originating_run_id`.

Cleanup policy: **`NONE`** — no sell, redemption, merge/split, transfer, approval, or on-chain cleanup.

## 3. Authorization model (current)

**Supersedes** R7A.2 session nonce / verbatim statement / chat authorization artifact.

- No verbatim authorization statement required.
- No session nonce or chat authorization artifact required.
- The agent must **never** run the live command.
- Operator manual invocation with `--execute-live` enables mutations for **that one process only**.
- Absence of `--execute-live` remains read-only.
- One process still permits only one bounded lifecycle.
- The $5 fee-inclusive BUY limit remains mandatory.

Stale blocker `R7B_AUTHORIZATION_ABSENT` is retained only for legacy `authorization_model=legacy_session` proposal readiness; the operator CLI path does not use it.

## 4. Operator live command (do not run from agent)

See final R7D.2 handoff response for the exact Git Bash command. Requires clean worktree (no `--allow-dirty-worktree`).
