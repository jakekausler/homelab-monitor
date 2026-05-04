# EPIC-002: Heartbeat receiver + cron registry + cron auto-discovery

## Status: Not Started

## Overview

Deliver the cron-monitoring half of the system: a Healthchecks.io-style heartbeat receiver, a cron registry that tracks every cron the user has, automatic discovery of crons from `/etc/cron.*` + user crontabs, and helpers that bootstrap heartbeat pings into existing cron jobs without breaking them.

Cron monitoring is hybrid by design (per spec §2 / Q5):

- **B (default)**: log-scrape — observe via syslog/journald that the cron ran and what its exit code was. Works on any cron without modification.
- **A (gold standard)**: active heartbeat — the cron is wrapped/modified to call `curl http://homelab-monitor/hb/<id>/start` and `…/ok|/fail`. Most reliable, requires touching the cron.
- **C (bootstrap helper)**: discovery + wrapper — the monitor reads the user's crontabs, auto-generates a wrapper script per job, and offers it as a one-click install via the dashboard.

The public release defaults to A-mode-via-opt-in and B-mode-by-default (per spec Q30). The host-overrides repo for this user's deployment can be more aggressive about applying B/C selectively.

## Source documents (read before starting any stage)

- Spec sections: §3.1 (heartbeat receiver), §6.1 (`crons`, `heartbeats_state` tables), §11 (`/storage/scripts/cron/backup.sh` integration).
- Project memory `reference_homelab_inventory.md` — the user's existing crons:
  - User crontab: `@reboot /storage/scripts/startup/startup.sh`, `17 * * * * /storage/scripts/rtlamr-watchdog.sh`
  - System: `/etc/cron.d/certbot` (12:00 daily renew), `/storage/scripts/cron/backup.sh` (4:10 daily)

## Stages (to decompose during epic Design phase)

A reasonable starting list:

| Likely stage | Theme |
|---|---|
| STAGE-002-001 | Heartbeat receiver endpoints (`/hb/<id>/start|ok|fail`), `crons` + `heartbeats_state` schema migrations, API token scope `heartbeat:write`, audit |
| STAGE-002-002 | Cron registry CRUD API + UI screen ("Crons" tab in Inventory): list, edit cadence/grace, soft-delete |
| STAGE-002-003 | Cron auto-discovery (system + user crontabs, `/etc/cron.{d,daily,hourly,weekly,monthly}/`); emits suggestions into the suggestion queue |
| STAGE-002-004 | Log-scrape default (B-mode): vector tails cron output to VL; collector parses cron status from journald and updates `heartbeats_state` accordingly |
| STAGE-002-005 | Heartbeat opt-in helpers (C-mode): generate a wrapper `cron-with-heartbeat.sh <id> <real-command>` and dashboard "Install wrapper" action that produces a copy-paste snippet; never touches user crontabs without explicit confirm |
| STAGE-002-006 | vmalert rules for stale heartbeats and unexpected-success-streak-broken; tests |

Stage decomposition is finalized at the start of this epic; the list above is a starting point.

## Cross-stage acceptance criteria

Same as EPIC-001 (see EPIC-001.md §Cross-stage acceptance criteria). Plus:

- **No existing cron is modified** without explicit user confirmation through the dashboard (per spec Q30 default).
- **Heartbeat endpoints accept API tokens only** — no anonymous heartbeat posts (rate-limited tokens with scope `heartbeat:write`).
- **B-mode parsing is idempotent** — re-processing the same syslog line never double-counts.

## Dependencies

- EPIC-001 complete (kernel, API, alerts, scheduler, logs pipeline).
- STAGE-001-004 created minimal-schema stubs for `crons` and `heartbeats_state` (per the "minimal-schema stub" rule in that stage). This epic's first migration adds the behavioral columns (cadence, grace, integration_mode, last_seen_state, etc.) when the heartbeat receiver lands.

## Notes

- The user's `/storage/scripts/cron/backup.sh` will receive an *opt-in* heartbeat ping in the host-overrides repo, not in the public release. Same for `rtlamr-watchdog.sh`.
- "Cron" here includes `@reboot` jobs (track via "should have been seen since last boot") and systemd timers (read from `systemctl list-timers --all --output=json`).
- An "expected cadence" model handles weird schedules like `17 * * * *` correctly (next-due = previous_run + interval, with grace).
