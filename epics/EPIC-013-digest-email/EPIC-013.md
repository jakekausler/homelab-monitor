# EPIC-013: Digest builder + email

## Status: Not Started

## Overview

Build the configurable digest pipeline. Per spec §8.5 and §2 Q28, digests are fully configurable per recipient: cadence (daily / weekly / both / custom), selectable sections, level-of-detail toggles per section. Each section is a `digest_section` plugin so new sections can be added without core changes. Format: HTML with embedded sparklines + plaintext fallback + dashboard deep-links. Delivery: SMTP.

After this epic, the user receives a daily morning summary and a weekly comprehensive summary by default; both are configurable in Settings.

## Source documents

- Spec §3.1 (digest builder), §8 (notifications, including digest section list), §6.1 (`digest_configs` table), §9.2 (Settings → Digests screen).
- Spec §2 Q28 (digest is fully configurable; ship with all listed sections; add more via plugins).

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-013-001 | `digest_section` plugin contract; built-in registration; default cadence engine (cron-driven jobs) |
| STAGE-013-002 | Built-in section: active alerts (open issues with severity, age) |
| STAGE-013-003 | Built-in section: resolved alerts since last digest (with durations) |
| STAGE-013-004 | Built-in section: auto-fix activity (Claude runs, dry-runs, exit codes — links to transcripts) |
| STAGE-013-005 | Built-in section: cron heartbeat report (on-time / late / missing per registered cron) |
| STAGE-013-006 | Built-in section: backup status (Hyper Backup, /storage/scripts/cron/backup.sh, Backblaze leg) |
| STAGE-013-007 | Built-in section: cert/domain expiry roundup (anything expiring in next N days, configurable) |
| STAGE-013-008 | Built-in section: update availability (container images, OS packages, DSM, Unifi firmware) |
| STAGE-013-009 | Built-in section: resource trends (top CPU/RAM/disk consumers; anomalies in trend) |
| STAGE-013-010 | Built-in section: tool effectiveness scorecard (links the EPIC-010 data) |
| STAGE-013-011 | Built-in section: "what changed" delta vs last digest (new containers, new devices, removed services, deleted crons) |
| STAGE-013-012 | Built-in section: top noisy alert sources (tuning candidates) |
| STAGE-013-013 | HTML rendering pipeline with embedded sparklines (lightweight inline-SVG; no external image dependencies) |
| STAGE-013-014 | Plaintext fallback rendering |
| STAGE-013-015 | SMTP channel: secret-store keys for SMTP host/port/user/password/from; STARTTLS/TLS support; bounce handling at log level |
| STAGE-013-016 | Settings → Digests UI: cadence, sections, level-of-detail toggles, "Send test now" button |
| STAGE-013-017 | "Send test now" backend: builds the digest with current data and sends to the configured recipients (with a TEST: prefix in subject and a banner) |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Section ordering** in the rendered output matches the order in the `digest_configs.sections` list.
- **Empty sections produce no output** — a section with nothing to report is dropped, not rendered as "Nothing to report" (configurable per section).
- **Sparklines are inline SVG** — no external image dependencies. Email clients that block remote images still display correctly.
- **Plaintext fallback always renders** — the multipart message has both `text/plain` and `text/html`.

## Dependencies

- EPIC-001 (alerts and audit log).
- EPIC-002 (cron heartbeats — feeds the heartbeat-report section).
- EPIC-003 (Docker — feeds the container update-availability section).
- EPIC-007/EPIC-008 (Unifi/Synology firmware/DSM updates).
- EPIC-009 (auto-fix — feeds the auto-fix-activity section).
- EPIC-010 (tool scorecard — feeds the scorecard section).
- EPIC-011 ("what changed" delta computation needs discovery state).

## Notes

- Daily digest default time: 7:00 AM local. Weekly digest: Sunday 6:00 PM local.
- The user has a single recipient (themselves); the framework supports multiple, but the public release ships with a single default recipient configured during first install.
- Email rendering is one of the harder UX problems in the project. Plan for several rounds of refinement after the initial build — actual email clients (Gmail, Apple Mail, Thunderbird, mobile) all behave differently.
