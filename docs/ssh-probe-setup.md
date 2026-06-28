# SSH `uptime` probe setup

This guide walks an operator through setting up an SSH-based **uptime probe**
against a remote target, using the `hm ssh-probe` CLI. It covers both supported
account modes:

- **An appliance** — a network appliance / router / firewall whose vendor OS only
  exposes a single privileged account and does **not** let you create a dedicated
  low-privilege user. In config this is `account_mode: appliance`.
- **A full-OS host** — a general-purpose Linux host or NAS where you **can** create
  a dedicated low-privilege user just for the probe. In config this is
  `account_mode: dedicated-user`.

> Placeholders. Every value in this guide is a placeholder you supply yourself:
> `<target-id>`, `<host>`, `<port>`, `<user>`, `<PUBKEY>`, `<HOST_KEY_LINE>`,
> `<FINGERPRINT>`. Do not copy literal example values — generate and retrieve your
> own with the CLI commands shown.

---

## 1. Overview

An SSH probe in homelab-monitor is a **read-only, observe-only** health check.
For each run it:

- Opens a fresh SSH connection (one connection per run, via `asyncssh` — no
  long-lived session).
- **Pins the target's host key.** The connection only succeeds if the server
  presents the exact host key you pinned in config. A mismatch is treated as a
  possible man-in-the-middle (MITM) and the run fails loudly.
- Runs against a **forced command** on the target. The probe key is installed on
  the target with an OpenSSH `command="..."` restriction, so the key can only ever
  invoke that one command — never an arbitrary shell.

The framework **never writes to the target's authentication config**. You install
the public key and the forced-command restriction **by hand** on the target. The
CLI's job is to:

1. Generate and store the per-target probe key (private key stays in the secrets
   store, never printed).
2. Capture the target's host key so you can pin it.
3. Render the exact, paste-able install instructions for your account mode.
4. Verify, after you've installed by hand, that the restriction actually holds.

Once the target is declared and installed, the monitor auto-registers an
`uptime-<target-id>` collector that emits uptime metrics on a schedule. This
`uptime` probe is a deliberately trivial **exemplar** — it reads `/proc/uptime`,
which needs no elevated privileges. Richer privileged probes are out of scope here
and arrive in later epics.

---

## 2. Prerequisites

- The `hm` CLI is available (the monitor's command-line entrypoint).
- A **master key** and database are configured. The probe's private key is stored
  encrypted in the secrets store, which is unlocked by `HOMELAB_MONITOR_MASTER_KEY`.
  If you haven't set this up yet, see the authentication / secrets documentation
  (`docs/security/auth.md`) — do not invent your own key handling.
- The **config file** is readable by the monitor. `ssh_targets:` lives in
  `homelab-monitor.yaml`, located via the `HOMELAB_MONITOR_CONFIG` environment
  variable (default `/config/homelab-monitor.yaml` inside the container). After
  editing it you must **restart the monitor** for changes to take effect.
- The target is reachable from the monitor over the network on its SSH port.
- You have whatever credentials the target requires to log in and edit its
  authentication config for the **one-time** key install (Step 5). The framework
  does not need or store these — they are only for your manual install.

---

## 3. Step 1 — Declare the target

Add an entry under the top-level `ssh_targets:` key in `homelab-monitor.yaml`. The
list is empty by default (no targets ship with the public release).

### Fields

| Field               | Required | Description                                                                                                  |
| ------------------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| `id`                | yes      | Stable target id. Charset `[A-Za-z0-9._-]`. Used to name the probe key secret and the `uptime-<id>` collector. |
| `host`              | yes      | Hostname or IP of the target.                                                                                |
| `port`              | no       | SSH port. Default `22`. Range `1–65535`.                                                                     |
| `user`              | yes      | SSH login user. For an appliance, the privileged account your appliance provides. For a full-OS host, the dedicated low-priv user you create. |
| `account_mode`      | yes      | `appliance` or `dedicated-user` (note the **hyphen** in the YAML value).                                     |
| `key_secret_ref`    | no       | Secret name holding the probe private key. Defaults to `ssh_probe_key_<id>` if omitted.                      |
| `host_key`          | no       | The **bare** OpenSSH public host-key line you pin in Step 3. Leave unset until then.                         |
| `forced_command`    | no       | Appliance mode only: the single command the key is restricted to.                                           |
| `script_id`         | no       | Selects a probe script (dedicated-user mode). Mutually exclusive with `forced_command`.                     |
| `concurrency_group` | no       | Optional group label to serialize probes that must not run concurrently against the same device.            |

> `forced_command` and `script_id` are mutually exclusive — set at most one.

### Appliance example

```yaml
ssh_targets:
  - id: <target-id>
    host: <host>
    port: <port>
    user: <user>            # the privileged account your appliance provides
    account_mode: appliance
    forced_command: cat /proc/uptime
    # host_key: filled in Step 3 (capture-hostkey)
```

### Full-OS host example (`dedicated-user`)

```yaml
ssh_targets:
  - id: <target-id>
    host: <host>
    port: <port>
    user: <user>                  # the dedicated low-priv user you will create
    account_mode: dedicated-user
    # host_key: filled in Step 3 (capture-hostkey)
```

> `host_key` stays unset for now. You'll capture and paste it in Step 3.

After adding the entry, restart the monitor so the new config is loaded before
running the CLI steps below (the CLI reads the same config file).

---

## 4. Step 2 — Generate the probe key

Generate a per-target ed25519 keypair. The private key is written to the secrets
store as `ssh_probe_key_<target-id>` and is **never** printed or logged. The
command prints **only** the bare public key line.

```bash
hm ssh-probe keygen <target-id>
```

Output (the public key is yours to install in Step 5; values shown are illustrative
placeholders):

```
ssh-ed25519 <PUBKEY>
# install this public key on the target per `hm ssh-probe install-instructions`
```

- The private key stays encrypted in the secrets store. There is no command to
  print it.
- To **replace** an existing key, pass `--rotate`:

  ```bash
  hm ssh-probe keygen <target-id> --rotate
  ```

  Rotating **breaks the probe** until you reinstall the new public key on the
  target (Step 5). Without `--rotate`, `keygen` refuses to overwrite an existing
  key and exits non-zero.

---

## 5. Step 3 — Pin the host key

Capture the target's SSH host key so the probe can detect MITM on every future
connection. This is a **read-only** probe — it connects, captures the host key
during key exchange (pre-auth), and writes nothing (no secret, no config edit).

```bash
hm ssh-probe capture-hostkey <target-id>
```

Output:

```
# host key for target '<target-id>' at <host>:<port>
<HOST_KEY_LINE>
fingerprint: SHA256:<FINGERPRINT>
# WARNING (TOFU): this key was captured on FIRST contact and is NOT yet trusted.
# Verify the fingerprint above OUT-OF-BAND before pinning.
# To pin: set ssh_targets['<target-id>'].host_key to the bare line above.
```

`<HOST_KEY_LINE>` is a **bare** OpenSSH public-key line of the form
`<key-type> <base64>` (for example `ssh-ed25519 AAAA...`). It is **not** a
`known_hosts` / `ssh-keyscan` line — do not prefix it with a hostname.

> Why pinning matters. This is Trust On First Use (TOFU): the key was captured on
> first contact and is not yet trusted. An attacker positioned between the monitor
> and the target could present their own host key on that first connection. Verify
> the `SHA256:<FINGERPRINT>` **out-of-band** (e.g. on the device console, or
> against a fingerprint the vendor/admin gives you) before trusting it. Once
> pinned, any future host-key change is flagged as a possible MITM and the probe
> fails rather than connecting blindly.

After verifying the fingerprint, paste the **bare** line into the target's config:

```yaml
ssh_targets:
  - id: <target-id>
    # ...other fields from Step 1...
    host_key: <HOST_KEY_LINE>     # the bare 'ssh-ed25519 AAAA...' line, verified out-of-band
```

Restart the monitor after editing the config. The config loader rejects a
`host_key` that isn't a bare public-key line (e.g. a `known_hosts`-style line with
a leading hostname), with an actionable error.

---

## 6. Step 4 — Get the install instructions

Render the exact, paste-able setup recipe for the target. This command does **no**
network I/O and never prints the private key — it derives the public key from the
stored secret and renders instructions specific to the target's `account_mode`.

```bash
hm ssh-probe install-instructions <target-id>
```

### Appliance mode

For an appliance, the output is a single `authorized_keys` line. The key is locked
to your `forced_command` and stripped of every interactive/forwarding capability:

```
command="<forced_command>",no-port-forwarding,no-pty,no-X11-forwarding,no-agent-forwarding <PUBKEY> hm-probe-<target-id>
```

The hardening options (`no-port-forwarding`, `no-pty`, `no-X11-forwarding`,
`no-agent-forwarding`) ensure the probe key can do nothing but invoke the forced
command.

> Firmware-persistence warning. Some appliance firmware stores `authorized_keys`
> on a volume that is **wiped by firmware updates**. If your appliance behaves this
> way, **re-apply** the `authorized_keys` line after each firmware update, or the
> probe will fail to connect.

> If you haven't set `forced_command` in config, the rendered line uses a
> placeholder and the CLI prints a NOTE on stderr telling you to set it first. For
> the uptime exemplar, set `forced_command: cat /proc/uptime`.

### Dedicated-user mode (full-OS host)

For a full-OS Linux/NAS host, the output is a multi-step recipe. The essential
steps for the uptime exemplar:

**(a) Create a dedicated low-privilege user.** No interactive login beyond the
forced command; do **not** add it to admin/root groups:

```bash
sudo useradd -m -s /bin/sh <user>
```

> The home directory is not always `/home/<user>`. On some full-OS hosts and
> NAS-style systems a newly-created user's home lives elsewhere (for example under
> a service-homes path), and the home can differ from what `useradd` would default
> to. The forced-command in `authorized_keys` references the script by **absolute
> path**, so the script path **and** the `command="..."` path must match the user's
> **real** home. After creating the user, check the real home with:
>
> ```bash
> getent passwd <user>   # or: grep '^<user>:' /etc/passwd
> ```
>
> The home directory is the **6th colon-separated field**. Substitute that real
> home for `/home/<user>` everywhere below (script path, `.ssh/authorized_keys`
> path, and the `command="..."` path).

> The login shell must **not** be a no-login shell. OpenSSH runs the forced command
> via the user's **login shell** (`$SHELL -c "<forced command>"`). If the dedicated
> user's login shell is a no-login shell such as `/sbin/nologin` or
> `/usr/sbin/nologin` (a common default for service/NAS users), that shell refuses
> to execute the forced command and returns **empty output** — the probe then
> reports `up=0` even though the restriction itself holds. Check the shell (last
> colon field of `getent passwd <user>`) and, if it's a `nologin` shell, set it to a
> real shell such as `/bin/sh`. See Troubleshooting for the symptom and fixes.

> Some stripped-down host userlands (e.g. BusyBox-based NAS systems) lack
> `usermod` and `chsh`. If a command in these instructions isn't found, use the
> platform's native user-management tool, or edit `/etc/passwd` directly (see the
> login-shell entry in Troubleshooting). Setting the login shell to `/bin/sh` is the
> only shell change the probe needs.

**(b) Install the probe script** at `/home/<user>/hm-probe.sh` (or under the user's
**real** home — see the note above), owned by `<user>`, mode `0755`. For the uptime
exemplar the body reads `/proc/uptime`:

```sh
#!/bin/sh
cat /proc/uptime
```

> Use `cat /proc/uptime`, **not** `uptime`. The uptime exemplar parses the raw
> `/proc/uptime` output (seconds since boot). The `uptime` command's
> human-readable output will not parse and will report the host as down.

**(c) Append the forced-command `authorized_keys` line** to
`/home/<user>/.ssh/authorized_keys` (use the user's **real** home from the note in
(a)), owned by `<user>`, mode `0600`. Make the `command="..."` path match the real
home — `command="<real-home>/hm-probe.sh"`:

```
command="/home/<user>/hm-probe.sh",no-port-forwarding,no-pty,no-X11-forwarding,no-agent-forwarding <PUBKEY> hm-probe-<target-id>
```

> Paste it as **one physical line**. This is a single long line, and terminals or
> editors may **word-wrap** it into multiple physical lines on paste. A wrapped line
> is **broken**: sshd needs the options, key blob, and comment all on **one**
> physical line. Use a wrap-proof install instead of pasting into an editor —
> `printf` will not introduce wraps:
>
> ```bash
> printf '%s\n' '<the full authorized_keys line>' | sudo tee -a /home/<user>/.ssh/authorized_keys > /dev/null
> ```
>
> Then **verify it's one line**: `wc -l /home/<user>/.ssh/authorized_keys` should
> report the expected count (one per installed key), and `cat` it to confirm the
> `command="..."` options, the key blob, and the comment are all on a **single**
> physical line. Symptom of a wrapped/broken line: `hm ssh-probe test <target-id>`
> returns exit 1 (auth failure) even though the key looks present.

> No sudoers entry is needed. `cat /proc/uptime` reads a world-readable file and
> requires **no** elevated privileges, so the dedicated user needs **no** sudoers
> rule for the uptime exemplar. A future probe that needs privileged reads would
> add a narrow `NOPASSWD` sudoers entry scoped to specific absolute command paths —
> but that is **out of scope** for the uptime exemplar; skip it.

#### Synology NAS (`homelab-probe`, EPIC-008)

This subsection walks through provisioning the Synology NAS (`192.168.2.4`, non-standard port
`53197`) as a dedicated-user SSH probe target for EPIC-008. The real probe body (combined
SMART/array/UPS/hwmon collector) arrives in STAGE-008-014. This stage installs the placeholder
script and wires up the framework.

> **Overrides-repo only.** The `ssh_targets` entry below belongs in your **overrides repo**
> (the private, gitignored `homelab-monitor-overrides` repo mounted as a volume), **not** in
> the public `homelab-monitor` config. Do not add it to `deploy/compose/.env` or any file in
> this repo.

**(1) Declare the target in your overrides config:**

```yaml
ssh_targets:
  - id: synology
    host: 192.168.2.4
    port: 53197
    user: homelab-probe
    account_mode: dedicated-user
    script_id: synology_probe
    # host_key: filled after Step 3 (capture-hostkey)
```

`script_id: synology_probe` identifies which probe script body to load (STAGE-008-014 will
supply the real implementation). Until that stage, the installed script is the placeholder
below.

**(2) Run the four CLI provisioning steps:**

```bash
# Step 2 — generate the probe key (writes secret ssh_probe_key_synology)
hm ssh-probe keygen synology

# Step 3 — capture and pin the host key; paste the printed bare line into host_key above
hm ssh-probe capture-hostkey synology

# Step 4 — get the DSM-side install instructions
hm ssh-probe install-instructions synology

# Step 6 — verify the restriction holds (run after DSM steps below)
hm ssh-probe test synology
```

**(3) DSM-side manual steps (perform as a DSM admin in Control Panel → User & Group):**

a. **Create the `homelab-probe` DSM user.** Use Control Panel → User & Group → Create. Assign
   it to **no** admin group and **no** privileged groups. This is a read-only service account —
   do not grant it any DSM admin privileges or shell access beyond the forced command.

b. **Determine the user's real home directory.** DSM creates service-user homes under a
   path that may differ from `/home/homelab-probe`. After creating the user, log in via SSH
   as admin and run:

   ```bash
   getent passwd homelab-probe
   ```

   The sixth colon-separated field is the real home. Substitute it for `/home/homelab-probe`
   everywhere below (script path, `.ssh/authorized_keys` path, and the `command="..."` path).

c. **Set the login shell to `/bin/sh`.** DSM may default service users to `/sbin/nologin`,
   which prevents the forced command from running. Check the last field of
   `getent passwd homelab-probe`; if it's a nologin shell, set it:

   ```bash
   sudo sed -i '/^homelab-probe:/ s#/sbin/nologin#/bin/sh#' /etc/passwd
   getent passwd homelab-probe   # verify
   ```

   > **DSM caveat.** DSM can rewrite `/etc/passwd` from its user database on reboot or
   > after user-config changes, reverting the shell to `nologin`. If the probe reports
   > `up=0` after a reboot, re-check and re-apply this step.

d. **Deploy the canonical combined probe script.** The source of truth is
   `deploy/ssh-probes/hm-probe-synology.sh` in this repo — do **not** hand-author it.
   Deploy that exact file to `/usr/local/bin/hm-probe-synology.sh` (owner `root:root`,
   mode `0755`). The full body is also printed at the bottom of
   `hm ssh-probe install-instructions synology`, so you can copy it from there:

   ```bash
   # On the NAS, paste the body printed by `hm ssh-probe install-instructions synology`
   # (the block between "BEGIN canonical ..." and "END canonical ..."):
   sudo tee /usr/local/bin/hm-probe-synology.sh > /dev/null <<'HM_PROBE_EOF'
   <paste the canonical script body>
   HM_PROBE_EOF
   sudo chown root:root /usr/local/bin/hm-probe-synology.sh
   sudo chmod 0755 /usr/local/bin/hm-probe-synology.sh
   ```

   The forced command in `authorized_keys` then points at
   `/usr/local/bin/hm-probe-synology.sh` (the `install-instructions` output already uses
   this absolute path). Edit the canonical repo file and redeploy if the probe body ever
   changes; never hand-edit the copy on the NAS.

e. **Append the forced-command `authorized_keys` line.** Copy the exact line printed by
   `hm ssh-probe install-instructions synology` (it includes your actual public key). It
   will look like:

   ```
   command="/home/homelab-probe/hm-probe.sh",no-port-forwarding,no-pty,no-X11-forwarding,no-agent-forwarding ssh-ed25519 AAAA... hm-probe-synology
   ```

   Install wrap-proof (one physical line — do **not** paste into a terminal editor):

   ```bash
   sudo mkdir -p /home/homelab-probe/.ssh
   sudo chmod 700 /home/homelab-probe/.ssh
   printf '%s\n' '<the full authorized_keys line from install-instructions>' \
     | sudo tee -a /home/homelab-probe/.ssh/authorized_keys > /dev/null
   sudo chown -R homelab-probe:homelab-probe /home/homelab-probe/.ssh
   sudo chmod 600 /home/homelab-probe/.ssh/authorized_keys
   ```

   Verify it's one physical line:
   ```bash
   wc -l /home/homelab-probe/.ssh/authorized_keys   # should be 1
   cat /home/homelab-probe/.ssh/authorized_keys      # options + key + comment on ONE line
   ```

> **No sudoers entry needed for Synology.** Per EPIC-008 Amendment 3, the Synology probe
> reads everything **unprivileged**: per-attribute SMART data via `synodisk --enum` /
> `--smart_info_get`, array status via `/proc/mdstat`, UPS data via `upsc`, hardware
> sensors via hwmon sysfs, and filesystem usage via `df`. The only root-level read
> (btrfs scrub status) is fetched from the DSM API instead of the shell — so the probe
> script needs **no** elevated privileges. Skip the advisory sudoers step (step 3 in the
> generic recipe) entirely.

**(4) Verify:**

```bash
hm ssh-probe test synology
```

Exit 0 = restriction holds = setup correct. The probe is ready for STAGE-008-014 to install
the real collector body.

---

## 7. Step 5 — Perform the install by hand

The framework **never** edits the target's authentication config for you. Using
whatever access the target requires for this one-time setup, log in and apply the
output of Step 4:

- **Appliance:** append the single `authorized_keys` line to the appliance's
  `authorized_keys` for the privileged account (e.g. its `.ssh/authorized_keys`).
- **Full-OS host:** create the dedicated user, install `/home/<user>/hm-probe.sh`
  (mode `0755`), and append the forced-command line to
  `/home/<user>/.ssh/authorized_keys` (mode `0600`).

Connect using the credentials your target requires for this initial key install.
The monitor itself never uses those credentials — once the public key and
forced-command restriction are in place, the probe authenticates only with the
stored probe key, restricted to the forced command.

---

## 8. Step 6 — Verify the restriction holds

After installing by hand, verify the forced-command restriction actually works.
This connects with the probe key, runs an arbitrary marker command, and checks that
the target **refuses** to run it (because the forced command overrides whatever the
client asks for):

```bash
hm ssh-probe test <target-id>
```

### Exit codes

| Exit code | Meaning                                                                                                  | What to do                                                                                                   |
| --------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| **0**     | **PASS.** The forced command overrode the arbitrary command — the restriction holds. Setup is correct.   | Done. The probe is ready.                                                                                     |
| **1**     | **Could not test.** No pinned host key, missing probe key, connection/auth error, or **host-key mismatch**. | Fix the underlying issue (see Troubleshooting). On mismatch the CLI prints a `CRITICAL` MITM line on stderr. |
| **3**     | **Restriction NOT enforced.** The arbitrary marker command actually ran — the forced-command setup is broken. | Re-check the installed `authorized_keys` line includes the `command="..."` option, then re-run.              |

On a host-key mismatch the command prints, on stderr:

```
CRITICAL: host key mismatch for '<target-id>' — possible MITM
```

Do not ignore this — investigate before re-pinning.

---

## 9. Step 7 — The probe runs automatically

Once the target is declared in `ssh_targets`, its public key + forced command are
installed, and its host key is pinned, the monitor auto-registers an
`uptime-<target-id>` collector. On each run it emits:

| Metric                                        | Labels            | Meaning                                                        |
| --------------------------------------------- | ----------------- | -------------------------------------------------------------- |
| `homelab_ssh_up`                              | `{target}`        | `1` healthy / `0` failing — emitted every run.                 |
| `homelab_ssh_uptime_seconds`                  | `{target}`        | Seconds since boot, parsed from `/proc/uptime`.                |
| `homelab_ssh_probe_duration_seconds`          | `{target,probe}`  | Probe run duration — emitted every run.                        |
| `homelab_ssh_host_key_mismatch`               | `{target}`        | `1` if the presented host key didn't match the pin, else `0`.  |
| `homelab_ssh_last_success_age_seconds`        | `{target,probe}`  | Age of the last successful run (emitted once a success exists).|

The probe stays **read-only / observe-only**. The framework ships only this trivial
`uptime` exemplar; richer, privileged probes arrive in later epics — there's nothing
else to enable here.

---

## 10. Troubleshooting

| Symptom                                                   | Likely cause                                                                        | Fix                                                                                                       |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `test` exits **1**, "no pinned host key"                  | `host_key` not set in config.                                                       | Run `hm ssh-probe capture-hostkey <target-id>`, verify the fingerprint, paste the bare line into `host_key`, restart. |
| `test` exits **1**, auth error                            | The `authorized_keys` line wasn't installed, or the wrong key is installed.         | Re-run `install-instructions`, confirm the **current** `<PUBKEY>` is the one on the target.              |
| `test` exits **1**, `CRITICAL ... possible MITM`          | The presented host key doesn't match the pin.                                       | Stop. Investigate out-of-band before re-pinning — do not blindly re-capture.                             |
| `test` exits **3**                                        | The forced command isn't actually forcing — arbitrary commands run.                 | Ensure the installed `authorized_keys` line begins with `command="..."` (don't drop the hardening flags).|
| Appliance probe suddenly fails after a firmware update    | Firmware wiped `authorized_keys`.                                                    | Re-apply the appliance `authorized_keys` line (Step 4 / Step 5).                                          |
| `homelab_ssh_up == 0` but the target is reachable         | Most likely (dedicated-user): the user's **login shell is a `nologin` shell**, so the forced command returns empty output; **or** the script path / `command="..."` path doesn't match the user's **real** home. Also possible: the forced command's output doesn't match what the probe parses. | For a dedicated-user target, check the login shell and real home with `getent passwd <user>` (see the next two rows). Otherwise, for the uptime exemplar the script must output `/proc/uptime` format — use `cat /proc/uptime`, not `uptime`. |
| `test` **passes (exit 0)** but `up=0` / no uptime metric; forced command returns **empty** output, yet running the script manually as `<user>` works | The dedicated user's **login shell is a no-login shell** (e.g. `/sbin/nologin`, `/usr/sbin/nologin`). OpenSSH runs the forced command via the user's login shell (`$SHELL -c "<forced command>"`); a `nologin` shell refuses and produces no output, so the probe sees empty stdout → `up=0`. | Check the shell (last colon field of `getent passwd <user>`). If it's a `nologin` shell, set it to a real shell (`/bin/sh`). If `usermod`/`chsh` are unavailable, edit `/etc/passwd` directly: `sudo sed -i '/^<user>:/ s#/sbin/nologin#/bin/sh#' /etc/passwd`, then verify with `getent passwd <user>`. Caveat: some NAS management layers rewrite `/etc/passwd` from their own user database on reboot or user-config changes and can revert the shell — if the probe silently goes `up=0` again after a reboot/user change, re-check and re-set the login shell. |
| `up=0` (dedicated-user); script never runs                | The script path or the `command="..."` path doesn't match the user's **real** home directory (home is not always `/home/<user>`). | Run `getent passwd <user>` and read the 6th colon field for the real home. Install the script and `.ssh/authorized_keys` under that real home, and make `command="<real-home>/hm-probe.sh"` match. |
| `test` exits **1**, auth failure, key looks present       | The `authorized_keys` line was **word-wrapped** on paste into multiple physical lines (sshd needs it on one line).  | Re-install wrap-proof: `printf '%s\n' '<the full authorized_keys line>' \| sudo tee -a <authorized_keys_path> > /dev/null`. Verify with `wc -l <authorized_keys_path>` (one line per key) and `cat` to confirm options + key + comment are on one physical line. |
| Collector logs failures / quarantines; **no** `homelab_ssh_*` metrics appear, yet `hm ssh-probe test <target-id>` works from a normal shell | Runtime/ops (custom/container deployment): the monitor runs as a numeric UID with no passwd entry and no `USER`/`LOGNAME` env var, so the SSH client library can't resolve a local username ("unknown local username"-style error) and fails before connecting. | Current versions handle this internally (a default client-side username is seeded), so you shouldn't hit it. If you do in a custom/container deployment, set a `USER` (or `LOGNAME`) environment variable for the monitor process. |
| `keygen` refuses, "already exists"                        | A probe key already exists for this target.                                         | Pass `--rotate` to replace it (this breaks the probe until you reinstall the new public key).            |

---

> See also: this guide should be linked from the docs index when one exists.
> Related: `docs/security/auth.md` (master key / secrets), `docs/admin/redaction.md`
> (config-file location + restart behavior).
