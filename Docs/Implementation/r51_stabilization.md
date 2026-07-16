# R5.1 Stabilization

## Host unification

`ObserveHost` is the single orchestration path (`TradingHost` alias).  
`ShadowHost` overrides only:

* `_build_decision_context` — lifecycle + `RetryController` gates  
* `_process_transition` — OMS dispatch  

Live adapters share `runtime/live_runner.py`; `live_observe` / `live_shadow` are thin wrappers.

## Entry retry

```text
ELIGIBLE → INTENT_PENDING → PLAN_FAILED/RETRY_WAIT → ELIGIBLE (cooldown + book change)
                         → CAP_REACHED
```

* Cooldown + material book fingerprint (or timer) required for retry  
* Max attempts per signal episode  
* Risk dedup remains a backstop; each attempt has unique `attempt_id` in semantic key  

## Exit retry / residual

```text
ACTIVE → EXIT_REQUESTED → EXIT_PENDING → FLAT
                       → EXIT_RETRY_WAIT → retry / escalate → MANUAL_INTERVENTION
```

* One outstanding exit request  
* Kill switch / market-close escalate urgency  
* `TERMINAL` only from `FLAT` (residual exposure never reported as successful terminal)
