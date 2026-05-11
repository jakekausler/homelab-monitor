# Regression Checklist - EPIC-002: Heartbeat + cron registry

(Items added per stage during Refinement.)

## STAGE-002-001 (heartbeat receiver)

- [ ] `make verify` GREEN: 1184+ backend tests pass, kernel coverage 100%, UI tests pass
- [ ] `POST /api/hb/<unknown-id>/ok` returns 404 (NotFoundProblem RFC 7807 body) — guards against accidental auto-create regression
- [ ] `POST /api/hb/<id>/ok?foo=bar` returns 422 — guards against `extra='forbid'` enforcement regressing if `query_model()` is removed or replaced with `Depends()` again
- [ ] Vertical slice: insert cron via SQL → POST /start, /ok, /fail → DB shows correct state transitions, audit_log has 3 rows, crons.last_seen_state mirror matches heartbeats_state.current_state
- [ ] Alembic 0006 down + up cycle preserves schema parity (idempotent migration)
- [ ] Heartbeat router rate-limit 429 path stays unit-tested (parametrized over /start, /ok, /fail in `test_rate_limit_returns_429_with_retry_after_per_verb`)
