# Milestone v1.00 — Venue, wallet, and API credential validation

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.00 |
| **Title** | Venue, wallet, and API credential validation |
| **Status** | Done |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §7](./implementation_plan.md#7-milestone-overview) · [Specification.md](./Specification.md) |
| **Upstream dependencies** | *None — first gated milestone* |
| **Blocking approvals** | §9 — required before **v1.01** or **v1.02** |
| **Approval required from** | Technical lead · Operator / account owner |
| **Target branch / PR** | `milestone/v1_00-venue-auth` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 — complete; human auth validated |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Demonstrate that the **operator’s Polymarket wallet model** is correctly understood and that **L1 (private key / EIP-712)** and **L2 (API key, secret, passphrase)** credentials can be **created or derived** and used for at least one **authenticated read** (e.g. balance or allowance) via the **same stack** Nautilus/py-clob expects—before repository modules depend on secrets.

---

## 2. Scope

- Document and execute a **repeatable** procedure to set:
  - `signature_type` ∈ {0, 1, 2} matching the operator account
  - `funder` (proxy/EOA) per [Polymarket authentication](https://docs.polymarket.com/developers/CLOB/authentication)
  - L2 credentials (`api_key`, `secret`, `passphrase`) via `create_or_derive` pattern
- Capture **environment variable names** aligned with Nautilus `Polymarket*Config` (`POLYMARKET_PK`, `POLYMARKET_FUNDER`, `POLYMARKET_API_KEY`, etc.) in **operator runbook** `Docs/Runbooks/polymarket_operator_v1_00.md` (or path agreed in PR).
- Perform **one successful L2-authenticated GET** (e.g. `get_balance_allowance`, `get_orders`, or other documented **read** L2 method) and attach **redacted** notes (no secrets).
- Commit **`scripts/verify_polymarket_auth.py`** that loads **optional** repo-root `.env` (shell env **overrides** `.env`) and implements the runbook path **without printing secrets**.

---

## 3. Out of Scope

- Platform package layout beyond minimal script + runbook, `GuruMonitorActor`, `CopyStrategy`, or trading logic
- Placing **market-making** or copy trades (reserved for **v1.02** / **v1.08+**)
- Production secret management (Vault/KMS)—only document **local dev** pattern
- Email Magic wallet **export** walkthrough beyond “must export PK per Polymarket docs” if `signature_type=1`

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **Prior milestone approval** | *None* |
| **External** | Polymarket account; Polygon-compatible key; Python 3.10+; `py-clob-client` installable |
| **Evidence that must exist before v1.01** | This milestone §9 sign-off **recorded** (PR description, `Docs/evidence/v1_00_signoff.md`, or ticket attachment) listing reviewer names + date |
| **Pin** | Optional: record proposed `py-clob-client` lower bound in runbook; full lock deferred to **v1.11** unless team adopts early `Docs/dependency_lock.md` |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| **Operator runbook** | `Docs/Runbooks/polymarket_operator_v1_00.md`: env vars, derive creds, L2 read, troubleshooting (`INVALID_SIGNATURE`, wrong funder) |
| **Verification script** | `scripts/verify_polymarket_auth.py` (repo root; `pip install -e .`) |
| **Evidence log** | Timestamp, **method name** called, HTTP status or API result **summary** (redacted) — attach to PR or `Docs/evidence/` |

---

## 6. Acceptance Criteria

1. L2 credentials are **derived or created** without error using the chosen `signature_type` and `funder`.
2. At least **one L2-authenticated** client call succeeds (non-error API result) for a **read** operation listed in [Polymarket L2 / client documentation](https://docs.polymarket.com/developers/CLOB/clients/methods-l2).
3. Runbook explicitly states **which address** signs orders vs **which address** holds balances for the operator’s wallet type.
4. No private keys, API secrets, or passphrases appear in committed files or evidence attachments.

---

## 7. Review evidence (standard pack)

### Required test commands

- From repo root: `python scripts/verify_polymarket_auth.py` — exit code **0** on success path. Secrets may be supplied via **shell only**, **`.env` only**, or **both** (precedence: shell over `.env` per runbook).
- Optional: `ruff check scripts/verify_polymarket_auth.py` if `ruff` is available in repo (may be **N/A** until **v1.03**).

### Required log or output artifacts

- Redacted console transcript: lines proving “derive OK” and “L2 read OK” **without** printing `secret` / `passphrase` / full API key.
- Optional: HTTP status + endpoint name table (no response bodies containing secrets).

### Required config or examples

- Runbook + **`.env.example`** listing **exact** env var names: `POLYMARKET_PK`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_PASSPHRASE`, optional `TYREX_PM_DOTENV`, `POLYMARKET_CLOB_HOST`, `POLYMARKET_CHAIN_ID`.

### Required demo scenario

- Reviewer runs script **once** on a **non-production** machine with operator-present credentials; confirms **one** successful read matches runbook.

### Required design or ADR references

- **N/A** (first milestone). Link **Polymarket auth** + **Nautilus Polymarket env** docs in runbook **References** section.

### Required reviewer sign-off inputs

- Completed checklist in runbook or PR template: `signature_type = …`, `funder = 0x…` (public), “L2 read method = …”, reviewers **names + date**.

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Wrong `funder` for proxy wallets | All later trading fails | Cross-check Polymarket profile/settings before sign-off |
| Lost valid nonce for API key creation | BLOCKED | Use derive path; document recovery per Polymarket auth doc |
| Using EOA path (0) when account is proxy | Signature errors | Force explicit gate on wallet type |

---

## 9. Approval gate

| Role | Responsibility |
|------|----------------|
| **Technical lead** | Confirms script matches documented env names and does not leak secrets |
| **Operator / account owner** | Confirms wallet type, funder, and live read against **their** account |

**What must be reviewed**

- Runbook accuracy vs official Polymarket auth doc
- Script source (no secret logging)
- Redacted evidence attachment

**Conditions that block v1.01 / v1.02**

- Missing **written** sign-off with reviewer names + date
- Any secret material in git history or PR artifacts

**Before starting work on**

- **v1.01** (instrument/market validation against live APIs requiring stable auth context): **v1.00 §9 must be Approved**
- **v1.02** (supervised order): **v1.00** and **v1.01** must both be **Approved**

**Sign-off template**

> Milestone **v1.00** **Approved**. Wallet model and L2 read path validated for operator ___ on ___ (YYYY-MM-DD). Evidence: ___ (link). Reviewers: ___ / ___
