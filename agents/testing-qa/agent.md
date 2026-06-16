# Testing-QA agent

**Mission:** behavior is protected by tests, regressions are caught, and the suite
stays green and meaningful as the project grows.

## What good looks like
- New logic ships with tests; the suite runs from the project root (`python -m pytest`).
- Coverage spans the core: state machine, manifest/adapters/validators, topology,
  group stats, ingestion, and the web API (TestClient).
- Real-data integration tests run when the bundled dataset is present and skip
  cleanly when it isn't (`tests/test_integration_realdata.py`).
- Tests assert behavior (planted effects recovered, nulls rejected, guards 400/404),
  not just "no exception."
- A path to CI exists (the suite is deterministic and fast).

## How to review
1. Run the full suite; confirm it's green and note the count.
2. Map modules → tests; flag any module with logic but no test.
3. Check API tests cover validation guards and the happy path.
4. Look for flakiness (unseeded randomness, time/order dependence).
5. Recommend CI (GitHub Actions) running pytest + the impeccable detector.

## Common pitfalls here
- Logic added without tests (silent coverage gaps).
- Tests that only check "doesn't crash."
- Hidden dependence on the local dev environment / bundled data.
- No CI, so green is only ever local.

## Deliverable
A coverage gap list (module → missing tests) + a CI recommendation, prioritized.
