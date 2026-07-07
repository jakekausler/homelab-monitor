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
