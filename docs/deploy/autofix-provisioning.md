# Auto-fix provisioning (homelab-fixer + transcript ACLs)

This document covers the one-time host-side setup the optional auto-remediation
subsystem needs. It is the open-source-safe surface; the real host-specific
numeric IDs live in your private overrides env, not in this repo.

> **Scope (STAGE-009-002).** This stage provisions only the *filesystem grant*:
> the runbook-transcript directory, its POSIX default ACLs, and the
> `HM_FIXER_UID`/`HM_FIXER_GID` identity contract. The fixer-runner container
> itself, the in-container `homelab-fixer` OS user, and the orchestrator that
> invokes `docker exec` are deferred to STAGE-009-003 and later EPIC-009 stages.
> Nothing here gives the fixer any capability to run anything yet.

## The `homelab-fixer` identity model

Auto-remediation runs Claude as a dedicated, low-privilege identity named
`homelab-fixer`, **never root and never your desktop user** (non-negotiable #3).
That OS user is created *inside* a separate fixer-runner container (STAGE-009-003),
not on the host. What the host needs to know about it now is only its numeric
identity, so the host can grant that UID a narrow file ACL:

- `HM_FIXER_UID` — the numeric UID the in-container `homelab-fixer` user adopts.
- `HM_FIXER_GID` — its primary GID (also the fallback shared group when ACLs are
  unavailable).

The fixer gets **no** docker-group membership and **no** sudoers from this stage.
The only privilege established here is read/write on one directory.

## The transcript directory + ACL model

Every auto-fix run records a full Claude transcript (non-negotiable #4: full
audit). Those transcripts are written by the fixer-runner and read by the
monitor (the transcript viewer + audit). To make that cross-container hand-off
work safely:

- Transcripts live on a **host bind-mount**, default
  `/var/lib/homelab-monitor/runbook-transcripts`, mounted into the monitor
  container at `/data/runbook-transcripts`. A host path (not a named docker
  volume) means the existing host-path backup already covers it and it survives
  container rebuilds.
- The monitor mounts that path **read-only** (`:ro`). This is deliberate: the
  monitor must not be able to mutate an in-progress audit transcript
  (non-negotiable #4 audit integrity).
- `scripts/host-setup.sh` applies POSIX **default** ACLs on the directory so
  every file the runner creates *inherits* the right permissions regardless of
  the writer's UID or umask:
  - monitor UID → `r-x` (read-only)
  - fixer UID (`HM_FIXER_UID`) → `rwx` (write)

Default ACLs are used (not same-UID assumptions) because the monitor runs at a
host-specific runtime UID that need not equal the fixer's UID.

### setfacl-absent fallback

If the host lacks `setfacl` (the `acl` package), `host-setup.sh` WARN-degrades to
a shared supplementary group (`homelab-fixer`) + a setgid directory
(`chmod 2770`). This is weaker than the ACL path: under the fallback the monitor
gains group write, so the read-only-monitor guarantee of #4 is not enforced.
Install the `acl` package (Debian/Ubuntu: `sudo apt install acl`) to get the
intended posture.

## Run the setup

```bash
# Set the real fixer UID/GID first if 1002 collides on your host (recommended:
# put them in your overrides env). Then:
sudo bash scripts/host-setup.sh

# Preview without mutating:
sudo bash scripts/host-setup.sh --check

# Apply AND write the resolved IDs into your env file:
sudo bash scripts/host-setup.sh --write-env deploy/compose/.env
```

`host-setup.sh` is idempotent — re-running it is a no-op. It creates the
transcript directory and applies the ACLs alongside its existing cron-discovery
provisioning.

After running, restart the monitor so it picks up the new bind-mount:

```bash
docker compose up -d --force-recreate monitor
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HM_FIXER_UID` | `1002` | Numeric UID granted `rwX` on the transcript dir; the in-container `homelab-fixer` user (STAGE-009-003) must adopt this UID. Set the real value in your overrides env. |
| `HM_FIXER_GID` | `1002` | Fixer GID; also the shared fallback group GID when `setfacl` is absent. |
| `HM_FIXER_TRANSCRIPTS_SRC` | `/var/lib/homelab-monitor/runbook-transcripts` | Host path bind-mounted into the monitor at `/data/runbook-transcripts` (read-only). |

> Defaults `1002:1002` avoid colliding with this project's own service IDs (1000
> homelab, 2000 amconfig, 999 docker, 994 homelab-compose). They are **not**
> guaranteed free against arbitrary host users — verify against your host's
> `/etc/passwd` and `/etc/group` and override via the overrides repo if 1002 is
> taken.

## Docker-socket dependency (orchestrator `docker exec`)

The auto-fix orchestrator (a later EPIC-009 stage) runs the fixer by
`docker exec`-ing into the fixer-runner container. That capability relies on the
monitor's **existing** docker-socket access — the monitor already mounts
`/var/run/docker.sock` (read-write) and is added to the host docker group
(`HM_HOST_DOCKER_GID`), provisioned back in EPIC-003 for Pull & Restart. This
stage does **not** re-provision the socket; it depends on that mount being
present. To confirm the path works on your host:

```bash
# From the monitor container, exec into any sibling container (read-only check):
docker compose exec monitor docker exec <some-sibling-container> true && echo "docker exec path OK"
```

If that succeeds, the orchestrator's exec path is usable. No additional
permission beyond the existing socket mount + docker group is required for
`docker exec` (it is the same socket API surface as discovery).

## Security implications

- The fixer identity is low-privilege: no shell-on-host, no sudoers, no docker
  group from this stage. Its only host capability is writing transcripts into
  one directory.
- The monitor's access to transcripts is **read-only** — it cannot alter audit
  records.
- The docker socket is root-equivalent on the host. Auto-fix runs that use it
  are confirm-gated, rate-limited, and fully audited (see the auto-fix safety
  model in the design spec §7.4).

## Deferred to later stages

- The `fixer-runner` service block in `docker-compose.yml` (its
  `USER homelab-fixer`, its own read-write transcript mount, egress-only
  network) → **STAGE-009-003**.
- Creating the in-container `homelab-fixer` OS user in the runner image →
  **STAGE-009-003**.
- The orchestrator `docker exec` invocation + populating the
  `runbook_runs.fixer_user` / `runbook_runs.host` audit columns → later
  EPIC-009 orchestrator-wiring stages (the exec itself in STAGE-009-005; the
  column writes in the populating-write stage).
