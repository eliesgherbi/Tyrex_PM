# Runtime quality hardening report

## Outcome

The unified LIVE runtime now distinguishes execution infrastructure, strategy-input eligibility, and economic signal selection. Wi-Fi latency is measured and reported without blocking the asyncio loop or force-cancelling shared SDK reads. No venue or chain mutation was performed during this implementation.

## Corrections

1. Clock synchronization uses a five-second rolling loop and selects the lowest-round-trip fresh Binance time observation. Offset is calculated against the request midpoint; uncertainty, offset, source RTTs, staleness, threshold, and readiness reason are reported.
2. The tiny-live policy retains an immutable `EXACT_AT_START` Chainlink PTB and allows a bounded 30-second arrival delay. Exactness, arrival policy, entry blockers, and non-blocking provenance warnings are separate report fields.
3. Runtime `entry_executable` is reported as `execution_infrastructure_ready`; strategy decisions independently publish `strategy_inputs_eligible`.
4. Z-Gap readiness retains a deterministic primary reason and every simultaneous blocker.
5. Active/prepared account observations are single-flight and sequential on the shared async SDK client. SDK transport timeouts own cancellation; slow operations and full exception chains are evidence.
6. Market deadlines use corrected-authoritative time, durations use monotonic time, and reports label raw/corrected/monotonic fields.
7. Synchronous public REST book bootstrap/recovery runs in worker threads and DESYNC recovery is coalesced while WebSocket ingestion continues. The store still rejects crossed books and restores only from an authoritative snapshot.
8. Report schema 3 includes `furthest_stage`, complete strategy blocker counts, and `validation_scope`, so a run that never reaches an execution session cannot claim BUY/EXIT validation.

## Tiny-live policy values

- Maximum total debit remains 5 USDC.
- Maximum clock uncertainty: 750 ms.
- Maximum exact-PTB arrival lag: 30,000 ms.
- Book and candidate freshness/final dispatch gates are unchanged.

These are bounded five-minute-strategy operational policies, not general permission to use approximate time or substitute a non-Chainlink boundary value.

## Official interface alignment

- Polymarket market-channel `book` snapshots and `price_change` level updates remain the authoritative reconstruction inputs.
- The official async SDK remains the private account/order boundary.
- No custom order transport, allowance mutation, clock-setting operation, or fallback execution path was introduced.

References:

- https://docs.polymarket.com/market-data/websocket/overview
- https://docs.polymarket.com/market-data/websocket/market-channel
- https://github.com/Polymarket/py-sdk

## Validation

- Focused hardening tests: 46 passed.
- Unit plus supported contract selection: 153 passed.
- Complete discovered suite: 167 passed, 1 environment-only failure.
- Ruff: all checks passed.
- Compileall: passed.
- Unified run-config validation: passed.

The remaining test cannot import a Python 3.11 native `regex`/`pydantic_core` wheel under the available Python 3.12 validation runtime. The project `.venv` points to a removed Python 3.11 executable. This is an environment repair item, not a product-code failure.
