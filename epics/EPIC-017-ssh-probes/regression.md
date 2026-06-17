# Regression Checklist - EPIC-017: SSH probes

(Items added per stage during Refinement.)

## STAGE-017-001 — SSH transport

- Run `make uv ARGS="--directory apps/monitor pytest tests/ssh/ -v"` → all SSH transport tests pass (host-key accept/mismatch/not-pinned, auth ok/reject, run/exit, refused, timeout, error-mapping). 100% coverage on kernel/ssh/client.py.
- The real `AsyncSshClientFactory` connects to the live UDM (192.168.2.1:22 root) + Synology (192.168.2.4:53197) with a BARE-string pinned host key, runs a command, returns typed `SshCommandResult` (exit_status 0). [Read-only prod check; do not modify targets.]
- Pinning a WRONG host key against a real target raises `HostKeyMismatch` (host-key pinning enforced; the security-critical path).
- CONTRACT: `SshTargetParams.pinned_host_key` is the bare `ssh-ed25519 AAAA...` string (no hostname prefix); 017-002 config + 017-004 capture-hostkey must strip the `ssh-keyscan` hostname token.
