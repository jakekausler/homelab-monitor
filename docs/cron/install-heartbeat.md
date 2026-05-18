# Installing the cron heartbeat wrapper (local host)

> Last updated: 2026-05-18 (STAGE-002-009 — host-side-executor wrapper install)

## Overview

The heartbeat wrapper is a small POSIX shell script that wraps a host cron
command so it reports execution status back to the monitor. This walkthrough
covers the local installation path — for hosts the monitor cannot reach
(Synology, NAS, air-gapped boxes), see `docs/cron/remote-install.md`.

The monitor detects local crons via discovery (it reads `/etc/crontab`,
`/etc/cron.d/*`, and a root-generated snapshot of per-user crontabs). For each
local cron you can install the wrapper from the monitor UI:

1. **Dry-run preview**: see exactly what command rewrite will happen — no
   changes are made.
2. **Confirm and install**: the request is handed to the host-side executor,
   which performs all privileged writes atomically; heartbeat events then flow
   back to the monitor on every cron run.

### The privilege boundary (read this first)

The monitor **container has zero host-write capability**. It cannot rewrite a
crontab, cannot write the wrapper script, cannot write the token file. All it
can do is drop a *request file* into an IPC directory. A separate root-owned
systemd service on the host — the **cron-apply executor** — picks the request
up and performs every privileged write, with a snapshot + rollback around the
whole operation.

This is the single, auditable privilege boundary: the container's only
read-write host mount is the IPC directory `/var/lib/homelab-monitor/cron-apply`.

---

## Prerequisites

### Host setup (one-time)

Before installing any wrappers, run the host setup script on the host:

```bash
sudo bash /path/to/homelab-monitor/scripts/host-setup.sh
```

This script (see `docs/deploy/host-prerequisites.md` for the full detail):

- Creates the `homelab-monitor` system user (no shell, low privilege).
- Sets **read-only** ACLs on `/etc/crontab` and `/etc/cron.d/` so discovery
  can read system crontabs. It does **not** ACL the user-crontab spool —
  an ACL on a `/var/spool/cron/crontabs/*` file makes vixie-cron reject the
  crontab as `INSECURE MODE`.
- Installs the **crontab-snapshot** mechanism: a root script
  (`hm-crontab-snapshot`) plus three systemd units
  (`homelab-monitor-crontab-snapshot.{service,path,timer}`) that run
  `crontab -l -u <user>` and write a world-readable snapshot of each user's
  crontab. The discoverer reads the snapshot, never the `0600` spool files.
- Installs the **cron-apply executor**: the root script
  `hm-cron-apply` (at `/usr/local/sbin/hm-cron-apply`) plus three systemd
  units — `homelab-monitor-cron-apply.service` (the `Type=oneshot` executor),
  `homelab-monitor-cron-apply.path` (watches the IPC `requests/` directory),
  and `homelab-monitor-cron-apply.timer` (a 60-second safety-net sweep in case
  the `.path` watcher misses a filesystem event).
- Creates the IPC directory tree `/var/lib/homelab-monitor/cron-apply/{requests,results}`.
- Removes any stale `homelab-monitor-crontab-acl` units from older installs.
- Requires `jq` on the host (the executor parses request JSON with it).

The script is idempotent — running it twice is a no-op. Run it again whenever
you pull a new version of the repo so the executor / snapshot scripts and units
stay current.

### Environment configuration

Set `HOMELAB_MONITOR_PUBLIC_URL` in your compose env file (`.env` next to
`docker-compose.yml`):

```bash
# Example (dev rig):
HOMELAB_MONITOR_PUBLIC_URL=http://127.0.0.1:19090

# Example (production):
HOMELAB_MONITOR_PUBLIC_URL=http://127.0.0.1:29090

# Example (behind nginx/TLS):
HOMELAB_MONITOR_PUBLIC_URL=https://homelab.example.com
```

This variable has **no default**. It is the URL baked into the wrapper script
as the heartbeat callback base. It MUST be reachable from the host's network
context (the wrapper runs as a child of the host's cron daemon, not inside the
container). If it is unset or empty, a wrapper install fails immediately —
`get_public_url()` returns `None` and the install endpoint returns HTTP 400.

---

## Wrapper contract

The wrapper script is installed at the fixed host path
`/usr/local/bin/cron-with-heartbeat.sh`. A wrapped crontab line invokes it as:

```
/usr/local/bin/cron-with-heartbeat.sh -- <original command and args>
```

Everything after the literal `--` separator is the unchanged original command.

The wrapper performs up to three HTTP `POST` requests against the monitor's
heartbeat receiver (`URL_BASE` = `HOMELAB_MONITOR_PUBLIC_URL`):

1. **`POST {URL_BASE}/api/hb/{fingerprint}/start`** — before the command runs.
2. **`POST {URL_BASE}/api/hb/{fingerprint}/ok?duration=<seconds>`** — on
   success (command exit code 0).
3. **`POST {URL_BASE}/api/hb/{fingerprint}/fail?duration=<seconds>&exit_code=<N>`**
   — on failure (non-zero exit code).

`duration` is whole **seconds** (`END_EPOCH - START_EPOCH`), not milliseconds.
Each request carries an `Authorization: Bearer <token>` header; the token is
read at runtime from the token file (see Security model).

**Best-effort delivery:** every POST runs with `curl --max-time 5` and a
trailing `|| true` — a network error, a 5xx, or a monitor outage **never blocks
or alters the real command**. The command runs regardless.

**Exit code preservation:** the wrapper captures the command's exit code and
`exit`s with the exact same value. If your command exits 7, the wrapper exits 7.

**Stdout/stderr pass-through:** the command's output goes to stdout/stderr
normally; the wrapper does not buffer or consume it. Only the heartbeat POSTs'
own output is discarded.

A malformed invocation (missing `--` separator) makes the wrapper exit 64
without running anything.

---

## Security model

### Token file (0644, world-readable — and why)

When a wrapper is installed, the executor writes a single shared bearer token
to `/etc/homelab-monitor/heartbeat.token` with mode **`0644`**
(owner read-write, group + world read-only).

It is deliberately world-readable. The wrapper is invoked by the host cron
daemon and runs **as the crontab's owning user** — which is frequently *not*
root. A `0600` token would be unreadable by a non-root cron user, and the
wrapper would silently send unauthenticated heartbeats (the monitor would
reject them). `0644` lets any local user's cron job read the token.

The token is a single system-managed API token scoped to `heartbeat:write`
only (minted once by `ensure_heartbeat_wrapper_token` and reused across every
wrapped cron). Its blast radius if read by another local user is limited to
posting heartbeats — it cannot read crons, change config, or touch any other
resource. On a single-user homelab this is an accepted trade-off.

### Process-list token visibility

The token is read from the file, **not** passed on the command line, so it
does not appear in `ps` output or the cron `(user) CMD (...)` journal line.
A local user can still *see that a wrapped cron exists* (the wrapper path is
in the command line) and can read the token from the world-readable file —
both are accepted single-user-homelab trade-offs, not leaks of a
command-line secret.

### Hardened host-side executor

The cron-apply executor is the only thing that performs privileged writes, and
its input validation is deliberately narrow:

- It writes the wrapper script and token to **fixed paths only**
  (`/usr/local/bin/cron-with-heartbeat.sh`, `/etc/homelab-monitor/heartbeat.token`).
  The request carries `content`, never a destination path.
- For a crontab rewrite it **re-derives the wrapped line itself** from
  `old_line` + `command` (find the last occurrence of `command`, splice in the
  wrapper prefix). The `new_line` the monitor supplies is only a cross-check —
  if the executor's independently re-derived line disagrees, the request is
  rejected (`bad_request`). The executor will *only* wrap a line that already
  exists in the target crontab; it refuses arbitrary lines and refuses an
  already-wrapped line.
- The target crontab string is validated against an allow-list
  (`/etc/crontab`, `/etc/cron.d/<name>`, `crontab:<user>` — no `/` or `..` in
  the name/user component).
- The systemd unit itself is locked down (`ProtectSystem=strict`,
  `NoNewPrivileges`, `MemoryDenyWriteExecute`, an explicit `ReadWritePaths`
  allow-list, etc.) so a compromised request can touch only crontab files and
  the IPC directory.

### Snapshot + rollback

Before any write, the executor snapshots whatever it is about to overwrite
(crontab file, and an existing wrapper/token if present). The operation list
(write wrapper → write token → rewrite crontab) is applied **atomically**: if
any step fails, every applied step is undone in reverse — files that did not
pre-exist are deleted, files that did are restored from the snapshot, and the
crontab is restored byte-exact. A `status="ok"` result means every operation
succeeded.

---

## Installation flow (UI)

1. **Browse crons**: open the Crons tab in the monitor UI.
2. **Find a local cron**: each cron carries an `is_local` computed field.
   Local crons (`host` equals the monitor's own hostname) show an enabled
   **Install heartbeat wrapper** button on the cron detail page. Remote crons
   show a disabled button with an EPIC-017 tooltip ("remote wrapping ships in
   EPIC-017").
3. **Dry-run**: clicking the button opens the install modal, which POSTs to
   `/api/crons/{fingerprint}/install-wrapper` with `confirm: false`. The
   backend calls `build_install_kit` — it reads the source crontab, finds the
   line whose fingerprint matches, and returns:
   - the current crontab line (`old_line`),
   - the rewritten line (`new_line`, with the wrapper prefix spliced in),
   - the fully-substituted wrapper script content.
   Nothing is written to disk.
4. **Confirm**: the modal re-POSTs the same endpoint with `confirm: true`.
   The backend:
   - ensures the shared heartbeat token exists (mints it if absent);
   - builds a 3-operation request — write-wrapper-script, write-token,
     wrap-crontab;
   - writes the request JSON into the IPC `requests/` directory
     (`/host-ipc/requests/<uuid>.json` inside the container) and polls
     `results/` for up to 30 seconds.
   The host's `homelab-monitor-cron-apply.path` watcher (or the 60s
   `.timer` safety-net) triggers the executor, which applies the three
   operations atomically and writes a result file.
5. **Done**: on a successful result the backend re-runs discovery's upsert and
   records a `crons.wrapper_installed` audit row. The wrapper is now active;
   the next cron execution POSTs heartbeats.

### CLI path

The same install is available without the UI via
`hm cron install-wrapper <fingerprint>`:

```bash
# Dry-run preview (no changes):
make uv ARGS="--directory apps/monitor hm cron install-wrapper <fingerprint>"

# Actually install:
make uv ARGS="--directory apps/monitor hm cron install-wrapper <fingerprint> --confirm"
```

The CLI requires `HOMELAB_MONITOR_PUBLIC_URL` to be set in its environment and
goes through the **same** host-side executor and IPC path as the UI button.
`hm cron get-wrapper-template` prints the raw wrapper template to stdout.

---

## Rollback

The executor takes a crontab snapshot and rolls back automatically on any
install failure — a failed install leaves the host byte-for-byte unchanged.

To remove a *successfully installed* wrapper, edit the crontab line back to its
original (unwrapped) form. The next discovery scan re-fingerprints the
unwrapped line; because the discoverer unwraps a wrapped command before
fingerprinting (see `docs/architecture/cron-identity.md`), the fingerprint is
stable and the same registry row is retained.

```bash
# On the host, as the crontab's owner:
crontab -e        # for a user crontab
# or edit /etc/crontab / the /etc/cron.d/ file directly as root
# Delete the "/usr/local/bin/cron-with-heartbeat.sh -- " prefix; save.
```

The wrapper script and token file are shared across all wrapped crons — leave
them in place even after un-wrapping one cron.

---

## Troubleshooting

### "HOMELAB_MONITOR_PUBLIC_URL is not configured" (HTTP 400)

**Symptom:** clicking Install heartbeat wrapper (or running the CLI) fails with
an HTTP 400 / `HOMELAB_MONITOR_PUBLIC_URL is not configured`.

**Fix:** set `HOMELAB_MONITOR_PUBLIC_URL` in your compose `.env` and restart:

```bash
docker compose up -d --force-recreate monitor
```

### "cron-apply executor did not respond" (HTTP 503)

**Symptom:** install fails with a 503 — the IPC `requests/` directory is
missing, or the executor never wrote a result within 30 seconds.

**Fix:** the host-side executor is not installed or its units are not enabled.
Re-run host setup, then confirm the units:

```bash
sudo bash scripts/host-setup.sh
systemctl status homelab-monitor-cron-apply.path
systemctl status homelab-monitor-cron-apply.timer
journalctl -u homelab-monitor-cron-apply.service   # see each apply run
```

Also confirm `jq` is installed on the host — the executor requires it.

### "no crontab line matches fingerprint" (HTTP 409)

**Symptom:** install fails with 409 / `CronLineNotFoundError`.

**Cause:** the crontab line was edited (schedule or command changed) after
discovery recorded the row, so its fingerprint no longer matches any line in
the file. Trigger a fresh discovery scan (`hm cron discover`, or the
discover-now button) and retry against the new row.

### "crontab line is already wrapped" (HTTP 409)

**Symptom:** install fails with 409 / `AlreadyWrappedError`.

**Cause:** the line already begins with the wrapper prefix. The cron is
already wrapped — no action needed.

### Cron is wrapped but no heartbeats arrive

The wrapper never blocks on HTTP errors, so a wrapped cron that cannot reach
the monitor runs silently with no heartbeat recorded. Check, in order:

1. **The wrapper ran.** Inspect the cron journal:
   ```bash
   sudo journalctl -u cron --follow
   ```
2. **The host can reach the monitor** at `HOMELAB_MONITOR_PUBLIC_URL`:
   ```bash
   curl -v http://127.0.0.1:29090/api/healthz
   ```
3. **The token file exists and is readable** by the cron's owning user:
   ```bash
   ls -l /etc/homelab-monitor/heartbeat.token   # expect mode 0644
   ```

---

## Related docs

- `docs/cron/remote-install.md` — installing on hosts the monitor cannot reach
- `docs/deploy/host-prerequisites.md` — host setup, systemd units, bind-mounts
- `docs/architecture/cron-identity.md` — fingerprint & identity semantics
- `docs/architecture/cron-logscrape.md` — observing cron via journald logs
