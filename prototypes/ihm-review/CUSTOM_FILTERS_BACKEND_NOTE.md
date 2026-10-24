# Custom window-set filters — frontend handoff

21 October 2026. User-requested extension to the reviewed Data Library journey. Prototype only; this note does not authorize backend implementation. The agreed behavior is now in section 11.4 of `Docs/implementation/v_1_goal/03_reusable_workflows.md` and in DL-2 of `step_03_data_library_backlog.md`. The prototype key `manual` remains a fixture name; the contract name is `custom_window_set`.

## Intended interaction

Custom window set replaces the requirement to tick every manual case. Select dates, collection/source, market, market outcome, label provenance, daily hours, time zone and weekdays. Default to all matches. Individual picks are an optional refinement of those matches. Input requirements, warm-up, duplicate/reference choices and assessment quality remain a separate eligibility layer. A preview shows both matching candidates and the final included/excluded windows with reasons.

Example: within these UTC dates, include development windows starting from 08:00 inclusive to 20:00 exclusive each selected day, whose final outcome is Up. Optionally require provider confirmation rather than payout-derived proxy labels.

Up/Down means the market outcome, not the strategy position or profitability. Unknown/unresolved stay separate. An outcome or label-source filter marks the set as an outcome-conditioned diagnostic population. It can support an eligible diagnostic research experiment; this status is not a blanket prohibition on replay. An incomplete-input diagnostic still requires its own supported-use check.

## Versioned filter fields

- Date range remains UTC, half-open on window start, as already agreed.
- `outcome`: any / Up / Down / unresolved / unknown.
- `label_source`: any / provider_confirmed / proxy / unknown.
- `daily_time`: enabled flag, local start and end `HH:mm`, and IANA time zone. The sketch supports UTC and Europe/Paris. The backend defines and validates its supported zone list; no browser-clock inference.
- Daily time is half-open on the window start. If start is after end, accept `[start, midnight) OR [midnight, end)` on each allowed calendar day in that time zone. Equal endpoints are invalid when enabled; disabling the filter means all hours.
- `weekdays`: explicit ISO weekday set. It uses the same time zone as the daily clock. An empty set is invalid. Midnight and daylight-saving conversions must be defined and tested.
- `selection_mode`: all_matches / picked. For picked, explicit market/source IDs intersect the date/source scope and filter matches; excluded or out-of-scope picks cannot bypass requirements or guards.

All active filters combine with AND. Missing measurements are not zero. An implemented query returning no matches differs from an unsupported filter. Partition protection is applied before reading outcome or label-source fields, filtering, preview counts or examples. Reserved outcomes remain withheld independently of their true values.

## Preview, save and reuse

The backend resolves these filters. Preview identity includes their canonical resolved values, time semantics, scope, requirements, source choices and assessment/input identities. Save validates that preview and pins real inputs. The saved set retains both exact membership and its versioned recipe/filter definition. Reusing filters creates a new draft/preview/set; it never changes the prior selection. The browser must not resolve a large archive into client-supplied work units.

The current preview sketch keeps `manual` as an internal UI recipe key with all-matches/picked modes; do not treat this fixture key as a new authoritative backend schema. Agree how the existing `manual_cases`/custom-rule contract and shared recipe scope carry these filters. No extra execution-scenario kind is needed.

## Extensibility

Presets and custom sets should use the same filter definitions, not separate copied selection logic. A supported filter declares its field ID, value type/allowed operators, units, data dependencies, measurement/policy version, applicability and retrospective-versus-causal meaning. Unsupported filters return an explicit reason.

Reversal is a future measurement, not an inferred property of the final outcome. First agree which series (reference versus a leg's book), threshold/crossing definition, observation range, amplitude/count/duration, gap handling and measurement version. Volatility/liquidity/spread need similarly defined measurements. Do not activate a reversal template merely because the UI can display its name.

## Acceptance additions

- Daytime and overnight bounds; UTC versus named-zone hours; weekday boundaries and DST; invalid equal/empty times.
- Up/Down and unknown/unresolved distinctions; provider versus proxy filtering; combined filters; dates still use UTC bounds.
- Protected labels are never accessed to resolve a filter or matching count; duplicate markets do not bypass the guard.
- All-matches and explicit-picks produce the declared population; unsupported measurements are distinct from zero matches.
- Changing filters updates the preview identity; saving/reopening preserves membership, filters, time zone, policies and sources; using a saved recipe creates a new version.
- Diagnostic outcome-conditioned sets can reach an otherwise supported experiment while retaining their population limitations in results/comparisons. Missing-input diagnostics do not gain eligibility automatically.

Prototype checks live in `validate-data-filters.mjs`; they exercise browser-fixture semantics only. Production Python contracts and evidence remain separate.

## Contract review clarifications

- Match actual `strategy_replay.labels.proxy_outcome`: the current Up/Down proxy is derived from a single positive entry in a recorded resolved payout vector, with explicit unqualified units/finality/semantics. It is not a reference-price comparison. The sketch now says “Payout-derived proxy”. Its fictional provider-confirmed examples are not claims about the current warehouse. That supported filter can return zero real matches.
- Reuse means copy filters into a draft and resolve a preview. A new window set is written only after an explicit Save. Resolving or opening filters does not itself save a set.
- Keep the written calendar-day semantics explicit: weekdays refer to the actual zoned window start. With Monday selected and 22:00–02:00, Monday 00:00–02:00 and Monday 22:00–24:00 match; Tuesday 00:00–02:00 does not. A session anchored on Monday night would be a different policy and must change both clients and backend tests together. Avoid the ambiguous phrase “early part of the next day”.
- The React browser still needs a thin local HTTP/IPC bridge before binding Python `workflows.call`. Shared in-process Python calls are the business-operation boundary, not a browser transport. This does not prevent DL-1 implementation.
