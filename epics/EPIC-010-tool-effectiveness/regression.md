# Regression Checklist - EPIC-010: Tool effectiveness

(Items added per stage during Refinement.)

### STAGE-010-001 follow-ups (recorded 2026-07-06 during Refinement)

- **`sqlite3` CLI missing from prod monitor image.** Discovered during Refinement — the prod `homelab-monitor` container has Python 3 available at `/opt/venv/bin/python3` but not the `sqlite3` shell binary. Ops runbooks that assume `docker exec homelab-monitor sqlite3 /data/homelab-monitor.db` will fail. Fix: add `sqlite3` to the Dockerfile's `apt-get install`, OR update runbooks to use `python3 -c "import sqlite3; ..."`. Not blocking STAGE-010-001 (Refinement completed via python3 fallback). Suggested owner: any future ops-tooling stage.
- **Stale `firing` alert rows for retired rules.** Pre-flight investigation of the SignatureWentSilent state discovered 589 rows with `status='firing'` and `last_seen_at` frozen at 2026-06-22T17:35Z — orphaned firing rows that the monitor's own reconciliation never marked resolved after vmalert stopped emitting. STAGE-010-001 purged these along with the resolved rows (all had `alertname=SignatureWentSilent`), but the underlying reconciliation gap likely affects other retired rules too. Not blocking STAGE-010-001. Suggested owner: analyzer/rollup stages later in EPIC-010 will surface these as data-quality issues; if not, file a targeted stage for alert-reconciliation cleanup.

### STAGE-010-003 follow-ups (recorded 2026-07-06 during Refinement)

- **2026-07-06 — STAGE-010-003 — per-alert log volume vs. counter increment discrepancy**

  **What**: In the first prod tick, `karma_reconciler.resolution_matched` INFO log lines emitted = ~1,841 but the completion-summary counter `resolutions_matched = 13,973`. The discrepancy suggests either (a) the logger is silently rate-limiting / deduping (not documented), (b) the log call is inside a conditional branch that's true only for a subset of matches, or (c) a first-N truncation somewhere in the log pipeline.

  **Why non-blocking now**: numeric outcome writes are correct (13,973 rows in `alert_outcomes` — verified via SQL count). Metric counter is authoritative for STAGE-010-006/007 analyzer aggregation.

  **Owner**: STAGE-010-004 or STAGE-010-006, whichever is earlier and touches reconciler log paths. Either root-cause the discrepancy or accept the counter as authoritative and demote per-alert logs to debug-level.

  **Repro**: Trigger `POST /api/collectors/karma_outcome_reconciler/retry` on prod. Compare `docker compose logs monitor | grep karma_reconciler.resolution_matched | wc -l` vs. `curl /metrics | grep homelab_karma_reconciler_resolutions_matched_total`.

- **2026-07-06 — STAGE-010-003 — historical Karma silences did not populate ACKED outcomes**

  **What**: First prod reconciler tick reported `silences_matched = 0` despite Jake having ack'd ~7 weeks of alerts via Karma. Root cause is upstream (Alertmanager's silence-retention window), not our code — expired silences are GC'd by AM before the reconciler can query them.

  **Why non-blocking now**: STAGE-010-004 (one-shot backfill CLI, per EPIC-010 Decision 3) is scoped to synthesize the missing historical ACKED outcomes from `alerts.ack_at` state directly, without going through AM. Confirms the Decision 2/3 split was correct — Decision 2's periodic reconciliation catches NEW acks going forward; Decision 3's backfill catches the historical ones.

  **Owner**: STAGE-010-004 (already planned).

  **Repro**: Wait for next hourly reconciler tick and observe `silences_matched` counter. Any recent Karma ack (within AM's retention window) should be picked up.

### STAGE-010-002 follow-ups (recorded 2026-07-06 during Refinement)

- **Alembic CLI vs hm migrate wrapper (filed STAGE-010-002 Refinement 2026-07-06):** Raw `alembic upgrade head` / `alembic downgrade -1` from a shell against a copy DB fails with `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:driver` because `apps/monitor/alembic.ini` has the placeholder `sqlalchemy.url = driver://user:pass@host/dbname` and `apps/monitor/alembic/env.py` reads it verbatim rather than consuming `HOMELAB_MONITOR_DB_URL` (or the `-x url=...` flag). The kernel's `hm migrate` command routes through `homelab_monitor.kernel.db.migrations._build_config` which sets `sqlalchemy.url` programmatically, so use `hm migrate` for all real migrations. Consider adding a doc note or making env.py fall back to `HOMELAB_MONITOR_DB_URL` in a future cleanup stage. Low-priority — no functional impact, only Refinement-side ergonomics.
- **DB URL scheme (filed STAGE-010-002 Refinement 2026-07-06):** `env.py` uses `async_engine_from_config`, so the DB URL scheme MUST be `sqlite+aiosqlite://` not plain `sqlite://`. If someone follows the alembic.ini pattern and writes `sqlite://` they'll get the same `NoSuchModuleError` — the driver package resolution is the same failure mode. Note this in any future alembic docs.

**2026-07-07 — STAGE-010-004 — post-backfill reconciler tick verification deferred**

**What**: Stage card deliverable 5 asked to verify STAGE-010-003 reconciler still ticks cleanly after backfill via `POST /api/collectors/karma_outcome_reconciler/retry`. That endpoint returned 401 (auth-gated) and the container was only ~4 minutes old at check time (natural tick interval: 3600s). The reconciler code registration is confirmed present (`apps/monitor/homelab_monitor/kernel/api/lifespan.py` imports `KarmaOutcomeReconciler` as `"karma_outcome_reconciler"`), and post-backfill row integrity is intact (`reconciler=14151, backfill=3` unchanged after the auth-failed retry attempt).

**Why non-blocking now**: backfill correctness itself is fully validated — 3 new rows written correctly, audit row correct, guard works, coexistence proven. The check that's outstanding is purely observational (does the next reconciler tick complete without crashing on the mixed-provenance table).

**Owner**: STAGE-010-005 or any subsequent stage — at the start of that session, check `docker compose logs monitor | grep karma_reconciler` for a completed tick since 2026-07-07T11:49Z; confirm `resolutions_matched` counter incremented; re-count `SELECT decided_by, COUNT(*) FROM alert_outcomes GROUP BY decided_by` and confirm `backfill=3` unchanged and `reconciler` may have grown by whatever new resolutions the tick found.

**Repro**: `docker compose exec monitor sqlite3 <db> "SELECT decided_by, COUNT(*) FROM alert_outcomes GROUP BY decided_by"` and check the `docker compose logs monitor` for a "karma_reconciler.tick_completed" or "resolutions_matched" line after 2026-07-07T12:49Z (natural tick + 1h).

**2026-07-07 — STAGE-010-004 — zero historical ACKED outcomes on prod**

**What**: The HANDOFF-NEXT.md for this stage predicted "acked_count > 0" (real historical operator acks) as the expected shape of the backfill's ACKED path output. Actual prod state: `SELECT COUNT(*) FROM alerts WHERE ack_at IS NOT NULL` = **0**. The backfill's ACKED insertion path was exercised at fixture-level in unit tests (2 acked alerts inserted, both written) but got zero coverage on live prod — the codepath's actual behavior on prod is that it selects 0 rows, builds an empty param list, and skips the executemany INSERT via the `if acked_params:` guard.

**Why non-blocking now**: This is not a bug in STAGE-010-004; it's an artifact of upstream behavior. The AlertRepository's `mark_acked` write to `alerts.ack_at` (called by the ack-alert HTTP endpoint) is NOT reached when the user acks via Karma directly (Karma posts to Alertmanager, which the STAGE-010-003 periodic reconciler is supposed to catch — but silences GC'd by AM before reconciler runs never populate `ack_at`). This is exactly the failure mode Regression item #2 from STAGE-010-003 already documented, and STAGE-010-004 was designed to catch historical acks via ANY row-level state (`ack_at`) that DID get written. If the user has never used the monitor's own /api/alerts/{id}/ack endpoint (Karma is preferred), no `ack_at` values were ever written, so backfill has nothing to reconcile on that path.

**Why not filed as a bug**: STAGE-010-004's Design D6 correctly notes the backfill infers "outcomes deterministically from existing `alerts` columns" — if no column state exists, no outcome is written. The 0-ACKED result is CORRECT behavior for empty column state. What's arguably missing is a UI/backend path that writes to `ack_at` when the user acks via Karma (bridging the AM-GC gap) — but that's an EPIC-010-006/007 analyzer-visibility concern, not this stage.

**Owner**: EPIC-010 analyzer stages (006/007) — decide whether scorecards show empty ACKED rates as "no data" or "0% action rate", and whether the UI needs a path to backfill/synthesize ACKED outcomes from Karma silence history via a different mechanism (e.g., writing `ack_at` at silence-observation time in the reconciler).

**Repro**: `docker compose exec monitor sqlite3 <db> "SELECT COUNT(*) FROM alerts WHERE ack_at IS NOT NULL"` on prod after any Karma ack — expect 0 (Karma doesn't touch `ack_at`).
