# homelab-monitor — local development environment

This document covers the centralized dev-rig tooling introduced in STAGE-001-021 Spec B. For project-wide guidance and the master workflow, see `CLAUDE.md`.

## TL;DR

```bash
# First-time setup (creates deploy/dev/dev.env from the example):
make dev          # immediately fails with a "set master key" error

# Generate a master key and paste it into deploy/dev/dev.env:
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"

# Then re-run:
make dev          # hybrid: docker sidecars + host backend + host UI
```

Login defaults: `admin` / `admin-dev-password` (override via `HM_DEV_ADMIN_*` in `dev.env`).

### Build-context mounts (local-build images only)

If your `build-sources.yaml` defines `build_context_roots`, run once (and re-run whenever
you edit `build-sources.yaml`):

```bash
make generate-build-mounts
```

This writes `deploy/compose/docker-compose.override.yml`, which docker compose auto-loads
(same directory as `docker-compose.yml`). Without this file, `docker compose build` inside
the monitor container cannot resolve host build-context paths.

## When to use each mode

| Command | Mode | What it does | When to use |
|---|---|---|---|
| `make dev` | hybrid | Docker sidecars + host `uv run uvicorn` + host `pnpm dev` | Day-to-day frontend / backend coding. Fastest reload. |
| `make dev-clean` | hybrid (after wipe) | Kill all existing rig processes + `docker compose down -v`, then `make dev` | When the DB or sidecar volumes are corrupted, or to start from a known-good empty state. |
| `make dev-prod` | prod | `docker compose up --build` of the full prod stack (monitor built from local Dockerfile) | Before merging Dockerfile, alembic, or compose changes. **This is the binding acceptance test for any deploy-touching stage.** |
| `make dev-down` | tear-down | Stop whatever is running, preserve volumes | When you're done for the day; preserves your dev DB. |

## Port map

All dev sidecar ports bind to `127.0.0.1` only and are published by the dev-only override `deploy/dev/docker-compose.dev.yml` (see "How dev sidecar ports get published" below). Override any port in `deploy/dev/dev.env` via the `HM_DEV_*_PORT` vars.

| Service | Host port | Container port | Notes |
|---|---|---|---|
| UI dev server (Vite) | 5180 | n/a | Hybrid mode only. Prod mode serves UI from the monitor itself on `:9090`. |
| Backend (uvicorn) | 19090 | 9090 (in prod container) | Host-port 9090 is taken by something else on the canonical dev host; hybrid mode uses 19090. Prod mode publishes container 9090 → host 9090 (override via `HOMELAB_MONITOR_PORT` in `dev.env`). |
| VictoriaMetrics | 8428 | 8428 | |
| VictoriaLogs | 9428 | 9428 | Prod compose normally does NOT expose this; the dev rig publishes it for host-backend access. |
| vmagent | 8429 | 8429 | Internal scrape coordinator. |
| Alertmanager | 9093 | 9093 | |
| Karma | 8081 | 8081 | UI for Alertmanager. Embedded in `/alerts` via reverse proxy. |
| kthxbye | (none) | n/a | Internal-only silence-extender. |
| vmalert (metrics) | 8880 | 8880 | |
| vmalert (logs) | 8881 | 8880 | Distinct host port to avoid collision with vmalert-metrics. |
| Vector | 8686 | 8686 | Log forwarder; admin port. |
| Grafana | 3000 | 3000 | Embedded in `/metrics` via reverse proxy. |
| fixture-host | 8000 | 8000 | Test fixture (only in test rig). |
| noisy-logger | 8001 | 8001 | Test fixture (only in test rig). |

### How dev sidecar ports get published

The prod compose file (`deploy/compose/docker-compose.yml`) intentionally
publishes **no** sidecar host ports — in production every sidecar is reached
through the monitor's `/api/<sidecar>/` reverse proxy (the port-map invariant:
prod publishes only the monitor backend on `29090`).

In **hybrid** dev mode the backend runs on the host and must reach the docker
sidecars directly, so `scripts/dev-up.sh` layers a dev-only override file,
`deploy/dev/docker-compose.dev.yml`, via a second `-f` flag:

    docker compose -f deploy/compose/docker-compose.yml \
                   -f deploy/dev/docker-compose.dev.yml up -d <sidecars...>

That override is the ONLY thing that adds `127.0.0.1:1xxxx:cccc` `ports:`
mappings, and it is loaded ONLY by `make dev` / `make dev-clean` (hybrid mode).
`make dev-prod` and real production never load it, so prod sidecars stay
container-internal. The host-port values default to the CLAUDE.md port-map
table and are overridable via the `HM_DEV_*_PORT` vars in `deploy/dev/dev.env`.

## Master key generation

```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Paste the output as the `HOMELAB_MONITOR_MASTER_KEY` value in `deploy/dev/dev.env`. The dev-up script aborts if it sees the placeholder `GENERATE_ME_WITH_SCRIPT`.

The `deploy/dev/dev.env` file is `chmod 600` (the script enforces this on every run) and gitignored. **Do not commit it.**

## First-boot user creation

The dev-up script runs `hm user create` automatically on every invocation. The command is idempotent: if the user already exists, the error is swallowed.

`hm user create` is interactive (prompts twice for password). The script pipes via:

```bash
printf '%s\n%s\n' "$PW" "$PW" | uv run hm user create "$USER"
```

Min password length: **12 chars**. The default dev password (`admin-dev-password`) is 18 chars.

## Hybrid vs prod mode — what's different

**Hybrid mode (`make dev`):**

- Backend runs on the host (`uv run uvicorn`) — fastest iteration.
- UI runs on the host (`pnpm dev`) — HMR works.
- Sidecars run in docker (VM, VL, AM, Karma, etc.).
- Host backend talks to sidecars via `127.0.0.1:<host-port>` (URLs come from `dev.env`).
- vmagent in docker scrapes the host backend via `host.docker.internal:19090` (Linux requires `extra_hosts: host.docker.internal:host-gateway` — see "Sidecar visibility" below).

**Prod mode (`make dev-prod`):**

- All services run in docker (including the monitor, built from `apps/monitor/Dockerfile`).
- Monitor talks to sidecars via docker DNS names (`http://victoriametrics:8428` etc.).
- The monitor serves the UI bundle from `apps/ui/dist` itself — no separate Vite server.
- This is the binding acceptance path: if `make dev-prod` doesn't work end-to-end, the deploy is broken.

## Troubleshooting

### Port collision

The script warns but does NOT abort if a port is taken. To detect collisions yourself:

```bash
ss -ltn 'sport = :9093'   # Alertmanager
lsof -nP -iTCP:9093 -sTCP:LISTEN
```

To override: edit the corresponding `HM_DEV_*_PORT` var in `deploy/dev/dev.env` and restart with `make dev-clean`.

### Backend won't start

```bash
tail -200 deploy/dev/logs/backend.log
```

Common causes:

- Master key not set (script should have caught this; double-check).
- DB schema mismatch — run `make dev-clean` to wipe.
- Port 19090 already taken by an earlier rig that wasn't torn down — `make dev-down` then re-run.

### UI loads but API calls return HTML / React error #31

Symptom: `Cannot read properties of undefined (reading 'data')` or React error #31 ("object with keys {code, message, details}").

Cause: vite proxy not configured. The vite proxy env var is `VITE_API_PROXY_TARGET` — NOT `API_PROXY_TARGET`. If you launched the UI by hand, check the env.

### Karma iframe is blank

Cause: most likely the backend's same-origin check is rejecting because of mismatched origins. The dev-up script sets `VITE_DEV_HOST=127.0.0.1` deliberately so the browser origin (`http://127.0.0.1:5180`) matches the backend's allowed-origin set. If you change the host, you may need to update the backend's CORS/origin policy or run `make dev-clean`.

For the full STAGE-019 lessons-learned on Karma proxying, see `epics/EPIC-001-foundation/STAGE-001-019.md`.

### Accessing the dev rig from a LAN device (e.g. mobile browser)

By default the dev rig binds the backend and UI to `127.0.0.1` (localhost only).
To expose both to other devices on the same local network:

1. Set `HM_DEV_BIND_HOST=0.0.0.0` in `deploy/dev/dev.env`.
2. Restart the rig:
   ```bash
   make dev-down
   make dev
   ```
3. Access the UI from any device on the same network:
   ```
   http://<host-LAN-IP>:5180
   ```
   Find your host's LAN IP with `ip route get 1` or `hostname -I`.

**Security caveat:** The dev rig has no TLS and uses a shared dev password.
Only enable `0.0.0.0` binding on trusted private networks. Revert to
`HM_DEV_BIND_HOST=127.0.0.1` (or remove the line — it is the default) when
done.

Note: The Vite→backend proxy still routes over loopback internally — only the
listen address changes. Sidecar ports (Alertmanager, Karma, Grafana, etc.)
remain bound to `127.0.0.1` via the docker-compose dev override and are NOT
exposed to the LAN by this setting.

### Grafana iframe is blank or shows "Origin not allowed"

Cause: `GF_SERVER_ROOT_URL` mismatch with the URL the browser is actually using. The dev rig sets `GRAFANA_PUBLIC_URL=http://127.0.0.1:9090/api/grafana/` for prod mode and `http://127.0.0.1:3000/` direct in hybrid mode (Grafana is direct on its own port in hybrid mode — the monitor proxy still works but the iframe URL changes).

For the full STAGE-019/020 lessons-learned on Grafana sub-path serving, see `epics/EPIC-001-foundation/STAGE-001-020.md`.

### Sidecar visibility into host backend (vmagent, hybrid mode only)

In hybrid mode, vmagent runs in docker but the backend runs on the host. vmagent must reach `host.docker.internal:19090` to scrape `/metrics`. On Linux, docker does NOT auto-resolve `host.docker.internal`; the prod compose `vmagent` service may need `extra_hosts: ["host.docker.internal:host-gateway"]`.

If vmagent is failing to scrape the host backend (visible as missing `homelab_monitor_*` metrics in `/api/metrics/query`), edit `deploy/compose/docker-compose.yml` and add:

```yaml
  vmagent:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Then reflect the change in `deploy/vmagent/scrape.yaml` so it scrapes `host.docker.internal:19090` instead of `monitor:9090`. **This is a known follow-up; do not block on it for Spec B unless integration tests fail.**

### "deploy/dev/dev.env: permission denied"

The script enforces `chmod 600`. If you're running as a different user than the one who created the file, recreate it:

```bash
rm deploy/dev/dev.env
make dev   # will recopy from example
# then edit the master key and re-run
```

### Manual fallback (when scripts fail)

If `make dev` fails for unknown reasons, fall back to the manual pattern in `CLAUDE.md` (Local Refinement section, "Manual fallback" subsection). That recipe sources from `/tmp/hm-refine/.env` and starts each process by hand.

## Files this stage introduced

- `deploy/dev/dev.env.example` — template (committed)
- `deploy/dev/dev.env` — your local copy (gitignored)
- `deploy/dev/logs/` — host-process logs (gitignored)
- `scripts/dev-up.sh` — launcher
- `scripts/dev-down.sh` — tear-down
- `Makefile` targets `dev`, `dev-clean`, `dev-prod`, `dev-down`

## See also

- `CLAUDE.md` — Local Refinement section
- `epics/EPIC-001-foundation/STAGE-001-019.md` — Karma proxy lessons-learned
- `epics/EPIC-001-foundation/STAGE-001-020.md` — Grafana sub-path lessons-learned
- `epics/EPIC-001-foundation/STAGE-001-021.md` — this stage's tracking doc
- `deploy/compose/.env.example` — prod-compose env template
