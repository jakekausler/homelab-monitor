# EPIC-008: Synology integration

## Status: Not Started

## Build order + SSH-scoping note (added 2026-06-17 Unifi brainstorm)

**Build sequence: EPIC-017 → EPIC-007 → EPIC-006 → EPIC-008** (whole epics, sequential; numbers unchanged).
EPIC-008 is built LAST of the four. Consequences:

- **EPIC-017 (SSH probe framework) is built FIRST**, so this epic's SSH-based DSM probes (`smartctl`,
  `btrfs scrub`, `df`, etc.) are built ON the scoped per-target forced-command framework from day one — NOT
  as unscoped access hardened later.
- **Synology-specific SSH probes live HERE, not in EPIC-017.** EPIC-017 is the generic framework + an
  exemplar `uptime` probe (against the UDM AND the Synology). The `synology_smartctl` / `synology_btrfs_scrub`
  / `synology_df` probes (previously sketched inside EPIC-017) are THIS epic's, built on the 017 framework.
- **Any SSH work that starts unscoped MUST be scoped via EPIC-017.** DSM SSH typically starts as an admin
  user; this epic's probes MUST run as the dedicated low-privilege key-restricted forced-command
  `homelab-monitor-probe` user the 017 framework provisions — never `root`, never `admin`. This is the same
  "replace unscoped SSH" mandate recorded in the EPIC-017 banner.
- EPIC-007 (Unifi) lands the unified client identity before this epic; the Synology appears as a wired
  client in Unifi's registry (see Notes).

## Overview

Land Synology DS3622xs+ as a plugin bundle. Three integration paths per spec Q12 (all in scope):

- **A:** SNMP via `snmp_exporter` with the Synology MIB — covers volumes, disks, RAID/SHR status, temps, fans, UPS, NIC stats. Drop-in, low intrusion.
- **B:** DSM API for things SNMP can't see well: Hyper Backup job status, Snapshot Replication, Surveillance Station camera status, package update availability, user logins.
- **C:** DSM syslog forwarding for SSH/DSM login events, SMART events, package install/update events.

All listed Synology signals in scope: volume/pool degraded, SMART warnings + temperature, disk usage threshold per volume, UPS battery / runtime, Hyper Backup job failed/missing, Snapshot Replication lag, Surveillance Station camera offline / recording stopped, failed login attempts (DSM/SSH/SMB), package/DSM update available.

The Synology is the most critical infra in this homelab — it holds backups, media, surveillance recordings, and serves NFS/SMB to several Docker containers. Failure here is high-impact.

## Source documents

- Spec §2 Q12 (Synology decisions), §3.4 (discovered targets).
- Project memory `reference_homelab_inventory.md`: DS3622xs+ with Surveillance Station + 3 Reolink cameras. Backups land on Synology (`/storage/backup/`) and replicate to Backblaze.

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-008-001 | `snmp_exporter` sidecar with Synology MIB; SNMP v3 user creation on Synology side documented; scrape config addition; first volume + disk metrics |
| STAGE-008-002 | DSM API client + auth (modern OTP-aware login or app token); secrets store for credentials; smoke connectivity |
| STAGE-008-003 | Hyper Backup status collector via DSM API: per-job last-run, success/failure, age; default rule `BackupMissing` (no successful run in N hours), `BackupFailed` |
| STAGE-008-004 | Snapshot Replication lag collector |
| STAGE-008-005 | Surveillance Station camera-status collector: per-camera online/recording status; default rule `CameraOffline` |
| STAGE-008-006 | Package + DSM update-availability collector: emits `homelab_synology_update_available{kind}`; rule at info severity |
| STAGE-008-007 | DSM syslog forwarding setup (instructions documented); vector source addition; failed-login pattern produces `SshFailedLoginBurst`-like rules scoped to Synology source. **MUST include the following EPIC-004 follow-ups (brainstormed 2026-05-28):** (a) add Synology DSM-specific API token + session-cookie + photo-token patterns to the redaction pipeline (`homelab-monitor.yaml` under `logs.redact:`) so DSM syslog content is redacted at ingest before reaching VL; default v1 (EPIC-004 STAGE-004-006) ships only generic patterns. (b) Set `service` label on Synology syslog source (e.g., `service="synology-auth"`, `service="synology-smart"`, `service="synology-package"` per facility) so the per-`service` Drain models from EPIC-004 STAGE-004-025+ automatically partition. (c) Embed `<LogViewer>` (from EPIC-004 STAGE-004-003) on the Synology UI panel (STAGE-008-009) with a pre-filled `service:"synology-*"` filter — uses the documented embedding contract. |
| STAGE-008-008 | UPS metrics (if/when a UPS is connected — many users have one on the Synology): battery %, runtime estimate, on-battery state |
| STAGE-008-009 | Synology integration UI panel: volumes, drive temperatures, SMART status grid, backup-job timeline, camera status grid. **MUST consume `<LogViewer>` from EPIC-004 STAGE-004-003 with a caller-provided `useLogs` hook scoped to Synology syslog** (e.g., `service:"synology-*"`). Use the embedding contract documented in `apps/ui/src/components/logs/README.md`. Per the brainstorming session 2026-05-28, EPIC-004 explicitly designs the embedding contract for this use case. |
| STAGE-008-010 | Default Grafana dashboard `synology.json` + default vmalert rules |
| STAGE-008-011 | NFS/SMB mount-health probe: validates `/rackstation/*` mounts on this host are alive (cross-cuts Docker containers that depend on these mounts) |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Concurrency group `synology`** for all collectors hitting the DSM API.
- **Read-only DSM credentials** wherever possible; document in setup how to create a dedicated read-only account.
- **Backup-status collector is the single most important rule** in this epic. False negatives are unacceptable. Test thoroughly — including the case where backup ran but produced nothing meaningful (size = 0).
- **Mount-health probe before container probes** — if `/rackstation` is gone, containers that depend on it are *expected* to be sad; we suppress the per-container alerts and surface the mount-health alert as the root cause.

## Dependencies

- EPIC-001 (kernel, etc.).
- EPIC-003 (Docker) — the mount-health probe is most useful when wired into Docker probe suppression.
- EPIC-017 (SSH probes) — some Synology checks (e.g., `btrfs scrub status`, fine-grained `smartctl`) may be easier via SSH than DSM API; SSH probe path lands later, so this epic ships without them and may add follow-on stages later.

## Notes

- The Synology is connected to the Unifi network gear, so Unifi (EPIC-007) reports it as a wired client. The two integrations cross-reference: when Unifi sees the Synology drop, Synology-side collectors will too. Tool-effectiveness analyzer (EPIC-010) will eventually evaluate which catches outages first.
- Surveillance Station cameras are the user's three Reolink cameras. They show up to monitor as both "Synology cameras" (via Surveillance Station) and as raw devices (via Unifi clients / direct ICMP). Don't over-alert.
