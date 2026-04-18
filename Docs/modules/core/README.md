# `core/`

Shared vocabulary used by every other module. **No I/O. No business logic.** If a name belongs in two places, it usually belongs here.

## Files

| File | Purpose |
|------|---------|
| `enums.py` | `Side` (BUY/SELL), `OrderStyle` (LIMIT/MARKET), `ExecutionMode` (SHADOW/LIVE), `EventSource` |
| `ids.py` | Typed string wrappers: `RunId`, `IntentId`, `ClientOrderId`, `VenueOrderId`, `TokenId` — keep them distinct at the type level so a function never confuses one for another |
| `models.py` | All canonical dataclasses (see below) — every one is `frozen=True` |
| `events.py` | Internal bus event payloads (legacy bridge; pipeline uses dataclasses + JsonlSink directly) |
| `bus.py` | In-process event bus stub (kept for tests / future expansion) |
| `time.py` | `utc_now()`, `monotonic_s()` — the **only** clocks used in business code |
| `errors.py` | `ConfigError` and other narrow exception types |
| `reason_codes.py` | Stable string constants for risk denials, strategy skips, pipeline rejects |

## Key dataclasses (`models.py`)

| Class | Used as |
|-------|---------|
| `GuruTradeSignal` | Normalized guru activity row (post-Data-API normalization) |
| `EnterIntent` / `ExitIntent` / `ReduceIntent` / `CancelIntent` | Strategy output (`Intent` is the union) |
| `ApprovedIntent` / `ApprovedCancel` | Risk-approved intent carrying a fresh `client_order_id` |
| `RiskDecision` | Risk engine output (approve/deny + reason codes + evidence extensions) |
| `RiskContext` | Snapshot the risk engine consumes (positions, open orders, balance, in-flight reservations, health flags) |
| `WalletPosition` | Outcome-token holding (qty + optional avg price) |
| `OpenOrderView` | Merged-truth resting order view (carries `venue_state_source` for `user_ws` vs `rest`) |
| `TradeFillRecord` | User-channel trade evidence (`MATCHED` → `MINED` → `CONFIRMED`) |
| `EventEnvelope` | Generic bus envelope for `bus.py` |

## Conventions

- Money/sizes/prices are `Decimal` everywhere. Quantization for *display* lives in `risk/evidence_format.py`, never in these dataclasses.
- Datetimes are timezone-aware UTC.
- Ids are wrapped at the boundary; business code shouldn't see raw `str`.

## Adding things here

Add a new dataclass / id / enum here only when **two or more modules** would import it. One-module-only types stay in that module.
