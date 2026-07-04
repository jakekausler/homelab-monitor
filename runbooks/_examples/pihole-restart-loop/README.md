# pihole-restart-loop — exemplar runbook

This folder ships as an **example only** in `runbooks/_examples/` and is NOT registered or enabled on a fresh install (the runbook loader silently skips `_`-prefixed folders per LOCKED Decision 4). A default homelab-monitor install has zero active auto-fix.

## Enabling this runbook on your host

1. Copy this folder to your host-overrides repo at `runbooks/pihole-restart-loop/` (no underscore prefix).
2. `POST /api/runbooks/refresh` to register.
3. `PATCH /api/runbooks/{id}` with `{"enabled": true}` to enable.
4. `POST /api/runbooks/{id}/trigger` `{"mode": "dry_run"}` to test.
5. Approve the resulting dry-run via `POST /api/autofix/approvals/{id}/approve`.

## Non-negotiables enforced

This exemplar is a canonical demonstration of the seven auto-fix non-negotiables:

- **#1 Allow-list:** matches only `alertname: PiholeCrashLoop`.
- **#2 Scope:** `scoped_capabilities.docker` limits action to `restart pihole-unbound`.
- **#3 Identity:** invoked as the `homelab-fixer` low-privilege user.
- **#4 Audit:** every step (dry-run, approval, exec, exit) is written to `audit_log`.
- **#5 Dry-run + approval for risky:** `risk_tag: risky` forces plan-first then user approval.
- **#6 Rate-limit + cooldown:** `rate_limit_per_hour: 2`, `cooldown_seconds: 900`.
- **#7 Kill switch:** honors the global `autofix_enabled` app-setting.

See `epics/EPIC-009-auto-fix/EPIC-009.md` for the full auto-fix subsystem design.
