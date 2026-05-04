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

## Dependencies

- EPIC-001 (kernel + secrets).
- EPIC-008 (Synology integration) — SSH probes are most useful when paired with the existing Synology integration.

## Notes

- Key generation: `hm ssh-probe keygen` produces a fresh ed25519 key per target host; private key stored in secrets store, public key printed for user to add.
- `paramiko` vs `asyncssh`: `asyncssh` is the better fit for our async architecture; lock at Design.
- Future targets beyond Synology and a sidecar host: not specified during the brainstorm. The framework supports any Linux host.
