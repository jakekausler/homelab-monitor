# Regression Checklist - EPIC-017: SSH probes

(Items added per stage during Refinement.)

## STAGE-017-001 — SSH transport

- Run `make uv ARGS="--directory apps/monitor pytest tests/ssh/ -v"` → all SSH transport tests pass (host-key accept/mismatch/not-pinned, auth ok/reject, run/exit, refused, timeout, error-mapping). 100% coverage on kernel/ssh/client.py.
- The real `AsyncSshClientFactory` connects to the live UDM (192.168.2.1:22 root) + Synology (192.168.2.4:53197) with a BARE-string pinned host key, runs a command, returns typed `SshCommandResult` (exit_status 0). [Read-only prod check; do not modify targets.]
- Pinning a WRONG host key against a real target raises `HostKeyMismatch` (host-key pinning enforced; the security-critical path).
- CONTRACT: `SshTargetParams.pinned_host_key` is the bare `ssh-ed25519 AAAA...` string (no hostname prefix); 017-002 config + 017-004 capture-hostkey must strip the `ssh-keyscan` hostname token.

## STAGE-017-002 — ssh_targets config model

- [ ] `load_ssh_targets()` with no `HOMELAB_MONITOR_CONFIG` override returns `{}` (public default empty — no targets shipped).
- [ ] A YAML `ssh_targets` entry with `account_mode: dedicated-user` (hyphen) resolves to `SshTargetParams.account_mode == "dedicated_user"` (underscore).
- [ ] An entry omitting `key_secret_ref` resolves to `key_secret_name == "ssh_probe_key_<id>"`.
- [ ] A `host_key` given as a ssh-keyscan/known_hosts line (leading hostname token) is REJECTED with a "provide the BARE public key" error.
- [ ] An entry setting BOTH `forced_command` and `script_id` is REJECTED (XOR).
- [ ] Duplicate `id` across entries is REJECTED.
- [ ] Unknown/extra field on an entry is REJECTED (`extra="forbid"`).
- [ ] The lifespan-wired `AsyncSshClientFactory` resolver returns the correct `SshTargetParams` for a configured id and `None` for an unknown id.

## STAGE-017-003 — SshProbe base collector + health metrics

- [ ] A concrete `SshProbe` subclass against the loopback server (happy path) emits `homelab_ssh_up{target}=1`, `homelab_ssh_probe_duration_seconds`, `homelab_ssh_host_key_mismatch{target}=0`, `homelab_ssh_last_success_age_seconds=0.0` (first success), and its payload metric; `CollectorResult.ok=True`.
- [ ] Connected + `parse` returns `up=False` → `ok=True`, `homelab_ssh_up=0`, payload still emitted (probe completed; target sad).
- [ ] A `HostKeyMismatch` (wrong pinned host key) → `ok=False`, `homelab_ssh_up=0`, `homelab_ssh_host_key_mismatch{target}=1`; no key material in the error message.
- [ ] A connection failure (no listener / refused) → `ok=False`, `homelab_ssh_up=0`, `homelab_ssh_host_key_mismatch=0`.
- [ ] `homelab_ssh_last_success_age_seconds` is OMITTED before the first successful (up=1) run; emits 0.0 on every up=1 run; emits elapsed (>0) on an up=0 run after a prior success.
- [ ] Defining a concrete `BaseCollector`/`SshProbe` subclass WITHOUT `name`/`interval`/`timeout` raises (enforcement still fires); `SshProbe` itself (`abstract=True`) does NOT raise despite lacking them.
- [ ] `homelab_collector_run_*` self-observation metrics are emitted by the scheduler (NOT the probe) — the probe does not emit them.

## STAGE-017-004 — hm ssh-probe keygen + capture-hostkey

- [ ] `hm ssh-probe keygen <t>` writes secret `ssh_probe_key_<t>` (PEM) + prints the bare PUBLIC key; stdout/stderr contain NO `PRIVATE KEY`.
- [ ] `hm ssh-probe keygen <t>` on an existing secret refuses (exit 1) without `--rotate`; `--rotate` replaces it; `--rotate` on an absent secret errors (exit 1).
- [ ] `hm ssh-probe keygen "bad id!"` (invalid charset) and a missing master key both exit 1 with clean errors (no traceback).
- [ ] `hm ssh-probe capture-hostkey <t> --host H --port P` against a reachable SSH host prints a bare host-key line + `SHA256:` fingerprint + TOFU warning + paste instruction, exit 0, and WRITES NOTHING (no secret, no config).
- [ ] **REGRESSION:** capture-hostkey succeeds even when the target's host key is ALREADY in `~/.ssh/known_hosts` (the `known_hosts=asyncssh.import_known_hosts("")` fix — any falsy value reintroduces the bug). Covered by `test_capture_hostkey_succeeds_when_key_already_in_known_hosts`.
- [ ] capture-hostkey on an unreachable host/port (e.g. `--port 1`) exits 1 with a clean connection error; a target not in `ssh_targets` config (no `--host`) exits 1 with a "not found in ssh_targets" error.
- [ ] capture-hostkey works against a non-standard port (e.g. Synology :53197) via `--port`, and accepts whatever host-key TYPE the server negotiates (ssh-rsa / ssh-ed25519 / ecdsa) — the validator is key-type-agnostic.
