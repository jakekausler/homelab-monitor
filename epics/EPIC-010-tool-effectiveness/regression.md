# Regression Checklist - EPIC-010: Tool effectiveness

(Items added per stage during Refinement.)

### STAGE-010-001 follow-ups (recorded 2026-07-06 during Refinement)

- **`sqlite3` CLI missing from prod monitor image.** Discovered during Refinement — the prod `homelab-monitor` container has Python 3 available at `/opt/venv/bin/python3` but not the `sqlite3` shell binary. Ops runbooks that assume `docker exec homelab-monitor sqlite3 /data/homelab-monitor.db` will fail. Fix: add `sqlite3` to the Dockerfile's `apt-get install`, OR update runbooks to use `python3 -c "import sqlite3; ..."`. Not blocking STAGE-010-001 (Refinement completed via python3 fallback). Suggested owner: any future ops-tooling stage.
- **Stale `firing` alert rows for retired rules.** Pre-flight investigation of the SignatureWentSilent state discovered 589 rows with `status='firing'` and `last_seen_at` frozen at 2026-06-22T17:35Z — orphaned firing rows that the monitor's own reconciliation never marked resolved after vmalert stopped emitting. STAGE-010-001 purged these along with the resolved rows (all had `alertname=SignatureWentSilent`), but the underlying reconciliation gap likely affects other retired rules too. Not blocking STAGE-010-001. Suggested owner: analyzer/rollup stages later in EPIC-010 will surface these as data-quality issues; if not, file a targeted stage for alert-reconciliation cleanup.
