# Installing the cron heartbeat wrapper (remote host)

> Last updated: 2026-05-18 (STAGE-002-009 — wrapper install path)

## Overview

For hosts the monitor cannot reach (Synology, NAS, air-gapped networks), use the standalone remote CLI to install the wrapper directly. The CLI runs on the target host, contacts the monitor via the API, and installs the wrapper without requiring the monitor to have inbound access.

**Contrast with local install:** the local install uses the monitor's UI (or
`hm cron install-wrapper`) and routes every privileged write through the
host-side cron-apply executor that `scripts/host-setup.sh` installs. The remote
install uses a self-contained Python CLI that runs on the target host, performs
its own crontab writes with its own snapshot + rollback, and needs nothing
installed on the host beyond `python3`.

---

## Prerequisites

### Python 3.7+

The remote CLI requires Python 3.7+ and only the Python standard library (no external dependencies).

```bash
python3 --version
# Python 3.7.x or later
```

### Monitor accessibility

The target host must be able to reach the monitor at the monitor's public URL (the same `HOMELAB_MONITOR_PUBLIC_URL` configured on the monitor host).

```bash
# Test from the target host:
curl -v http://homelab-monitor-url/api/healthz
```

### Heartbeat token (scoped to heartbeat:write)

The remote CLI requires an API token with `heartbeat:write` scope. Generate one on the monitor:

```bash
# On the monitor host:
hm api-token create --scope heartbeat:write
```

This outputs a token. Save it securely (it's a secret).

---

## Getting the remote CLI

The remote CLI is a single Python script:
`apps/monitor/homelab_monitor/cli/install_wrapper_remote.py` in the
homelab-monitor repo. It imports **only the Python standard library** — no
`homelab_monitor.kernel.*` imports — so it runs on any host with just
`python3` and no project install. (EPIC-019 will ship a PyInstaller-built
single-file binary.)

It does **not** embed a copy of the wrapper template. At runtime it fetches
the canonical template from the monitor (see "How the CLI builds the wrapper"
below), so the wrapper it produces is byte-identical to the server-side
installer's output.

### Option A: Clone the repo

```bash
git clone https://github.com/jakekausler/homelab-monitor.git
cd homelab-monitor
python3 apps/monitor/homelab_monitor/cli/install_wrapper_remote.py --help
```

### Option B: Copy the script

Copy `apps/monitor/homelab_monitor/cli/install_wrapper_remote.py` to the target
host via scp, rsync, or USB. The single file is fully self-contained.

---

## Installation (dry-run, then confirm)

The CLI works directly against the **local** crontab files on the target host —
it does not query the monitor for a list of discovered crons. You point it at a
crontab and a line; it parses that line itself, computes the fingerprint, and
installs the wrapper.

```bash
# 1. Dry-run preview (NO changes — omit --confirm):
HM_MONITOR_URL=http://homelab-monitor-url \
HM_HEARTBEAT_TOKEN=<token-from-above> \
python3 install_wrapper_remote.py

# 2. Same command + --confirm to actually apply:
HM_MONITOR_URL=http://homelab-monitor-url \
HM_HEARTBEAT_TOKEN=<token-from-above> \
sudo python3 install_wrapper_remote.py --confirm
```

With no `--crontab` / `--line` flags the CLI prompts interactively: it lists
`/etc/crontab` and every file under `/var/spool/cron/crontabs/`, then lists the
job lines in the chosen crontab for you to pick by 1-indexed number.

The CLI will:

1. **Resolve the crontab** — from `--crontab` (`/etc/crontab` or
   `crontab:<user>` or an explicit path) or the interactive prompt.
2. **Parse the chosen line** — extract `(schedule, command)`, mirroring the
   kernel's `cron_parser.py` (USER_CRONTAB vs SYSTEM_WITH_USER_FIELD).
3. **Fetch the wrapper template** from the monitor and substitute placeholders.
4. **Dry-run preview** (no `--confirm`) — print the wrapper script, the
   crontab `- old / + new` diff, and the registration payload, then exit.
5. **Apply** (`--confirm`) — write the wrapper + token, rewrite the crontab
   line, and register with the monitor.

---

## How the CLI builds the wrapper (template fetch)

The remote CLI does **not** carry an embedded copy of the wrapper template.
It fetches the canonical template at runtime:

```
GET {HM_MONITOR_URL}/api/crons/wrapper-template
Authorization: Bearer {HM_HEARTBEAT_TOKEN}
```

The endpoint returns the raw `cron-with-heartbeat.sh.tmpl` text (`text/plain`).
It requires the same `heartbeat:write`-scoped token the CLI already holds — the
template itself carries no secret, the auth is for uniform API access.

The CLI then substitutes the four placeholders itself —
`{{FINGERPRINT}}`, `{{HEARTBEAT_URL_BASE}}`, `{{TOKEN_FILE_PATH}}`,
`{{INSTALL_DATE}}` — in the same order and with the same values as the
server-side installer (`kernel/cron/install.py:_build_wrapper_content`). The
produced wrapper script is therefore byte-identical to what the local UI/CLI
install path would write. If the template fetch fails (monitor unreachable,
non-2xx, bad token), the CLI aborts before touching the host.

---

## Environment variables

| Variable              | Required? | Description |
|-----------------------|-----------|-------------|
| `HM_MONITOR_URL`      | Yes       | Base URL of the monitor (e.g., `http://127.0.0.1:29090` or `https://homelab.example.com`). Overridable with `--monitor-url`. |
| `HM_HEARTBEAT_TOKEN`  | Yes       | API token with `heartbeat:write` scope. Overridable with `--token`. |

Other inputs are CLI flags only (no env var): `--crontab`, `--line`, `--host`
(defaults to `socket.gethostname()`), `--confirm`.

### Example with environment file

```bash
cat > /tmp/hm-env <<EOF
export HM_MONITOR_URL=http://homelab.local:29090
export HM_HEARTBEAT_TOKEN=hm_heartbeat_1234567890abcdef1234567890abcdef
EOF

source /tmp/hm-env
python3 install_wrapper_remote.py --crontab crontab:root --line 1
```

---

## Interactive flow

```
$ HM_MONITOR_URL=http://homelab-monitor HM_HEARTBEAT_TOKEN=<token> \
    python3 install_wrapper_remote.py

Available crontabs:
  1. /etc/crontab
  crontab:root
Enter crontab (e.g., crontab:root): crontab:root

Crontab lines:
  1. 0 2 * * * /backup.sh
  2. 0 3 * * * /cleanup.sh
Select line (1-indexed): 1

=== Wrapper script ===
#!/bin/sh
# DO NOT EDIT — generated by `hm cron install-wrapper` ...
...

=== Crontab diff ===
File: crontab:root
- 0 2 * * * /backup.sh
+ 0 2 * * * /usr/local/bin/cron-with-heartbeat.sh -- /backup.sh

=== Registration payload ===
{
  "host": "nas-backup",
  "source_path": "crontab:root",
  "schedule": "0 2 * * *",
  "command": "/backup.sh",
  "wrapper": true
}
```

Re-run the exact same command with `--confirm` (and, for a user/system
crontab, `sudo`) to apply it.

---

## Wrapper contract (same as local install)

The wrapper is installed at the fixed path
`/usr/local/bin/cron-with-heartbeat.sh` and posts up to three events to the
monitor's heartbeat receiver:

1. `POST /api/hb/{fingerprint}/start` — before the command runs
2. `POST /api/hb/{fingerprint}/ok?duration=<seconds>` — on success
3. `POST /api/hb/{fingerprint}/fail?duration=<seconds>&exit_code=<N>` — on failure

`duration` is whole seconds. Each POST carries an `Authorization: Bearer`
header with the token read from the token file, and is best-effort (a network
error never blocks the real command). After installing, the CLI also makes one
best-effort `POST /api/hb/{fingerprint}/register` call so the cron row appears
in the registry immediately. See `docs/cron/install-heartbeat.md` for the full
wrapper contract.

---

## Security notes

### Token scoping

The `heartbeat:write` token is scoped narrowly — it can only POST heartbeats, not read crons or modify other resources. Treat it like a password.

### No remote write access required

Unlike the local install, the remote CLI does NOT require the monitor to have write access to the host's crontab files. The CLI runs locally and modifies the crontab directly.

### Token file placement

The CLI writes a single shared bearer token to
`/etc/homelab-monitor/heartbeat.token` with mode **`0644`**. It is
world-readable on purpose: the wrapper is invoked by the host cron daemon as
the crontab's owning user, which is often not root — a `0600` token would be
unreadable and the wrapper would send unauthenticated heartbeats. The token is
scoped to `heartbeat:write` only, so its blast radius if read by another local
user is limited to posting heartbeats. The token is never passed on the cron
command line, so it does not appear in `ps` output. On a single-user homelab
this world-readable token is an accepted trade-off (the same trade-off the
local install path makes).

---

## Rollback

The remote CLI implements its **own** snapshot + rollback — it does not use the
host-side cron-apply executor (that executor exists only on hosts that run the
monitor stack; the whole point of the remote CLI is foreign hosts that do not).

During `--confirm`, the CLI applies its mutations in order — write wrapper
script, write token, rewrite the crontab line **last** — recording an undo
action for each. If any step fails, every applied mutation is undone in
reverse: a file that did not pre-exist is deleted, a file that did pre-exist is
restored from an in-memory snapshot, and the crontab is restored byte-exact via
the same atomic temp-file + rename used for the forward write. A failed install
therefore leaves the host unchanged ("Rollback complete; host left unchanged.").
The best-effort `/register` call happens after the crontab rewrite and is **not**
rolled back.

To remove a *successfully installed* wrapper, edit the crontab line back to its
original (unwrapped) form by hand:

```bash
# On the target host, as the crontab's owner:
crontab -e
# Delete the "/usr/local/bin/cron-with-heartbeat.sh -- " prefix from the line.
# Save and exit.
```

The shared wrapper script and token file can stay in place (other wrapped
crons on the host use them). The discoverer unwraps a wrapped command before
fingerprinting, so the registry row's identity is unaffected by wrapping or
un-wrapping.

---

## Troubleshooting

### "Connection refused" / "Cannot reach monitor"

**Symptom:** `HM_MONITOR_URL=http://homelab-monitor python3 install_wrapper_remote.py` fails with a network error.

**Fix:** verify the monitor URL is reachable from the target host:

```bash
curl -v http://homelab-monitor/api/healthz
```

### "Invalid token"

**Symptom:** CLI reports 401 Unauthorized.

**Fix:** verify the token was generated with `--scope heartbeat:write`:

```bash
# On the monitor host:
hm api-token create --scope heartbeat:write
```

And that you're using the correct value:

```bash
echo "Token: $HM_HEARTBEAT_TOKEN"
```

### "no crontab lines found" / "cannot parse crontab line"

**Symptom:** the CLI reads the crontab but finds no job lines, or cannot parse
the line you selected.

**Cause:** the CLI parses the **local** crontab file directly (it does not
query the monitor for discovered crons). An empty crontab, a crontab with only
comments/env-vars, or a malformed schedule produces this error.

**Fix:** verify the crontab actually contains the job you expect:

```bash
crontab -l -u root          # or cat /etc/crontab
```

The `host` recorded on the registry row defaults to
`socket.gethostname()`; override it with `--host <name>` if the monitor knows
this host under a different name.

### "Permission denied" writing crontab

**Symptom:** install (`--confirm`) fails with a permission error.

**Fix:** the CLI must run as root (or the crontab owner) to rewrite a crontab
and to write `/usr/local/bin/cron-with-heartbeat.sh` +
`/etc/homelab-monitor/heartbeat.token`. Use `sudo`:

```bash
sudo HM_MONITOR_URL=http://homelab-monitor HM_HEARTBEAT_TOKEN=<token> \
  python3 install_wrapper_remote.py --crontab crontab:root --line 1 --confirm
```

---

## Advanced: non-interactive install

To script the remote install without the interactive prompts, pass every input
as a flag:

```bash
sudo HM_MONITOR_URL=http://homelab-monitor HM_HEARTBEAT_TOKEN=<token> \
  python3 install_wrapper_remote.py \
    --crontab crontab:root \
    --line 1 \
    --host nas-backup \
    --confirm
```

`--line` is the 1-indexed position among the crontab's non-comment job lines.
Omit `--confirm` to get the dry-run preview for that exact line.

---

## Related docs

- `docs/cron/install-heartbeat.md` — local install (for hosts the monitor can reach)
- `docs/architecture/cron-identity.md` — fingerprint & identity semantics
- `docs/architecture/cron-logscrape.md` — observing cron via journald logs
