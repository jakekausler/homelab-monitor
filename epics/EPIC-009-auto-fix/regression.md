# Regression Checklist - EPIC-009: Auto-fix

(Items added per stage during Refinement.)

## STAGE-009-001 — Runbook schema & config-file contract

- [ ] Migration `0045_runbook_schema.py` applies up to head on a fresh SQLite DB and adds all 8 `runbooks` columns (alert_match_patterns, risk_tag default 'risky', dry_run_required default 1, rate_limit_per_hour, cooldown_seconds, enabled default 0, auto_trigger default 0, content_hash) and all 10 `runbook_runs` columns (alert_id FK→alerts.id, mode, prompt, transcript_path, exit_code, started_at, ended_at, fixer_user, host, runbook_hash). Verify via `PRAGMA table_info` on both tables + `PRAGMA foreign_key_list(runbook_runs)` shows the alerts FK.
- [ ] Migration downgrades cleanly to `0044` (all 18 new columns gone; original stub columns + `runbook_id→runbooks.id` FK preserved).
- [ ] Conservative open-source-safe defaults hold when a real runbook YAML omits them: `RunbookConfig.load_from_path` yields `risk_tag=RISKY` and `dry_run_required=True`.
- [ ] Safety gate (non-negotiable #2 scope): a runbook config whose `scoped_capabilities` declares NEITHER `docker` NOR `ssh` is REJECTED by `RunbookConfig` with a `ValueError` ("must declare at least one of 'docker' or 'ssh'"). Egress-only is not a valid scope.
- [ ] `RunbookConfig.load_from_path` rejects malformed files (missing `scoped_capabilities`; non-mapping YAML root; unknown extra top-level field via extra=forbid) with a `ValueError` that includes the file path.
- [ ] `compute_runbook_content_hash` is YAML-format-agnostic (same semantic config in different formatting → identical hash) and sensitive to semantic change (mutating a field → different hash).
- [ ] **STAGE-009-012 follow-up (deferred from 001 Design):** decide whether per-run drift detection (`runbook_hash`) must also cover markdown-intent changes (whole-folder hash incl. `*.md`), or remain config-only (current 001 behaviour = canonical-config hash only).

## STAGE-009-002 — Runbook provisioning, host ACLs, and orchestrator init

- [ ] **STAGE-009-002:** `scripts/host-setup.sh` section 3.9 provisions `/var/lib/homelab-monitor/runbook-transcripts` (or `$HM_FIXER_TRANSCRIPTS_SRC`) and applies POSIX default ACLs granting the monitor runtime UID `r-x` (READ-ONLY — never a write bit; #4 audit integrity) and `HM_FIXER_UID` `rwx`. Verify via `getfacl <dir>`: `user:<monitor-uid>:r-x`, `user:<fixer-uid>:rwx`, plus matching `default:` entries that inherit to new files.
- [ ] **STAGE-009-002:** host-setup.sh section 3.9 is idempotent (re-run leaves `getfacl` byte-identical, no duplicate ACL entries) and `--check` mutates nothing (all section-3.9 mutations are wrapped in `do_or_check`).
- [ ] **STAGE-009-002:** host-setup.sh WARN-degrades when `setfacl` is unavailable — falls back to a shared supplementary group (`HM_FIXER_GID`) + setgid directory (`chmod 2770`), emits a WARN, and does NOT error out.
- [ ] **STAGE-009-002:** the monitor container mounts the transcript dir READ-ONLY (`docker-compose.yml`: `...runbook-transcripts:/data/runbook-transcripts:ro`) — confirm the `:ro` suffix is present (audit integrity #4: monitor must not be able to mutate in-progress transcripts).
- [ ] **STAGE-009-002:** the orchestrator's docker-exec path is viable — the prod `homelab-monitor` container mounts `/var/run/docker.sock` RW, has the docker GID in its process supplementary groups, and can `docker exec` into sibling containers (in-container `/usr/bin/docker` CLI + SDK-over-socket both available). This is the path future stages use to exec into the fixer-runner.
- [ ] **STAGE-009-002 (host PATH gotcha):** on this host the homebrew `setfacl` (`/home/linuxbrew/.linuxbrew/bin/setfacl`) rejects bare numeric UIDs; the system `/usr/bin/setfacl` handles them. `sudo bash host-setup.sh` uses root's `secure_path` (no homebrew) so it resolves the system setfacl correctly — but if section 3.9 is ever run NOT via sudo/root, ensure the system setfacl is used (numeric-UID-capable).

## STAGE-009-003 — `fixer-runner` container — Dockerfile + static CLAUDE.md + compose wiring

- [ ] fixer-runner image builds with a FAKE claude (`--build-arg CLAUDE_BINARY_SOURCE=fake`) and runs the idle keepalive (PID 1 = `tail -f /dev/null`).
- [ ] Non-interactive `docker exec -i -u homelab-fixer ... claude -p <folder> --dangerously-skip-permissions < /dev/null` works: argv passthrough captured, transcript written to the RW-mounted dir, file owned by HM_FIXER_UID:GID (1002:1002 default) — #3 identity.
- [ ] `docker kill` terminates a `FAKE_CLAUDE_SLEEP`-stalled in-flight exec — #7 kill switch.
- [ ] Integration test `apps/monitor/tests/integration/test_fixer_runner.py` SKIPS FAST (require_docker) when the docker daemon is unavailable; runs (both tests pass) under `make integration` with Docker present.
- [ ] Dockerfile #3 identity holds: homelab-fixer created from build-arg UID/GID, `USER homelab-fixer` at end, NO root at runtime, NO docker group, NO sudoers; compose has NO `user:` override.
- [ ] Dockerfile claude-install RUN ends with `test -x` (presence check), NEVER a binary-EXECUTING command (executing the fake at build time fails: transcript dir absent at that layer).
- [ ] fixer-runner compose service is `profiles: ["fixer"]` (OFF by default); RW transcript mount (NO `:ro`) while the monitor's transcript mount stays `:ro`; NO `ports:`; ANTHROPIC_API_KEY empty passthrough; on the dedicated `fixer-egress` network.
- [ ] baked CLAUDE.md floor contains ONLY universal invariants — NO host-specific allow/deny targets leaked (open-source split intact).
- [ ] Real-host ACL round-trip (host-setup.sh §3.9 applied): fixer (1002) writes a transcript into /var/lib/homelab-monitor/runbook-transcripts; new file inherits the default ACL (monitor UID 995 effective r--, audit-readable); the live prod monitor's `:ro` mount can LIST+CAT it but CANNOT write (audit integrity #4).
