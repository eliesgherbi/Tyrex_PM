from __future__ import annotations

FACT_SCHEMA_VERSION = 2

FACT_TYPE_GURU_SIGNAL = "guru_signal"
FACT_TYPE_STRATEGY_SKIP = "strategy_skip"
FACT_TYPE_INTENT = "intent_created"
FACT_TYPE_RISK = "risk_decision"
FACT_TYPE_OMS_SUBMIT = "oms_submit"
FACT_TYPE_OMS_REJECT = "oms_reject"
FACT_TYPE_OMS_CANCEL = "oms_cancel"
FACT_TYPE_OMS_RESULT = "oms_result"
FACT_TYPE_HEALTH = "health"
FACT_TYPE_RECONCILE = "reconcile"
FACT_TYPE_GURU_POLL = "guru_poll"
FACT_TYPE_LIVE_ATTEST = "live_attest"
#: Emitted after a successful REST wallet refresh (open orders + balance/allowance, optionally
#: positions). Lets operators see the cadence and content of the positions/balance safety net
#: without having to infer it from the absence of symptoms. See ``runtime.pipeline.emit_wallet_sync``.
FACT_TYPE_WALLET_SYNC = "wallet_sync"
#: Scheduled exit / sell_test lifecycle (P3.5): pending, arm attempts, terminal SELL outcomes.
FACT_TYPE_EXIT_LIFECYCLE = "exit_lifecycle"
#: Per-strategy token allocation mutations (P4): buy/sell/reserve/clamp.
FACT_TYPE_ALLOCATION_LEDGER = "allocation_ledger"

# Canonical key inside oms_submit / oms_cancel payloads for venue response summary string.
OMS_RESULT_PAYLOAD_KEY = "oms_result"
