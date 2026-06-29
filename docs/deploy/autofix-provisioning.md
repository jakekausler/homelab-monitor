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

## Claude CLI + API credential (STAGE-009-003)

The fixer-runner image bakes a **version-pinned native `claude` binary** (no
Node, auto-update disabled). The version is controlled by the `CLAUDE_VERSION`
build arg (`deploy/compose/.env`, default `latest` — pin a specific version in
production).

The Claude API credential is supplied via `ANTHROPIC_API_KEY`:

- It is a **host/overrides-only** value. **NEVER commit a real key to this repo.**
  Put it in your private overrides `.env` or the host environment.
- In `deploy/compose/.env` it is wired as a passthrough
  (`ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}`); leave the `.env.example` entry
  empty.
- Under CI and the integration test the image is built with
  `CLAUDE_BINARY_SOURCE=fake`, so no real key is used and the fake records
  `anthropic_api_key_present=0`.

> **Deferral (STAGE-009-005).** This static passthrough is a placeholder. A later
> stage moves the key to **exec-time vault injection**: the orchestrator fetches
> the key from the vault and supplies it per `docker exec -e ANTHROPIC_API_KEY=…`,
> rather than baking it into the container environment via compose.

## fixer-runner service wiring (STAGE-009-003)

The `fixer-runner` service is **OFF BY DEFAULT**, gated behind the `fixer`
compose profile (mirroring the `host-collectors` profile). To enable it, add
`fixer` to `COMPOSE_PROFILES` in `deploy/compose/.env`:

```bash
COMPOSE_PROFILES=host-collectors,fixer
docker compose up -d fixer-runner
```

Key properties:

- **Identity is baked at build time** (`FIXER_UID`/`FIXER_GID` build args, default
  1002:1002 from `HM_FIXER_UID`/`HM_FIXER_GID`). There is intentionally **no**
  compose `user:` override — the image's `USER homelab-fixer` is the identity, so
  it matches the transcript-dir POSIX default ACL granted to `HM_FIXER_UID`.
- **PID 1 is `tail -f /dev/null`** — an idle keepalive. `claude` is NEVER launched
  at boot; the orchestrator (STAGE-009-005) `docker exec`s it on demand. `docker
  kill` stops the container and any in-flight exec (non-negotiable #7).
- **Read-write transcript mount.** The fixer is the WRITER: it mounts the same
  host transcript path the monitor mounts read-only, but read-write
  (`…/runbook-transcripts:/data/runbook-transcripts`, no `:ro`). The POSIX default
  ACLs (host-setup.sh §3.9) keep every file it writes monitor-readable.
- **Dedicated egress network** (`fixer-egress`). It is internet-reachable (claude
  needs the Anthropic API) and SEPARATE from `homelab-monitor-net`, so the fixer
  cannot reach the monitor's sidecars directly. **No ports are published.**

> **Deferral (STAGE-009-008).** Per-runbook egress DESTINATION filtering
> (restricting which endpoints the `fixer-egress` network can reach) is owned by
> STAGE-009-008.

## Baked CLAUDE.md floor vs mounted host overlay (STAGE-009-003)

`claude` is governed by two layers:

1. **Baked public floor** — a public-safe `CLAUDE.md` baked into the image at
   `/home/homelab-fixer/CLAUDE.md` (also the container WORKDIR, so `claude`
   discovers it). It contains ONLY universal invariants: you are in a container
   not the host; you are fully non-interactive (never wait for or request input);
   deny by default; if uncertain, exit non-zero. It contains **no** host-specific
   targets.
2. **Mounted host overlay** — an optional, read-only host-specific overlay at
   `/data/policy/CLAUDE.host.md`, sourced from your private overrides repo
   (`HM_FIXER_POLICY_OVERLAY_SRC`, default `/dev/null` = absent). It carries the
   host-specific allow/deny TARGET list. **The overlay can only NARROW the floor**
   — it can never widen the baked invariants.

> **Deferral (STAGE-009-008).** The overlay's real TARGET content lives in the
> private overrides repo and is owned by STAGE-009-008. This stage ships only the
> baked floor + the read-only mount point + the compose plumbing.

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

- The orchestrator `docker exec` invocation that launches `claude`, plus
  exec-time vault injection of `ANTHROPIC_API_KEY`, plus populating the
  `runbook_runs.fixer_user` / `runbook_runs.host` audit columns →
  **STAGE-009-005** (the exec + key injection) and the populating-write stage
  (the column writes).
- Per-runbook egress DESTINATION filtering on the `fixer-egress` network, and the
  real host-specific allow/deny TARGET content of the mounted
  `/data/policy/CLAUDE.host.md` overlay → **STAGE-009-008** + the private
  overrides repo.

> Delivered in STAGE-009-003: the `fixer-runner` service block, the in-image
> `homelab-fixer` OS user (`USER homelab-fixer`), the read-write transcript mount,
> the dedicated `fixer-egress` network, the idle-keepalive entrypoint, the baked
> CLAUDE.md floor + read-only overlay mount point, and the `ANTHROPIC_API_KEY` /
> `CLAUDE_VERSION` compose plumbing.
