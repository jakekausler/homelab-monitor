# Host prerequisites for homelab-monitor

This document lists the one-time host-side setup required before
`docker compose up monitor` will succeed in production.

## Cron discovery (STAGE-002-007, STAGE-002-007A)

The cron-discoverer plugin reads the host's crontab files via read-only
bind-mounts. To grant the container access without making it root:

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
- Sets read ACLs on `/var/spool/cron/crontabs/`: directory traversal (rx),
  per-existing-file (r), AND default ACL (r) for new files. Requires
  `setfacl` (Debian/Ubuntu: `apt install acl`).
- Installs and enables a systemd `.path` unit
  (`homelab-monitor-crontab-acl.path`) that watches
  `/var/spool/cron/crontabs/` and re-applies the read ACLs on every change.
  Requires `systemctl` (any systemd host).

**Run this script ONCE per machine.** `crontab -e` does not edit a crontab in
place — it `rename()`s a new `0600` file into the spool directory, and a moved
file does NOT inherit the directory's default ACL. The installed `.path` unit
catches every such change and re-runs `/usr/local/sbin/refresh-crontab-acl.sh`,
so the container never loses read access. No operator action is needed after
the initial run. (On a host without `systemd`, the watcher cannot be
installed — re-run `host-setup.sh` manually after each `crontab -e`.)

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

## Compose bind-mounts (read-only)

The monitor container reads the host's crontab files via two read-only
bind-mounts wired in `deploy/compose/docker-compose.yml`:

```yaml
volumes:
  - /etc:/host/etc:ro                               # /etc/crontab + /etc/cron.d/*
  - /var/spool/cron/crontabs:/host/var/spool/cron/crontabs:ro
```

The container reads from `/host/...` paths (configurable via
`HM_CRON_HOST_ROOT`, default `/host`); the database stores the equivalent
HOST paths (e.g., `/etc/crontab`, `crontab:alice`) so fingerprints converge
with wrapper installers (see `docs/architecture/cron-identity.md`).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HM_CRON_HOST_UID` | `1000` | Host UID for `homelab-monitor` user; container drops to this UID |
| `HM_CRON_HOST_GID` | `1000` | Host GID for `homelab-monitor` user |
| `HM_HOST_HOSTNAME` | (empty → `socket.gethostname()`) | Stored on every discovered cron row's `host` column |
| `HM_CRON_HOST_ROOT` | `/host` | Container-side prefix where host bind-mounts land |
| `HM_CRON_DISCOVERY_INTERVAL_SECONDS` | `300` | Discoverer tick interval; frozen at module-import time |
| `HOMELAB_MONITOR_BIND_HOST` | `127.0.0.1` | Host address the monitor port binds to. Set to `0.0.0.0` for LAN access in trusted homelab networks. |

### Distro variance

- **Debian / Ubuntu** (default): user crontabs at `/var/spool/cron/crontabs/`. Script works as-is.
- **RHEL / CentOS / Fedora**: user crontabs at `/var/spool/cron/`. Edit the bind-mount in `docker-compose.yml`:
  ```yaml
  - /var/spool/cron:/host/var/spool/cron/crontabs:ro
  ```
  (Map the distro's path to the container's expected location.)
- **Arch / other**: check `man crontab` for the spool dir. Adjust the mount accordingly.

### Security implications

- The container has **read-only** access to `/etc` and `/var/spool/cron/crontabs`.
- It runs as a dedicated, low-privilege host user (no shell, no sudo).
- Crontab secrets (if any operator stores secrets in cron commands) become
  readable by the monitor process. Use environment variables in cron jobs
  instead — best practice anyway.

### Crontab ACL watcher (systemd)

`host-setup.sh` installs three host-side artifacts so per-user crontab ACLs
survive `crontab -e`:

| Artifact | Installed to | Purpose |
|---|---|---|
| `refresh-crontab-acl.sh` | `/usr/local/sbin/refresh-crontab-acl.sh` | Re-applies `homelab-monitor` read ACLs on the spool dir and every file in it. Idempotent. |
| `homelab-monitor-crontab-acl.service` | `/etc/systemd/system/` | `Type=oneshot` unit that runs the refresh script. |
| `homelab-monitor-crontab-acl.path` | `/etc/systemd/system/` | `PathChanged=` watcher on `/var/spool/cron/crontabs`; triggers the service on every change. |

The repo source for these lives in `deploy/systemd/` (units) and
`scripts/refresh-crontab-acl.sh`.

Inspect the watcher:

```bash
systemctl status homelab-monitor-crontab-acl.path
journalctl -u homelab-monitor-crontab-acl.service   # see each ACL refresh
```

To re-apply ACLs by hand at any time:

```bash
sudo /usr/local/sbin/refresh-crontab-acl.sh
```

### Disabling discovery

Set `HM_CRON_DISCOVERY_INTERVAL_SECONDS=86400` (24h) to effectively disable
periodic polling. The `POST /api/crons/discover-now` endpoint remains
available for manual triggers.
