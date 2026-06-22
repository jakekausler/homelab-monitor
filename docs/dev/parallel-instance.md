# Parallel Instance (instance B) — operator guide

This guide explains how to run a **second, fully independent** instance of
homelab-monitor ("instance B") on the same host, alongside the existing
production instance ("instance A"). It documents the variables, the bring-up
procedure that was validated live, A's one-time migration, teardown, and the
known limitations.

> Source of truth for the rationale and the defects that were found/fixed during
> validation: `PARALLEL-INSTANCE-PLAN.md` at the repo root. The authoritative
> variable list with inline comments lives in `deploy/compose/.env.example`.

---

## 1. Overview / when to use

Instance B exists so you can develop against a **full, prod-like compose stack**
without disturbing the live monitor (instance A). Both stacks run concurrently
on the same host with no project / container / network / volume / port
collision. In the validated run, A served on `127.0.0.1:29090` and B served on
`0.0.0.0:39090`.

Instance B is **dev-only**, but it is a real `docker compose` prod stack (the
same `deploy/compose/docker-compose.yml`), not the hybrid `make dev` rig.

### Constraints (by design)

- **B does NO Docker container monitoring.** It runs with
  `HOMELAB_MONITOR_DOCKER_ENABLED=false`, which skips registration of the Docker
  discoverer and the Docker socket collector entirely. The docker API endpoints
  return `503`, and the auto-degrading consumers (probe supervisor, image-update
  collector, local-build collector) receive a `None` socket client and degrade
  cleanly.
- **B does NOT participate in host integration.** The cron snapshot/apply IPC,
  the `homelab-monitor` host user/group, the `/usr/local/sbin` executors, and
  the systemd units are a **host singleton owned by instance A**. B points its
  cron/etc/proc bind-mount **sources** at B-private (empty) directories, so cron
  discovery is a clean no-op and cron-apply returns a clean `503` if invoked.
- **B never touches A's data.** Namespaced volumes, a distinct SQLite DB volume,
  and B-private mount sources keep the two instances fully isolated.

If you only need to iterate on backend/frontend code (not the full sidecar
stack), use `make dev` (hybrid dev rig) instead — see
`docs/dev/local-environment.md`. Reach for instance B when you specifically need
a second **prod-like** stack running in parallel with the live monitor.

---

## 2. The variables

All of these live in `deploy/compose/.env` (gitignored). Defaults reproduce
instance A's exact behavior, so **A needs no new variables** except
`COMPOSE_PROFILES` (see §5). Set the following on **instance B**:

| Variable | Default | Set on B to | Purpose |
| --- | --- | --- | --- |
| `HM_INSTANCE` | `homelab-monitor` | `homelab-monitor-b` | Namespaces the **project name**, the **network**, and (via the project-name prefix) all **named volumes**. |
| `HM_CONTAINER_PREFIX` | `homelab` | `homelab-monitor-b` | Per-container name prefix (the 13 `container_name:` values). **Must be set equal to `HM_INSTANCE`** for a clean second instance, so containers become `homelab-monitor-b-monitor`, `homelab-monitor-b-vm`, etc. (A separate var exists because A's live containers use prefix `homelab` while its project/volumes use `homelab-monitor` — the two cannot be reproduced by one variable.) |
| `HOMELAB_MONITOR_DOCKER_ENABLED` | `true` | `false` | App-level flag (consumed by the monitor, not compose). Disables the Docker plugin entirely on B. |
| `COMPOSE_PROFILES` | (unset) | empty (`COMPOSE_PROFILES=`) | Gates the `host-collectors` profile (cadvisor + vector). **A MUST set `host-collectors`**; **B leaves it empty** so those two sidecars are omitted. |
| `HM_CRON_IPC_SRC` | `/var/lib/homelab-monitor/cron-apply` | B-private dir | Host **source** of the RW cron-apply IPC mount. **The dangerous one** — if left at the default, B would RW-mount A's live IPC dir. |
| `HM_CRON_SNAPSHOT_SRC` | `/var/lib/homelab-monitor/crontab-snapshot` | B-private dir | Host source of the read-only crontab-snapshot mount. |
| `HM_HOST_ETC` | `/etc` | B-private empty dir | Host source of the read-only `/host/etc` mount. Point B at an empty dir so it does not mount A's host `/etc`. |
| `HM_HOST_PROC` | `/proc` | B-private empty dir | Host source of the read-only `/host/proc` mount. |
| `HOMELAB_MONITOR_OVERRIDES_DIR` | `/var/lib/homelab-monitor/overrides` | B-private dir | Host source of the read-only `/config` (docker probe overrides) mount. |
| `HM_CRON_HOST_UID` | `1000` | runtime uid (`995` on this host) | Runtime uid the monitor container runs as; `config-init` chowns the data volumes to this uid. **Must match the uid you own the B-private dirs as.** |
| `HM_CRON_HOST_GID` | `1000` | runtime gid (`995` on this host) | Runtime gid (paired with `HM_CRON_HOST_UID`). |
| `HOMELAB_MONITOR_PORT` | `9090` | `39090` | Host-published backend port for B (A uses `29090`). |
| `HOMELAB_MONITOR_BIND_HOST` | `127.0.0.1` | `0.0.0.0` (LAN) or `127.0.0.1` (loopback) | Bind host for the backend port. `0.0.0.0` exposes B on the LAN; `127.0.0.1` keeps it loopback-only. |
| `HM_UDM_SYSLOG_PORT` | `5514` | distinct (e.g. `35514`) | UDM syslog UDP port. Only relevant if `host-collectors` is enabled; set a distinct value as belt-and-suspenders so B never collides with A's `5514`. |
| `HM_UDM_SYSLOG_BIND_HOST` | `0.0.0.0` | `127.0.0.1` | B has no real UDM feed; bind loopback. |
| `HOMELAB_MONITOR_DB_URL` | `sqlite+aiosqlite:////data/homelab-monitor.db` | leave default | The DB lives on B's own namespaced `data_monitor` volume, so the default in-container path is already isolated. |
| `HOMELAB_MONITOR_MASTER_KEY` | (none) | **freshly generated** | 32-byte base64 master key. **Do not reuse A's.** |

> **Why `COMPOSE_PROFILES` is required on A:** Compose profiles have no
> "default-on but omittable" mode. `cadvisor` and `vector` are behind the
> `host-collectors` profile, so they start **only** when that profile is active.
> If A's `.env` does not set `COMPOSE_PROFILES=host-collectors`, those two
> sidecars silently stop on A's next `up`/restart.

---

## 3. Step-by-step bring-up of instance B

The procedure below is the one that was validated live (two instances running
concurrently). Run it from a separate clone so amended/rebased commits on the
feature branch don't require force-push.

### 3.1 Clone to a sibling directory

```bash
git clone /storage/programs/homelab-monitor /storage/programs/homelab-monitor-b
cd /storage/programs/homelab-monitor-b
git checkout feat/parallel-instance
```

`.env` files are gitignored and are **not** cloned — you create B's `.env`
fresh in the next step.

### 3.2 Create B's `.env`

Create `/storage/programs/homelab-monitor-b/deploy/compose/.env`. Generate a
fresh master key first:

```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Then write the file (complete example block — adjust uid/gid `995` to your
host's runtime uid):

```dotenv
# --- stack identity ---
HM_INSTANCE=homelab-monitor-b
HM_CONTAINER_PREFIX=homelab-monitor-b

# --- host-collectors profile: empty on B (omit cadvisor + vector) ---
COMPOSE_PROFILES=

# --- app-level docker plugin: OFF on B ---
HOMELAB_MONITOR_DOCKER_ENABLED=false

# --- ports ---
HOMELAB_MONITOR_PORT=39090
HOMELAB_MONITOR_BIND_HOST=0.0.0.0          # or 127.0.0.1 for loopback only
HM_UDM_SYSLOG_PORT=35514
HM_UDM_SYSLOG_BIND_HOST=127.0.0.1

# --- runtime uid/gid (match the owner of the B-private dirs below) ---
HM_CRON_HOST_UID=995
HM_CRON_HOST_GID=995

# --- B-private bind-mount SOURCES (never A's live paths) ---
HM_CRON_IPC_SRC=/var/lib/homelab-monitor-b/cron-apply
HM_CRON_SNAPSHOT_SRC=/var/lib/homelab-monitor-b/crontab-snapshot
HM_HOST_ETC=/var/lib/homelab-monitor-b/empty-etc
HM_HOST_PROC=/var/lib/homelab-monitor-b/empty-etc
HOMELAB_MONITOR_OVERRIDES_DIR=/var/lib/homelab-monitor-b/overrides

# --- DB stays on B's own namespaced volume (default in-container path) ---
HOMELAB_MONITOR_DB_URL=sqlite+aiosqlite:////data/homelab-monitor.db

# --- fresh master key (NOT A's) ---
HOMELAB_MONITOR_MASTER_KEY=<paste the freshly generated 32-byte base64 key>
```

Lock the file down:

```bash
chmod 600 /storage/programs/homelab-monitor-b/deploy/compose/.env
```

### 3.3 Create the B-private host directories

These are the bind-mount sources referenced in B's `.env`. They must be owned
by the runtime uid/gid (`995:995` on this host) so the container can read/write
them. The cron-apply IPC dir needs `requests/` and `results/` subdirs; the cron
discovery sources can be empty (the app no-ops on empty cron sources).

```bash
sudo mkdir -p \
  /var/lib/homelab-monitor-b/overrides \
  /var/lib/homelab-monitor-b/cron-apply/requests \
  /var/lib/homelab-monitor-b/cron-apply/results \
  /var/lib/homelab-monitor-b/crontab-snapshot \
  /var/lib/homelab-monitor-b/empty-etc
sudo chown -R 995:995 /var/lib/homelab-monitor-b
```

> The `data_monitor` (`/data`) volume itself is auto-chowned to the runtime uid
> by the `config-init` one-shot at startup — you do not chown the volume by
> hand. (This fix is what makes a fresh instance work; see §5.)

### 3.4 Build B's image locally from the branch

B runs an image built **from the feature branch**, not a pulled `:latest`. Point
the image name at B-specific values so it never clashes with A's image, e.g.:

```bash
export GITHUB_REPOSITORY=homelab-monitor-local-b
export IMAGE_TAG=dev
```

These feed the compose `image:` line:
`ghcr.io/${GITHUB_REPOSITORY:-jakekausler/homelab-monitor}:${IMAGE_TAG:-latest}`.

**Gotcha (real, do not skip):** a fresh clone does not contain the generated
OpenAPI TypeScript types (`apps/ui/src/api/schema.ts` is gitignored) or the
built UI, and `docker compose build` needs them. From the clone root, run the
UI prep before building the image:

```bash
cd /storage/programs/homelab-monitor-b
pnpm install
pnpm --filter ui run generate-types
pnpm --filter ui run build
```

Then build the monitor image (same `GITHUB_REPOSITORY`/`IMAGE_TAG` exported
above must be in scope):

```bash
cd /storage/programs/homelab-monitor-b/deploy/compose
docker compose --env-file .env -f docker-compose.yml build monitor
```

### 3.5 Safety assertion, then bring up

**Always assert the rendered project name before `up`.** If B's `.env` is not
loaded, the project name falls back to `homelab-monitor` and B would stomp A:

```bash
cd /storage/programs/homelab-monitor-b/deploy/compose
docker compose --env-file .env -f docker-compose.yml config | grep '^name:'
# MUST print:  name: homelab-monitor-b
# If it prints "homelab-monitor", ABORT — the .env was not loaded.
```

Only when the assertion passes:

```bash
docker compose --env-file .env -f docker-compose.yml up -d
```

Verify after `up`:

- `config-init` completed (`service_completed_successfully`) — otherwise the
  monitor will not start.
- All containers are named `homelab-monitor-b-*`, on `homelab-monitor-b-net`,
  with `homelab-monitor-b_*` volumes; **no `cadvisor` / `vector`** (profile
  excluded).
- `docker inspect` shows B's cron-apply mount source is the B-private dir,
  **not** `/var/lib/homelab-monitor/cron-apply`.
- Instance A is undisturbed (its containers healthy, `homelab-monitor_*`
  volumes intact).

---

## 4. Create the admin user on B

B starts with a fresh, empty SQLite DB. Create the first admin user via the
`hm user create` CLI inside B's monitor container. The password prompt is
interactive, so pipe it on stdin (the same password twice). Minimum password
length is **12 characters**:

```bash
printf 'change-me-please-now\nchange-me-please-now\n' \
  | docker exec -i homelab-monitor-b-monitor hm user create admin
```

Then log in at `http://<host>:39090` (or `http://127.0.0.1:39090` if you bound
loopback) as `admin`.

---

## 5. Migrating instance A to the new compose

The parallel-instance changes are backward-compatible **except** for the
`host-collectors` profile. To keep A's `cadvisor` + `vector` running after
pulling these changes, A's `deploy/compose/.env` **must** add:

```dotenv
COMPOSE_PROFILES=host-collectors
```

If you skip this, `cadvisor` and `vector` silently stop on A's next
`up`/restart (they are now behind the profile). After adding the line, restart
A's stack:

```bash
cd /storage/programs/homelab-monitor/deploy/compose
docker compose --env-file .env -f docker-compose.yml up -d
```

All other A identity (project name, container names, network, volumes, mount
sources) is byte-identical under the defaults, so no other `.env` change is
needed.

> **`config-init` /data fix (applies to any fresh instance, including a fresh
> A):** the `config-init` one-shot now also `chown`s `/data` and the backup
> dir to `${HM_CRON_HOST_UID}:${HM_CRON_HOST_GID}` and `chmod 755`s them. A
> fresh `data_monitor` volume is seeded `1000:1000` by the image, which a
> non-default runtime uid (e.g. `995`) cannot write — that caused a SQLite
> "unable to open database file" error before the fix. With it, fresh volumes
> are auto-chowned to the runtime uid at startup; no manual step is required.

---

## 6. Teardown of instance B

Stop B (preserve its volumes):

```bash
cd /storage/programs/homelab-monitor-b/deploy/compose
docker compose --env-file .env -f docker-compose.yml down
```

Stop B **and drop its volumes** (full wipe, including the SQLite DB):

```bash
docker compose --env-file .env -f docker-compose.yml down -v
```

The B-private host directories under `/var/lib/homelab-monitor-b/` are bind
mounts (not docker volumes), so `-v` does **not** remove them. Delete them
manually if you want a clean slate:

```bash
sudo rm -rf /var/lib/homelab-monitor-b
```

Because every B identity is namespaced (`homelab-monitor-b*`), teardown never
touches instance A.

---

## 7. Known limitations

- **`/var/run/docker.sock` is still mounted into B's monitor container.** The
  socket mount line is a shared literal (not parameterized). It is **unused**
  when `HOMELAB_MONITOR_DOCKER_ENABLED=false` (the monitor never constructs a
  socket client), so sharing it is harmless — but it is present. Omitting it for
  B would require a host-specific compose override; there is no functional
  impact, so it is left as-is.
- **Host integration is A-only and not instance-aware.** The cron snapshot/apply
  executors, the systemd units, and the `homelab-monitor` host user/group are a
  host singleton owned by instance A. `host-setup.sh` is **not** instance-aware
  and should not be re-run for B. B simply points its cron/etc/proc mount
  sources at empty B-private dirs and runs cron disabled.
```
