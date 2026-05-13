# EPIC-017: SSH probe framework (per-target users, forced commands)

## Status: Not Started

## Overview

Build the SSH probe framework per spec §2 Q29. Use per-target dedicated low-priv users with key-restricted forced commands (`command="..."` in `authorized_keys`). Each probe declares its exact remote command. Setup instructions are documented per remote target.

This unlocks deep checks against remote machines (notably the Synology) that DSM API + SNMP can't surface — `smartctl --xall`, `btrfs scrub status`, `df -h /volumeN`, etc.

## Source documents

- Spec §2 Q29 (decided: per-target dedicated low-priv users, command-restricted), §3.1 (`SshClientFactory` in `CollectorContext`), §3.4 (Synology in scope; "anything else" deferred).

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-017-001 | `SshClientFactory` implementation: opens connections lazily by `target_id`; uses `paramiko` or `asyncssh`; closes on context-manager exit; pools connections per host with idle timeout |
| STAGE-017-002 | Probe contract: `SshProbe` is a `Collector` subtype; declares remote_user, remote_host, remote_command, parser; the parser maps stdout → metrics |
| STAGE-017-003 | Setup instruction generator: `hm ssh-probe install <probe-name>` prints instructions for the target host (create user, install key with `command="..."` restriction in `authorized_keys`); never auto-runs against a remote |
| STAGE-017-004 | Synology probes (the first real users):
  - `synology_smartctl` — `smartctl --json --xall /dev/sda` etc., parsed for predictive failure flags; emits `homelab_synology_smart_*`
  - `synology_btrfs_scrub` — `btrfs scrub status -R /volume1`; emits scrub progress + last completion
  - `synology_df` — `df --output=source,fstype,size,used,avail,pcent,target` for Btrfs subvolumes that DSM API doesn't surface |
| STAGE-017-005 | Generic probe library: a few example probes (e.g., `ping`, `df`, `uptime`) for any remote Linux host; serves as templates for users |
| STAGE-017-006 | UI: per-target "SSH probes" tab; lists configured probes with last-result + setup-instructions link; "Test connection" button |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Setup instructions never include the private key.** They include the public key the user adds to `authorized_keys`. The private key stays on our side, in the secrets store.
- **Forced-commands enforced.** A test probe verifies that connecting and trying to run an arbitrary command fails (because `command="..."` restricts to one).
- **Per-target user.** The Synology user is `homelab-monitor-probe`; never `root`, never `admin`. Document.
- **STAGE-002-006 cross-epic criterion (added 2026-05-12):** When SSH-pull cron discovery or SSH-push wrapper install ships in this epic, two STAGE-002-006 UI elements need updating:
  1. The `source_path IS NULL` remote-cron banner in `apps/ui/src/routes/inventory/CronDetail.tsx` MUST be removed (or trigger condition updated) for remote crons whose source files become readable via SSH-pull discovery.
  2. The disabled "Install heartbeat wrapper" button in the Actions panel of CronDetail.tsx (introduced disabled in STAGE-002-006, enabled for local hosts in STAGE-002-009) needs to be enabled for remote hosts when SSH-push wrapper install ships. The tooltip currently reads: "Local install ships in STAGE-002-009. Remote install requires cross-host work in EPIC-015 / EPIC-017." Replace the disabled state with a functional button OR remove the EPIC-017 reference from the tooltip when this epic delivers the remote-install path.

## Dependencies

- EPIC-001 (kernel + secrets).
- EPIC-008 (Synology integration) — SSH probes are most useful when paired with the existing Synology integration.

## Notes

- Key generation: `hm ssh-probe keygen` produces a fresh ed25519 key per target host; private key stored in secrets store, public key printed for user to add.
- `paramiko` vs `asyncssh`: `asyncssh` is the better fit for our async architecture; lock at Design.
- Future targets beyond Synology and a sidecar host: not specified during the brainstorm. The framework supports any Linux host.

## Cross-epic absorbed scope (from EPIC-002 cron derived-state redesign, 2026-05-11)

Per `docs/superpowers/specs/2026-05-11-cron-derived-state-redesign.md`, this epic absorbs the **SSH-based cross-host work** for the cron monitoring subsystem:

1. **SSH-pull cron discovery** — for hosts where the monitor has SSH credentials configured, a probe runs the same crontab-scanning logic that EPIC-002's local `cron-discoverer` plugin runs (read `/etc/crontab`, `/etc/cron.d/*`, per-user crontabs), parses each line, computes the fingerprint, and writes fingerprint-keyed rows into the registry with audit verb `crons.discover`. Equivalent to the local discoverer but the file reads happen over SSH instead of from a bind-mount.

2. **"Install heartbeat" SSH-push variant** — STAGE-002-009 ships a local-host-only "Install heartbeat" UI button. This epic extends the button to work for any host where the user has configured SSH credentials. The push variant:
   - Computes the wrapper script + fingerprint locally.
   - SSHes into the target host as the configured probe user.
   - `scp`s the wrapper script to `/usr/local/bin/cron-with-heartbeat.sh` (chmod 0755).
   - Writes the token file (chmod 0600).
   - Rewrites the target's crontab via `crontab -u <user> -` (or by editing `/etc/cron.d/...` via sudo if a system-level crontab).
   - POSTs `/register` against itself (the monitor) with `wrapper: true` to finalize.
   - Same dry-run-preview + explicit-confirm flow as STAGE-002-009.

3. **EPIC-002 UI integration** — STAGE-002-006's CronDetail page Panel 4 already has a disabled "Install heartbeat" button with tooltip pointing at this epic. When this epic's SSH-push stage ships, the button becomes ENABLED for any cron with `host` matching an SSH-configured target.

Suggested decomposition: add two new stages (e.g., STAGE-017-007: SSH-pull cron discovery; STAGE-017-008: SSH-push wrapper install) after the Synology probe stages.
