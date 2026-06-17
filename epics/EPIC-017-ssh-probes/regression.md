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
