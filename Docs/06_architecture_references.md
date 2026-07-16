# 06 — Architecture references

**Engine decision: complete.** Tyrex_PM uses its own minimal event-driven engine.

## NautilusTrader status

| Use | Allowed? |
|-----|----------|
| Design-pattern study | Yes |
| Dependency / install / import | **No** |
| PoC, facade, migration path | **No** |
| Future engine candidate | **No** |

Do not add `nautilus_trader` to this project.

## Concepts that may inspire smaller Tyrex equivalents

When patterns are borrowed later, document for each: problem solved, NT concept, whether Tyrex needs it, smaller equivalent, Polymarket adaptation, why not copying the full abstraction.

Useful inspiration themes (not commitments to copy):

- Event-driven component communication
- Commands versus events
- Central portfolio truth
- Strategy lifecycle callbacks
- Order/fill lifecycle events
- Live vs simulated execution behind one interface
- Restart reconciliation
- Composition root wiring
- Clock-driven timers
- Deterministic handler ordering

Official docs for study only: https://nautilustrader.io/docs/latest/

## Deliberately rejected for Tyrex

- Full multi-venue enterprise platform shape
- Large distributed/message-bus machinery
- Reproducing NT type systems inside strategies
- Dual Tyrex/NT runtime
- Delaying delivery for NT integration research

## Overengineering risk

Copying NT breadth would recreate the accidental complexity the reset removes. Prefer the smallest interface justified by `ReferenceMomentumStrategy`, then Z-Gap.
