# Host prerequisites for homelab-monitor

This document lists the one-time host-side setup required before
`docker compose up monitor` will succeed in production.

## Cron discovery (STAGE-002-007, STAGE-002-009, STAGE-002-009A)

The cron-discoverer plugin discovers the host's crontab files via read-only
bind-mounts. To grant the container read access without ACLs (which break
vixie-cron):

### 1. Run the setup script

```bash
sudo bash scripts/host-setup.sh
```

The script is idempotent: re-running it is a no-op. To preview what it
would do without making changes:

```bash
sudo bash scripts/host-setup.sh --check
```

To apply AND write the resolved UID/GID/hostname directly into an env file
(idempotent — replaces existing keys or appends if missing; preserves file
permissions):

```bash
sudo bash scripts/host-setup.sh --write-env deploy/compose/.env
# Or for dev:
sudo bash scripts/host-setup.sh --write-env deploy/dev/dev.env
```

The script:
- Creates the `homelab-monitor` host user (system user, no shell).
- Adds it to the `crontab` group (Debian/Ubuntu).
- Sets read ACLs on `/etc/crontab` and `/etc/cron.d/` (system crontabs only).
  Requires `setfacl` (Debian/Ubuntu: `apt install acl`). Real user crontabs
  (`/var/spool/cron/crontabs/*`) are **NOT** given an ACL — an ACL makes
  vixie-cron reject the crontab as INSECURE MODE and refuse to run it.
- Installs and enables three systemd units for the **crontab-snapshot**
  mechanism (STAGE-002-009 Option B fix):
  - `homelab-monitor-crontab-snapshot.service` — `Type=oneshot` unit that runs
    the snapshot script.
  - `homelab-monitor-crontab-snapshot.path` — watches `/var/spool/cron/crontabs`
    for changes and triggers the snapshot refresh.
  - `homelab-monitor-crontab-snapshot.timer` — periodic refresh (~300s) as a
    backstop.
- Installs the `hm-crontab-snapshot` host script, which runs `crontab -l -u
  <user>` (the cron-sanctioned read path) and writes a world-readable snapshot
  of each user's crontab into `/var/lib/homelab-monitor/crontab-snapshot`. The
  discoverer reads the snapshot instead of the real `0600` spool files.
- Installs and enables three systemd units for the **cron-apply executor**
  (STAGE-002-009 — the host-side privileged-write path for wrapper installs):
  - `homelab-monitor-cron-apply.service` — `Type=oneshot` root executor
    (`hm-cron-apply`) that processes wrapper-install request files.
  - `homelab-monitor-cron-apply.path` — watches
    `/var/lib/homelab-monitor/cron-apply/requests` and triggers the executor on
    each new request.
  - `homelab-monitor-cron-apply.timer` — a 60-second safety-net sweep that runs
    the executor even if the `.path` watcher misses a filesystem event.
- Installs the `hm-cron-apply` host script and creates the IPC directory tree
  `/var/lib/homelab-monitor/cron-apply/{requests,results}`. Requires `jq` on
  the host (the executor parses request JSON with it).
- Removes any stale `homelab-monitor-crontab-acl.{path,service}` units and the
  `refresh-crontab-acl.sh` script left by older installs — that ACL-based
  approach was retired because an ACL on a spool file makes vixie-cron reject
  the crontab as `INSECURE MODE`.

**Run this script ONCE per machine** for initial setup. The `.path` watchers
and `.timer`s keep the snapshot fresh and process cron-apply requests
automatically. No operator action is needed in steady state. (On a host without
`systemd`, the watchers and timers cannot be installed — manually run
`hm-crontab-snapshot` after each `crontab -e`; the cron-apply executor /
wrapper-install feature requires systemd.)

> **Re-run after upgrades that change the host scripts.** `host-setup.sh` is
> also the **deploy mechanism** for the non-containerized host scripts — a
> `docker compose up --build` does **not** update them. After pulling a new
> version of the repo, re-run the script if `scripts/hm-cron-apply.sh` or
> `scripts/hm-crontab-snapshot.sh` changed (these are installed to
> `/usr/local/sbin/hm-cron-apply` and `/usr/local/sbin/hm-crontab-snapshot`
> respectively). The script is idempotent: it uses a content-diff (`cmp`)
> before each install step and skips files whose content is already current,
> so re-running it on an unchanged host is a no-op.
>
> ```bash
> sudo bash scripts/host-setup.sh
> ```
>
> **Bugfix (2026-06-17):** `/etc/crontab` wrapping now works under `systemd ProtectSystem=strict`
> (previously failed silently). Commands with backslash escapes (e.g., Debian certbot's `\!`) now wrap
> correctly (awk escape-processing bug fixed). Re-run `host-setup.sh` after June 2026 updates to deploy
> the fixed executor.

### 2. Update your env

Paste the UID/GID/hostname from the script's output into your env file
(or use `--write-env` above to skip this step):

```
HM_CRON_HOST_UID=<from-script>
HM_CRON_HOST_GID=<from-script>
HM_HOST_HOSTNAME=<from-script>
```

Production: edit your `.env` next to `docker-compose.yml`.
Dev rig: edit `deploy/dev/dev.env`.

### 3. Restart the monitor

```bash
docker compose up -d --force-recreate monitor
```

## Compose bind-mounts

The monitor container's host mounts are wired in
`deploy/compose/docker-compose.yml`:

```yaml
volumes:
  - /etc:/host/etc:ro                                                    # /etc/crontab + /etc/cron.d/* (READ-ONLY)
  - /var/lib/homelab-monitor/crontab-snapshot:/host-crontab-snapshot:ro   # user crontab snapshot (READ-ONLY)
  - /var/lib/homelab-monitor/cron-apply:/host-ipc:rw                      # cron-apply IPC (READ-WRITE)
  - /proc:/host/proc:ro                                                    # host /proc for btime (READ-ONLY, STAGE-002-010)
```

System crontabs (`/etc/crontab`, `/etc/cron.d/*`) are read directly from
`/host/etc`. User crontabs are read from the host-generated snapshot
directory (`HM_CRON_SNAPSHOT_DIR`, default `/host-crontab-snapshot`). The
snapshot is populated by the `hm-crontab-snapshot` host script (installed
and triggered by `host-setup.sh`), which reads each user's crontab via the
cron-sanctioned `crontab -l -u <user>` path and writes it to a world-readable
snapshot file. The real `0600` spool files are never modified.

The `/host-ipc` mount is the **only** read-write host mount and the single
privilege boundary. The container writes wrapper-install request files into
`requests/`; the host-side cron-apply executor writes result files into
`results/`. The container never writes any other host path — the crontab
rewrite, the wrapper script, and the token file are all written by the
executor on the host. See `docs/cron/install-heartbeat.md`.

The container reads from `/host/...` and `/host-crontab-snapshot` paths; the
database stores the equivalent HOST paths (e.g., `/etc/crontab`, `crontab:alice`)
so fingerprints converge with the wrapper installer (see
`docs/architecture/cron-identity.md`).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HM_CRON_HOST_UID` | `1000` | Host UID for `homelab-monitor` user; container drops to this UID |
| `HM_CRON_HOST_GID` | `1000` | Host GID for `homelab-monitor` user |
| `HM_HOST_HOSTNAME` | (empty → `socket.gethostname()`) | Stored on every discovered cron row's `host` column |
| `HM_CRON_HOST_ROOT` | `/host` | Container-side prefix where host bind-mounts land (/etc, /etc/cron.d) |
| `HM_CRON_SNAPSHOT_DIR` | `/host-crontab-snapshot` | Container-side path of the host crontab-snapshot directory (Option B fix). Bind-mounted read-only. |
| `HM_CRON_APPLY_IPC_DIR` | `/host-ipc` | Container-side path of the cron-apply IPC directory. Bind-mounted read-write. The monitor writes `requests/` files here; the host executor writes `results/`. |
| `HM_CRON_DISCOVERY_INTERVAL_SECONDS` | `300` | Discoverer tick interval; frozen at module-import time |
| `HOMELAB_MONITOR_PUBLIC_URL` | (none — unset) | Host-reachable base URL baked into the heartbeat wrapper. **No default;** a wrapper install fails with HTTP 400 if unset. Required only for the wrapper-install feature. |
| `HOMELAB_MONITOR_BIND_HOST` | `127.0.0.1` | Host address the monitor port binds to. Set to `0.0.0.0` for LAN access in trusted homelab networks. |

### Distro variance

- **Debian / Ubuntu** (default): user crontabs at `/var/spool/cron/crontabs/`. Script works as-is.
- **RHEL / CentOS / Fedora**: user crontabs at `/var/spool/cron/`. Edit the
  `SPOOL_DIR` constant in `scripts/hm-crontab-snapshot.sh` to match your distro:
  ```bash
  readonly SPOOL_DIR="${_ROOT}/var/spool/cron"  # change from /var/spool/cron/crontabs
  ```
  The snapshot mechanism and bind-mounts remain the same.
- **Arch / other**: check `man crontab` for the spool dir. Edit
  `scripts/hm-crontab-snapshot.sh` and `docs/architecture/cron-logscrape.md`
  accordingly. The snapshot mechanism is spool-path-agnostic; only the source
  directory name needs adjustment.

### Security implications

- The container has **read-only** access to `/etc` and `/var/spool/cron/crontabs`.
- It runs as a dedicated, low-privilege host user (no shell, no sudo).
- Crontab secrets (if any operator stores secrets in cron commands) become
  readable by the monitor process. Use environment variables in cron jobs
  instead — best practice anyway.

### Host-side systemd artifacts

`host-setup.sh` installs two independent host-side mechanisms (STAGE-002-009):
the **crontab-snapshot** (read path for discovery) and the **cron-apply
executor** (privileged-write path for wrapper installs). Each is three systemd
units plus a root script.

**Crontab-snapshot** — keeps the discovery snapshot fresh:

| Artifact | Installed to | Purpose |
|---|---|---|
| `hm-crontab-snapshot` | `/usr/local/sbin/hm-crontab-snapshot` | Runs `crontab -l -u <user>` for each user and writes a world-readable snapshot. Idempotent. |
| `homelab-monitor-crontab-snapshot.service` | `/etc/systemd/system/` | `Type=oneshot` unit that runs the snapshot script. |
| `homelab-monitor-crontab-snapshot.path` | `/etc/systemd/system/` | `PathChanged=` watcher on `/var/spool/cron/crontabs`; triggers the service on every change. |
| `homelab-monitor-crontab-snapshot.timer` | `/etc/systemd/system/` | Periodic refresh (~300s) as a backstop. |

**Cron-apply executor** — the host-side process that performs every privileged
write for a heartbeat-wrapper install **or removal**. The monitor container has
no host-write capability; it only writes a request JSON into the IPC
`requests/` directory, and this executor (running as root) applies the wrapper
script, the token file, and the crontab rewrite atomically with rollback.

The executor supports four operations:

- `write-wrapper-script` — write the wrapper script to its FIXED host path.
- `write-token` — write the heartbeat token to its FIXED host path.
- `wrap-crontab` — rewrite a crontab line to its wrapped form.
- `unwrap-crontab` — strip the wrapper prefix from a wrapped crontab line
  (STAGE-002-009A; the inverse of `wrap-crontab`). A wrapper *install* request
  carries all three of the first ops; an *uninstall* request carries only
  `unwrap-crontab`.

After a successful `wrap-crontab` / `unwrap-crontab`, the executor also
refreshes the world-readable crontab snapshot (`/var/lib/homelab-monitor/crontab-snapshot/<user>`)
**inline** — a best-effort copy of the just-written spool file — so the
monitor's next install/uninstall dry-run gate sees the fresh state immediately
instead of waiting up to 300s for the snapshot timer (STAGE-002-009A). This
inline refresh applies only to user crontabs (`crontab:<user>`); `/etc/crontab`
and `/etc/cron.d/*` are read directly from the container's `/etc` bind mount
and have no snapshot mirror.

| Artifact | Installed to | Purpose |
|---|---|---|
| `hm-cron-apply` | `/usr/local/sbin/hm-cron-apply` | Root executor: reads a request JSON, applies the operations atomically (snapshot + rollback on failure), refreshes the crontab snapshot after a wrap/unwrap, writes a result JSON. Requires `jq`. |
| `homelab-monitor-cron-apply.service` | `/etc/systemd/system/` | `Type=oneshot` unit that runs the executor. Hardened (`ProtectSystem=strict`, explicit `ReadWritePaths`). |
| `homelab-monitor-cron-apply.path` | `/etc/systemd/system/` | `PathChanged=` watcher on `/var/lib/homelab-monitor/cron-apply/requests`; triggers the executor on each new request. |
| `homelab-monitor-cron-apply.timer` | `/etc/systemd/system/` | 60-second safety-net sweep — runs the executor even if the `.path` watcher missed a filesystem event. |
| IPC directory | `/var/lib/homelab-monitor/cron-apply/{requests,results}` | `requests/` owned by the monitor user (container writes); `results/` owned by root (executor writes). |

The executor also writes the crontab-snapshot directory
(`/var/lib/homelab-monitor/crontab-snapshot`) after a successful wrap/unwrap —
this is the same directory the `hm-crontab-snapshot` script and its timer
maintain; both write paths are root-owned and produce byte-identical content.
Both `/var/lib/homelab-monitor/cron-apply` and
`/var/lib/homelab-monitor/crontab-snapshot` are therefore in the cron-apply
service's `ReadWritePaths` allow-list.

The repo source for all six units lives in `deploy/systemd/`; the scripts in
`scripts/hm-crontab-snapshot.sh` and `scripts/hm-cron-apply.sh`.

Inspect the units:

```bash
systemctl status homelab-monitor-crontab-snapshot.path
systemctl status homelab-monitor-crontab-snapshot.timer
journalctl -u homelab-monitor-crontab-snapshot.service   # see each snapshot

systemctl status homelab-monitor-cron-apply.path
systemctl status homelab-monitor-cron-apply.timer
journalctl -u homelab-monitor-cron-apply.service         # see each apply run
```

To refresh the crontab snapshot by hand at any time:

```bash
sudo /usr/local/sbin/hm-crontab-snapshot
```

### Disabling discovery

Set `HM_CRON_DISCOVERY_INTERVAL_SECONDS=86400` (24h) to effectively disable
periodic polling. The `POST /api/crons/discover-now` endpoint remains
available for manual triggers.
