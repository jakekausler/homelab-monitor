# EPIC-017: SSH probe framework (per-target users, forced commands)

## Status: Not Started

## Build order + framework-first mandate (LOCKED — 2026-06-17 brainstorm)

**EPIC-017 is built FIRST in the sequence EPIC-017 → EPIC-007 → EPIC-006 → EPIC-008** (whole epics,
sequential; numbers unchanged). Rationale: the per-target scoped-user + forced-command SSH framework must
exist BEFORE any consumer epic ships an SSH collector, so no epic accumulates "unscoped now, harden later"
SSH debt. EPIC-007's opt-in DHCP-lease collector (STAGE-007-012) and EPIC-008's DSM probes are built ON this
framework.

**Replace currently-unscoped SSH access (NON-NEGOTIABLE mandate).** Today the monitor host has UNSCOPED SSH
to both real targets (passwordless root to the UDM; a passwordless **admin** user to the Synology on port
53197). This epic's framework MUST provide a dedicated, key-restricted **forced-command** access path per
target, and the consumer collectors MUST use it — never the existing unscoped human-ops keys:
- **Unifi (EPIC-007):** the opt-in DHCP-lease read runs through this framework's forced-command path (the
  key can ONLY emit the lease data), NOT a general root shell. No committed doc reveals the current unscoped
  path; the framework replaces it.
- **Synology (EPIC-008):** DSM probes run as a NEW dedicated low-priv user (NOT the existing admin), via this
  framework's forced-command + scoped-sudoers path.

**Synology-specific probes are NOT this epic's.** EPIC-017 is the GENERIC framework + a trivial `uptime`
exemplar (against the UDM AND the Synology). The `synology_smartctl` / `synology_btrfs_scrub` / `synology_df`
probes + their specific sudoers entries are EPIC-008's deliverables (built on this framework).

## Overview

Build the read-only SSH probe framework (spec §2 Q29: per-target dedicated low-priv users with key-restricted
forced commands; §3.1: `SshClientFactory` in `CollectorContext`). The kernel already SCAFFOLDS this — the
context field `ssh: SshClientFactory` and the `SshClientFactory.open(target_id)` + (empty) `SshConnection`
Protocol exist in `apps/monitor/homelab_monitor/kernel/plugins/{context,io}.py` with explicit "real impl:
EPIC-017" markers. This epic fills those stubs with a real **asyncssh**-based implementation (asyncssh is NOT
yet a dependency — added here), the `SshProbe` collector base, the setup tooling (`hm ssh-probe`), framework
health metrics + alerts, and a trivial `uptime` exemplar proving BOTH account-modes end-to-end against the
two real targets.

This framework is **read-only and observe-only**. It does NOT write to targets' auth config (the user installs
keys manually from generated instructions) and ships NO write-capable SSH paths. The previously-absorbed cron
SSH-pull/push scope is DEFERRED (see "Deferred scope").

## Verified deployment reality (recon 2026-06-17 — read-only; re-verify live in each stage's Design)

- **Kernel plumbing is scaffolded, not built.** `CollectorContext.ssh: SshClientFactory` is a required field
  (`kernel/plugins/context.py`). `SshClientFactory.open(target_id) -> AbstractAsyncContextManager[SshConnection]`
  is declared in `kernel/plugins/io.py`; `SshConnection` is an EMPTY Protocol ("methods land in EPIC-017").
  `asyncssh`/`paramiko` are NOT dependencies. Secrets: read via `ctx.secrets.get(name)` (sync resolver),
  write via `AsyncSecretsRepository.set(name, value, who=)`. Collector base = `BaseCollector` (ClassVars +
  `async run(ctx) -> CollectorResult`); the `ok=True`-even-when-target-down convention is load-bearing (the
  HA `ha_up.py` collector is the exemplar). Snapshot secrets are pickled across the PROCESS-run IPC boundary
  (a PEM key string survives to subprocess collectors).
- **UDM Pro (`192.168.2.1`, appliance):** passwordless **root** SSH works today (single existing ed25519 key
  in `/root/.ssh/authorized_keys`). UniFi OS = stock OpenSSH (forced commands supported), but **root-only — no
  non-root user can be created**. `/root/.ssh/authorized_keys` lives on **overlayfs** (upperdir
  `/mnt/.rwfs/data`), NOT the `/persistent` partition → **a forced-command key likely will NOT survive a
  UniFi OS firmware update** (re-paste required). Target data (the DHCP lease file
  `/data/udapi-config/dnsmasq.lease`) is world-readable; plain `cat` as root works.
- **Synology DS3622xs+ (`192.168.2.4`, port `53197`, full DSM 7.3.2 OS):** passwordless SSH works today but
  lands as a **privileged admin** user (uid 1026, in `administrators` + `root` groups) — NOT a low-priv user.
  Stock OpenSSH (forced commands supported), real persistent ext4 home, bash. `df`/`uptime` readable
  unprivileged; **`smartctl -a` and `btrfs` queries require root** and there is **no passwordless sudo** for
  this user → a dedicated low-priv user + a **narrow NOPASSWD sudoers** wrapper is required for privileged
  data (EPIC-008's specifics).
- **Crons on both targets are NOT worth SSH cron-discovery:** the UDM's are 100% UniFi/Debian housekeeping;
  the Synology's watch-worthy jobs (monthly SMART, ActiveBackup retention — currently disabled) are surfaced
  by EPIC-008's DSM-API collectors, not cron. → cron SSH work DEFERRED (see "Deferred scope").

## Account model (the spine — two modes, driven by the appliance-vs-full-OS tension)

The two real targets sit at opposite ends of the constraint space, so the framework models a per-target
**`account_mode`**:

- **`appliance` mode (UDM):** the framework does NOT create a user. It accepts landing as the existing
  privileged user (root) and treats the **`command="..."` forced command in `authorized_keys` as the SOLE
  least-privilege boundary**, pinned to a **fixed inlined compound read-only command** (e.g. `cat` the lease
  file + `uptime`). **No on-target script** (it wouldn't survive the overlay wipe, and there's no low-priv
  user to own it). Setup instructions include a **firmware-update persistence warning**.
- **`dedicated-user` mode (Synology, full OS):** the framework's setup instructions create a **NEW dedicated
  low-priv user** (NOT the existing admin), with its own keypair; the forced command pins to an **installed
  read-only collector script** owned by that user. Privileged data (SMART/btrfs) is reached via a **narrow
  NOPASSWD sudoers** entry scoped to exactly those binaries, called from inside the script. The script + the
  specific sudoers commands are the **consumer epic's (EPIC-008)** content; the framework owns the
  install/sudoers-generation **mechanism** + the trivial exemplar script body.

In BOTH modes the forced command is the security boundary; the dedicated user + sudoers on Synology is
defense-in-depth. The framework = **mechanism + trivial exemplar**; consumer epics = **real script bodies +
specific sudoers commands**.

## Transport & key model (LOCKED)

- **Library: `asyncssh`** (added as a dependency; async-native; SSH probes run `run_kind=ASYNC`).
- **Open-per-run, no connection pool** (probes run at 60s–5m cadence; per-target `concurrency_group`
  serializes; pooling is an unneeded complexity for v1).
- **Per-target pinned host-key verification** — capture each target's host key at setup, verify against it on
  every connection. NEVER blanket `known_hosts=None` (that would be MITM-open on the LAN gateway — ironic for
  a security-probe framework). A host-key mismatch is a first-class **critical** signal (potential MITM).
- **Per-target ed25519 key** in the secrets store (`ssh_probe_key_<target>`), generated by the framework and
  distinct from the existing human-ops keys (those stay for human ops, untouched).
- **Manual key install (Option A)** — the framework GENERATES the exact `authorized_keys` line (with
  `command="..."` + `no-port-forwarding,no-pty,no-X11-forwarding,no-agent-forwarding` hardening) and the
  account-mode-aware setup steps, and VERIFIES the restriction holds. It NEVER writes to a target's auth
  config itself (trust-minimization is the whole point of this epic).
- **Non-standard ports supported** (Synology = `53197`).

## Probe contract & observability (LOCKED)

- **`SshProbe` base** (subclasses `BaseCollector`): open via `ctx.ssh.open(target_id)` → run the pinned
  forced command → capture stdout/exit → parse → emit → close. Follows the `ok=True`-even-when-target-down
  convention: target sad → `homelab_ssh_up{target}=0`, still `ok=True`; `ok=False` ONLY when the probe itself
  errors (connection refused / host-key mismatch / timeout). The empty `SshConnection` Protocol gets a narrow
  connect-and-run-pinned-command → typed-output surface.
- **Framework health metrics** (every probe, spec §5.7): `homelab_ssh_up{target}`,
  `homelab_ssh_probe_duration_seconds{target,probe}`, `homelab_ssh_last_success_age_seconds{target,probe}`,
  kernel `homelab_collector_run_*`, and **`homelab_ssh_host_key_mismatch{target}`** (first-class, distinct
  from "target down").
- **Framework-health alerts** (`deploy/vmalert/metrics/ssh.yaml`): `SshTargetUnreachable` (warning),
  **`SshHostKeyMismatch` (critical — MITM on a trusted target)**, `SshProbeStale` (warning). Target-specific
  alerts (e.g. "Synology SMART failing") belong to consumer epics.

## CLI & config (LOCKED)

- **CLI `hm ssh-probe`:** `keygen <target>` (ed25519 → secrets, print public key), `capture-hostkey <target>`
  (pin the host key), `install-instructions <target>` (account-mode-aware: appliance → authorized_keys line +
  persistence warning; dedicated-user → create-user + install-script + sudoers-line + authorized_keys line),
  `test <target>` (connect + run the forced command + **verify the restriction** — an arbitrary command is
  refused/overridden).
- **Config:** `ssh_targets:` in plugin config (pydantic-validated; per-target `host`/`port`/`account_mode`/
  `user`/key-secret-ref/pinned-host-key/forced-command-or-script-id/`concurrency_group`). Public default is
  **empty** (no targets); the user's overrides repo declares the UDM + Synology.

## UI (headless — Option B)

EPIC-017 ships **NO dedicated UI**. SSH-probe state surfaces via: (1) **alerting** (`ssh.yaml`),
(2) **Grafana** (consumer dashboards include `homelab_ssh_*` panels), and (3) **consumer integration pages** —
the Unifi/Network pages and the Synology page each render THEIR OWN probe states (last success, host-key
status, duration). The framework provides the metrics; consumers render. (EPIC-007 + EPIC-008 carry the
rendering scope notes; no EPIC-017 UI stage.)

## Deferred scope (explicitly NOT built here)

- **Remote-cron SSH-pull discovery + SSH-push wrapper install** (the scope EPIC-002's cron derived-state
  redesign had assigned here). DEFERRED entirely: the recon found neither real target has user-relevant cron
  jobs that aren't better-sourced elsewhere (UDM = housekeeping noise; Synology backup/SMART scheduling =
  EPIC-008 DSM-API). **Architecture recorded for if/when a remote host with real custom crons appears:** a
  separately-installed, scoped script consuming THIS epic's `SshClientFactory` transport (a `dedicated-user`
  forced-command path), in its own cron-remote-management home (NOT folded into this read-only-probe
  framework — it is a write path). When built it depends on 017's transport. EPIC-002's CronDetail UI
  references to EPIC-017 are updated to "remote-cron SSH deferred" (STAGE-017-008).
- **Consumer probes** — the Unifi DHCP-lease probe (EPIC-007-012) and the Synology SMART/btrfs/df probes +
  their sudoers (EPIC-008) are NOT here; this epic ships only the trivial `uptime` exemplar.
- **Automated key install** to targets' auth config (we generate instructions; the user installs).
- **Connection pooling** (open-per-run is sufficient at these cadences).

## Stage decomposition (8 stages, sequential)

Re-decomposed from scratch (the pre-brainstorm sketch is fully superseded). Framework-only; the only probe is
the exemplar. Alert rules come AFTER the exemplar so they validate against real `homelab_ssh_*` data (the
"rules validate against real data" discipline from EPIC-006/007).

| # | Stage | Theme |
|---|---|---|
| STAGE-017-001 | asyncssh transport: add the dep; implement `SshClientFactory.open(target_id)` + fill the `SshConnection` Protocol (connect-and-run-pinned-command → typed output); per-target pinned host-key verification; open-per-run; per-target concurrency group |
| STAGE-017-002 | `ssh_targets:` config model (pydantic; per-target fields; empty public default) + per-target key secret model (`ssh_probe_key_<target>`, read via `ctx.secrets.get`, written via `AsyncSecretsRepository`) |
| STAGE-017-003 | `SshProbe` base collector (open→run→parse→emit→close; `ok=True`-when-target-down) + framework health metrics (`homelab_ssh_up`/`_probe_duration_seconds`/`_last_success_age_seconds`/`_host_key_mismatch`) |
| STAGE-017-004 | `hm ssh-probe keygen` (ed25519 → secrets, print pubkey) + `capture-hostkey` (pin host key) |
| STAGE-017-005 | `hm ssh-probe install-instructions` (account-mode-aware: appliance authorized_keys-line + persistence-warning; dedicated-user create-user + script + sudoers-line + authorized_keys-line) + `test` (connect + run forced command + verify the restriction holds) |
| STAGE-017-006 | `uptime` exemplar probe — BOTH account-modes against BOTH real targets (UDM `appliance` inlined-command; Synology `dedicated-user` installed-script); emits `homelab_ssh_up` + `homelab_ssh_uptime_seconds`; end-to-end keygen→install→pin→probe→verify-restriction |
| STAGE-017-007 | `deploy/vmalert/metrics/ssh.yaml` framework-health rules (`SshTargetUnreachable` warning, `SshHostKeyMismatch` critical, `SshProbeStale` warning) — AFTER the exemplar so they validate against real metrics |
| STAGE-017-008 | Cross-epic reconciliation: update EPIC-002 CronDetail UI EPIC-017 references → "remote-cron SSH deferred"; record the deferred cron-SSH (Option-B architecture) + EPIC-007/008 consumer-rendering notes + the EPIC-008 sudoers-hop contract |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:
- **Setup instructions never include the private key** — only the public key + the account-mode-aware steps.
  The private key stays in the secrets store.
- **Forced command is the enforced boundary** — a test (017-005 `test` + 017-006 exemplar) verifies that
  connecting and trying to run an arbitrary command fails (the forced command runs instead).
- **Per-target user / least privilege** — `dedicated-user` targets use a dedicated low-priv user (never
  root/admin); `appliance` targets lean on the forced command as the boundary. Document both.
- **Host-key pinning enforced** — no connection without a verified pinned host key; a mismatch is a critical
  `homelab_ssh_host_key_mismatch` signal, distinct from "target down."
- **Read-only / observe-only** — no write paths to targets' auth config or anything else; the framework only
  runs the pinned read commands.
- **Self-observing** — every probe emits the `homelab_ssh_*` health surface + `homelab_collector_run_*`.

## Dependencies

- EPIC-001 (kernel: `CollectorContext`/`SshClientFactory` stubs, secrets store, collector base, scheduler).
- **Consumers built AFTER this epic:** EPIC-007 (Unifi DHCP-lease probe, STAGE-007-012) and EPIC-008
  (Synology DSM probes + sudoers). They use this framework's transport + forced-command + (Synology) script/
  sudoers mechanism.

## Notes

- `asyncssh` is the locked library (async-native; fits the FastAPI loop; `run_kind=ASYNC`). paramiko was
  rejected (would force `run_kind=THREAD`).
- Key generation: `hm ssh-probe keygen` produces a fresh ed25519 key per target; private key in secrets,
  public key printed for manual install.
- **UDM persistence caveat:** the forced-command key on the UDM lives on overlayfs and is likely wiped on
  UniFi OS firmware updates — the `install-instructions` output for `appliance` targets MUST warn the user to
  re-apply after upgrades.
- **Synology privileged-data caveat:** SMART/btrfs need root; the `dedicated-user` script reaches them via a
  narrow NOPASSWD sudoers entry. The framework provides the sudoers-line-generation mechanism; the SPECIFIC
  commands (`smartctl -a /dev/sd*`, `btrfs scrub status *`, etc.) are EPIC-008's deliverable.
