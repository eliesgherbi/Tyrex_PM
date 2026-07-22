# N7 live tools

## N7A (this milestone)

- `run_n7_fixture_acceptance.py` — FakeTransport one-shot entry→exit→FLAT
- `run_n7_readonly_preflight.py` — authenticated read-only preflight + auth request

Mutations remain OFF unless an operator consumes a single-use authorization for N7B.

## N7B

Not enabled by these tools alone. Requires the exact operator approval phrase
presented after N7A commit.
