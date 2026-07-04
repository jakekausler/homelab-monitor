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

## STAGE-009-004 — Runbook registry API & content-hash contract

- [ ] **STAGE-009-004 (unauth → 401):** Unauthenticated `GET /api/runbooks`, `POST /api/runbooks/refresh`, and `PATCH /api/runbooks/{id}` must each return `401` (no session cookie). Enforces non-negotiable #1 (allow-list management is authed).
- [ ] **STAGE-009-004 (CSRF on mutations):** `POST /api/runbooks/refresh` and `PATCH /api/runbooks/{id}` authed but WITHOUT an `X-CSRF-Token` header must return `403` (`code: csrf_mismatch`), not `200`/`401`. State-changing registry-management methods are CSRF-protected.
- [ ] **STAGE-009-004 (inert defaults — register ≠ enable ≠ auto-trigger):** A freshly-registered runbook must list with `enabled=false` AND `auto_trigger=false` (both gates OFF by default). A registered-but-not-enabled runbook is INERT. Enforces non-negotiable #1 (default manual; auto-trigger explicit opt-in only) and EPIC-009 Decision 4 (three orthogonal gates, inert defaults).
- [ ] **STAGE-009-004 (three gates orthogonal):** PATCH-ing `{enabled:true}` then `{auto_trigger:true}` then `{enabled:false}` on the same runbook must leave `auto_trigger=true` throughout (toggling one gate never resets the other). The `enabled`/`auto_trigger` gates are independent operator switches.
- [ ] **STAGE-009-004 (file-authoritative content_hash + drift detection):** After registration each runbook has a non-null `content_hash`. Editing the folder's `runbook.yaml` (e.g. `cooldown_seconds`) and re-running `POST /api/runbooks/refresh` must UPDATE the `content_hash` and the cached config field, AND must PRESERVE the operator `enabled`/`auto_trigger` gates (reconcile updates file-authoritative cached fields only, never clobbers gates). Enforces non-negotiable #4 (content hash stored at registration so drift is detectable) and the file-authoritative contract.
- [ ] **STAGE-009-004 (`_`-prefix folders skipped — exemplar never registers):** A runbooks root containing a `_examples/` (underscore-prefixed) folder with an otherwise-valid runbook inside must NOT register/list it. The pihole/`_examples` exemplar must never be auto-registered. Enforces EPIC-009 Decision 4 (exemplar-only) + non-negotiable #1 (conservative defaults).
- [ ] **STAGE-009-004 (malformed runbook reported, not fatal):** A refresh over a root with a malformed `runbook.yaml` (schema/pattern violation) and a folder missing/empty `CLAUDE.md` must return those folders in the response `errors[]` (each with `path` + `message`) while STILL registering the valid folders — one bad folder does not abort the scan.
- [ ] **STAGE-009-004 (refresh no-op on unchanged):** Re-running `POST /api/runbooks/refresh` with no on-disk change must register/refresh nothing (`registered=[]`, `refreshed=[]`), list the unchanged runbooks under `skipped`, produce no audit churn, and preserve gates.

## STAGE-009-005 — Auto-fix orchestrator (match → gates → docker-exec claude → capture → persist)

- [ ] **STAGE-009-005 (end-to-end match → gates → exec → capture → persist):** With a registered runbook (`enabled=1`, `auto_trigger=1`, `dry_run_required=0`, generous rate-limit, `cooldown=0`) matching a firing alert and `autofix_enabled='true'`, `AutoFixOrchestrator.handle_alert(alert)` execs the fake claude in a real fixer-runner container and persists a COMPLETED `runbook_runs` row (started/ended, `exit_code=0`, `mode='real'`, `fixer_user='homelab-fixer'`, host set, `runbook_hash`=content_hash, `transcript_path` discovered), an `alert_outcomes('auto_fixed')` row, and an `audit_log` `autofix.ran` row. Proves non-negotiables #1 (allow-list fire), #3 (homelab-fixer identity), #4 (full audited run record).
- [ ] **STAGE-009-005 (no-match → no run, nothing recorded):** An alert matching ZERO registered runbooks must produce no `runbook_runs`, no `alert_outcomes`, and NO `audit_log` denial entry — a non-match is silent (not a denial). `handle_alert` returns `None`.
- [ ] **STAGE-009-005 (ambiguous match → denied):** An alert matching ≥2 registered runbooks must DENY with `audit_log` `autofix.denied` gate `ambiguous_match`, run nothing, and write no `runbook_runs` row (conservative: a wrong auto-fix is worse than no auto-fix).
- [ ] **STAGE-009-005 (kill-switch gate first):** With `app_settings.autofix_enabled` unset/false, a matching auto-trigger alert is DENIED with `autofix.denied` gate `kill_switch` BEFORE any other gate, with no `runbook_runs` row. The kill-switch is checked at the TOP of the gate sequence (#7).
- [ ] **STAGE-009-005 (allow-list gate):** A matching runbook that is NOT (`enabled` AND `auto_trigger`) is DENIED `allow_list` — only enabled+auto_trigger runbooks auto-fire (#1).
- [ ] **STAGE-009-005 (rate-limit denial):** With `rate_limit_per_hour=1`, the first fire runs and a second immediate fire is DENIED `rate_limit` with NO new `runbook_runs` row and an `autofix.denied` audit entry (#6).
- [ ] **STAGE-009-005 (cooldown denial):** With `cooldown_seconds>0`, a fire within `last_run.ended_at + cooldown_seconds` is DENIED `cooldown` with no new run row (#6).
- [ ] **STAGE-009-005 (risky_blocked — dry-run-required denies auto-fire):** A matching, allow-listed runbook with `dry_run_required=1` is DENIED `risky_blocked` and never exec'd — 005's real-exec path only fires for non-risky runbooks (#5 enforced in code; STAGE-009-006 owns the real dry-run → approval flow).
- [ ] **STAGE-009-005 (concurrent-duplicate claim):** A second `handle_alert` for a runbook with an in-flight run (`runbook_runs.ended_at IS NULL`) is DENIED `already_running` with no second run row — the open-ended row is the durable claim.
- [ ] **STAGE-009-005 (exec non-zero exit → not auto_fixed):** When the fake claude exits non-zero, the `runbook_runs` row records the non-zero `exit_code` and NO `alert_outcomes('auto_fixed')` row is written (auto_fixed only on exit 0).
- [ ] **STAGE-009-005 (secret injection without leakage):** `ANTHROPIC_API_KEY` is fetched from the secrets store and injected into the exec env when present; never logged. Under the fake claude with no key set, exec proceeds and the transcript records `anthropic_api_key_present=0`.
- [ ] **STAGE-009-005 (Instance-A deployment surface):** After a prod monitor rebuild + `fixer` profile up (fake claude), the running monitor image contains `kernel/autofix/orchestrator.py`, the monitor is healthy, `homelab-fixer-runner` is up with the fake claude binary, `app.state.autofix_orchestrator` is non-None (docker enabled), and the monitor reaches the fixer-runner via the real docker socket.

## STAGE-009-006 (Dry-run mode + approval flow) — added 2026-07-01

- [ ] Migration 0047 (`runbook_run_approvals` table): schema present with columns `id`, `dry_run_id`, `runbook_id`, `alert_id`, `pinned_runbook_hash`, `status`, `approved_by`, `decided_at`, `real_run_id`, `created_at`; indexes on `status`, `dry_run_id`, `runbook_id`. Verify: `hm migrate status` reports head=0047; `SELECT sql FROM sqlite_master WHERE name='runbook_run_approvals'` matches the migration DDL.
- [ ] Non-negotiable #5 (risky → dry-run → approval → real): a risky runbook (`dry_run_required=True`, `enabled=True`, `auto_trigger=True`, matches an alert) triggers a plan-only claude exec (mode='dry_run') producing a stored `runbook_runs` row + a `pending` `runbook_run_approvals` row + `autofix.dry_run_stored` audit; NO real exec fires without an explicit approval. Verify via `apps/monitor/tests/integration/test_autofix_orchestrator_e2e.py::test_autofix_dry_run_approval_real_run_e2e_pipeline`.
- [ ] Dry cmd shape: claude is invoked with `--permission-mode plan` AND WITHOUT `--dangerously-skip-permissions` for dry runs; real cmd unchanged (`--dangerously-skip-permissions`). Verify: fake-claude `.args` files show the correct argv per run mode.
- [ ] Approval → real run: `POST /api/autofix/approvals/{id}/approve` with correct `confirm_phrase` transitions approval to `approved` (sets `decided_at`, `approved_by`, `real_run_id`) and fires a real exec via the shared `_claim_and_exec` path. Verify: e2e test above + `runbook_run_approvals.real_run_id` non-null after approve.
- [ ] Drift invalidation: if a runbook's `content_hash` changes between dry-run capture and approval, `execute_approved` (or the API `approve` endpoint) rejects the approval (status='rejected'), does NOT exec, writes `autofix.rejected` audit. Verify via `test_autofix_approval_drift_invalidates_e2e`.
- [ ] API auth + CSRF: `GET/POST /api/autofix/approvals*` require session (401 unauth); mutating routes require CSRF (403 without token). Verify: `curl -sS -o /dev/null -w '%{http_code}' http://192.168.2.148:29090/api/autofix/approvals` → 401.
- [ ] Confirm-on-destructive (§7.2): approve endpoint requires `confirm_phrase == "approve"`; wrong phrase → 400 with "approve" in the error message.
- [ ] Full audit (#4): both dry and real runs write `runbook_runs` rows distinguished by `mode`; the approve action writes `autofix.approved`; the reject action writes `autofix.rejected`; drift-triggered rejection writes an audit row (via `_reject_approval_txn`).
- [ ] No-orchestrator behavior: with `HOMELAB_MONITOR_DOCKER_ENABLED=false`, all autofix routes return 503 `autofix_unavailable`.
- [ ] Deferrals owned by later stages (track that they are NOT covered by 006 alone):
  - Markdown/whole-folder drift detection → STAGE-009-012 (006's drift check is CONFIG-hash-only).
  - Session-PIN confirm-on-destructive → STAGE-009-010/011 (006 ships typed `confirm_phrase` only).
  - Approval UI (list pending, view plan, approve/reject buttons) → STAGE-009-010/011 (006 is API-only).

## STAGE-009-007 (Kill switch — pre-run gate + mid-run kill + dashboard control)

Per-stage regression checks. Run these when suspecting any regression that touches the auto-fix kill switch:

- **Kill-switch toggle audit**: hit `POST /api/settings/autofix/kill-switch` with the correct confirm phrase; assert an `audit_log` row with `what="autofix.kill_switch_toggled"` was written.
- **Confirm-on-destructive enforcement**: hit the same endpoint without a phrase or with a wrong phrase; assert HTTP 400 and no state change.
- **Case-insensitive confirm phrase**: hit the endpoint with `"DISABLE AUTO-FIX"` or `"  disable auto-fix  "`; both should succeed.
- **Pre-run gate ordering**: verify `_check_operational_gates` still evaluates `autofix_enabled` FIRST. Trace the code path (`orchestrator.py:142-171`) or grep for `DenialReason.KILL_SWITCH`; the first check must be against `app_settings.autofix_enabled`.
- **Mid-run kill against fake claude**: run `make uv ARGS="--directory apps/monitor pytest tests/integration/test_fixer_runner.py::test_docker_kill_terminates_inflight_exec -v -m 'integration or not integration' --no-cov"` on the host (docker daemon required). Expected: PASS in <10s.
- **`runbook_runs.killed_at` column**: verify migration 0048 has NOT been reverted — `killed_at TEXT NULL` must be present in the `runbook_runs` schema (grep migrations or run `sqlite3 <db> 'PRAGMA table_info(runbook_runs)'`).
- **`DockerSocketClient.kill_container` idempotency**: assert 409 (container already stopped) is treated as success (idempotent), not raised.
- **Kill-switch endpoint auth**: unauth GET/POST → 401; missing CSRF header on POST → 403.
- **UI sub-nav route**: `/settings/logs` still redirects/serves; `/settings/autofix` serves the page; both `NavLink`s highlight correctly.

## STAGE-009-008 — Per-runbook scoped-capability granting + fixer-runner egress control

- [ ] `_resolve_grants` happy path: a valid `runbook.yaml` with `scoped_capabilities: {docker, ssh, egress}` resolves to a populated `ResolvedGrants` envelope; audit rows `autofix.grant_resolved` land with correct grant fields on `handle_alert`-driven exec.
- [ ] `_resolve_grants` missing YAML: record path pointing at a directory with no `runbook.yaml` raises `GrantResolutionError(reason='scoped_capabilities_unavailable')`; `_exec_claude` early-returns `(exit_code=1, transcript_path=None, errored=True)`; audit row `autofix.grant_failed` lands with correct reason + detail; `_current_run` is NEVER published (kill-switch coexistence preserved).
- [ ] `_resolve_grants` malformed YAML: syntactically invalid YAML raises `GrantResolutionError` with reason `scoped_capabilities_unavailable`; same early-return + audit as missing case; detail names the YAMLError.
- [ ] `_resolve_grants` schema-invalid YAML (ValueError branch): syntactically valid but Pydantic-invalid YAML (e.g. a YAML list at root, missing required RunbookConfig fields) raises `GrantResolutionError` with reason `scoped_capabilities_unavailable`; detail names the ValueError.
- [ ] `_resolve_grants` unknown SSH target_id: YAML declares `ssh: {target_id: 'bogus_nonexistent'}` and provider returns a frozenset without that id → raises `GrantResolutionError(reason='unknown_ssh_target_id')`; audit + early-return match the failure path.
- [ ] `_resolve_grants` docker-only: YAML declares docker + egress but no ssh → envelope has `ssh_target_id=None`, `docker_container` populated; audit row lands with `ssh_target_id: null`.
- [ ] `_resolve_grants` ssh-only: YAML declares ssh only (no docker) → envelope has `docker_container=None`, `docker_allowed_actions=()`; audit row lands with `granted_docker: null`.
- [ ] `autofix.egress_unenforced` warning: real (non-dry) exec that resolves grants with a non-empty egress list emits an `autofix.egress_unenforced` audit row referencing `STAGE-009-015` as owner. Dry runs do NOT emit this row. Empty-egress runs do NOT emit this row.
- [ ] Constructor cascade: `AutoFixOrchestrator` requires `ssh_target_ids_provider: Callable[[], frozenset[str]]` (default `frozenset`); production wiring in `apps/monitor/homelab_monitor/kernel/api/lifespan.py` passes `lambda: frozenset(load_ssh_target_configs().keys())`.
- [ ] Kill-switch coexistence: a grant failure MUST NOT publish `_current_run` (`_kill_lock`-protected assignment only occurs AFTER `_resolve_grants` succeeds). Test: fixture with missing runbook.yaml → post-call `orch._current_run is None`.
- [ ] Instance-A validation reproducibility: `docker exec homelab-monitor python3 -c "from homelab_monitor.kernel.api.app import create_app; from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator; print(hasattr(AutoFixOrchestrator, '_resolve_grants'))"` prints `True`. Warm `create_app` import first to avoid the standalone-REPL circular-import artifact.
- [ ] Follow-up (deferred, tracked in Refinement Design Notes / future cleanup): `homelab_monitor/kernel/autofix/__init__.py` eagerly imports the orchestrator; a cold Python REPL importing `homelab_monitor.kernel.autofix.orchestrator` FIRST hits a circular import via `kernel.api`. Lazy the autofix `__init__` re-exports to remove the cycle (cosmetic / code-hygiene only — does NOT affect prod boot).

## STAGE-009-009 — Claude→user improvement-feedback channel

- [ ] Migration `0049_runbook_run_feedback.py` applies up to head on a fresh SQLite DB and creates the `runbook_run_feedback` table with all 6 columns (id TEXT PK, runbook_run_id TEXT NOT NULL FK→runbook_runs.id, kind TEXT NOT NULL, suggestion_text TEXT NOT NULL, structured_hint TEXT NULL, created_at TEXT NOT NULL), the index `ix_runbook_run_feedback_runbook_run_id`, and the FK `fk_runbook_run_feedback_runbook_run_id` (bare FK, no ondelete — matches 0045/0047 convention). Verify via `PRAGMA table_info(runbook_run_feedback)` + `PRAGMA index_list('runbook_run_feedback')` + `PRAGMA foreign_key_list('runbook_run_feedback')`. Downgrade drops the table cleanly.
- [ ] `FeedbackKind` StrEnum in `homelab_monitor.kernel.autofix.types` has exactly 7 values: `missing_capability`, `config_change`, `runbook_gap`, `blocked`, `worked_around`, `other`, `parse_error`. `RunbookRunFeedback` frozen slots dataclass has fields (id, runbook_run_id, kind, suggestion_text, structured_hint, created_at). `SUGGESTION_TEXT_MAX = 4096`. `TRUNCATION_SUFFIX = "\n...[truncated]"`.
- [ ] `parse_feedback_file` correctly handles: (a) valid single-item file → 1 item; (b) valid multi-item → N items in order; (c) empty JSON list → `[]`; (d) missing `kind` key → parse_error item; (e) missing `suggestion_text` → parse_error; (f) unknown `kind` value → downgrades to `OTHER` (forward-compat, not parse_error); (g) `structured_hint` not a dict → parse_error; (h) `structured_hint` null or missing → item persists with `None`; (i) suggestion_text > 4096 chars → truncated with `\n...[truncated]` suffix; (j) non-JSON content → parse_error containing raw content (truncated). Verify by running `make uv ARGS="--directory apps/monitor pytest tests/test_feedback_parser.py -v --no-cov"` → 27 tests PASS.
- [ ] `scan_transcript_dir_for_feedback` respects the pre-exec snapshot: only returns `*.feedback.json` files NOT in `snapshot_before`; if multiple new files, returns the newest by mtime; empty transcript_dir → None; dir with only `.transcript` files → None. Docstring names the invariant: MUST be called from within `_transcript_lock` critical section.
- [ ] `RunbookRunFeedbackRepository.insert_conn` and `list_by_run` round-trip all 6 columns via an `AsyncConnection`; JSON `structured_hint` encodes/decodes via `json.dumps` / `json.loads`; `structured_hint=None` roundtrips as `None`; parse_error kind is persistable. FK constraint prevents insertion with a non-existent `runbook_run_id`. Verify with `pytest tests/test_feedback_repository.py -v --no-cov` → 9 tests PASS.
- [ ] Orchestrator's `_exec_claude` returns a 5-tuple ending in `feedback_items: list[ParsedFeedbackItem] | None`. Scan+parse happens INSIDE `self._transcript_lock` (immediately after `_resolve_transcript`), NEVER post-lock — this is the fix for the race the code-reviewer caught. Grant-error early return skips scan and returns `None`.
- [ ] `_process_feedback` no longer scans/parses — it takes pre-parsed `feedback_items` and just persists. No-op if `self._feedback_repo is None` OR `feedback_items` is None/empty. Wraps loop in try/except: any repo exception is swallowed + logged via `self._log.exception("autofix_feedback_processing_failed", ...)` so the parent txn survives.
- [ ] `_persist_outcome` order is: `mark_completed_conn` → `autofix.ran` audit → `_insert_outcome_conn` (if exit_code == 0) → `_process_feedback`. Feedback is LAST so a swallowed exception cannot poison the primary audit trail. Rationale comment at the swap site.
- [ ] All 3 txn paths (real success in `_persist_outcome`, errored in `_claim_and_exec`, dry in `_claim_and_store_dry`) call `_process_feedback` with the `feedback_items` from the `_exec_claude` return tuple. Verify with `pytest tests/test_feedback_orchestrator_wiring.py -v --no-cov` → 11 tests PASS.
- [ ] Malformed feedback marker → orchestrator persists ONE `runbook_run_feedback` row with `kind="parse_error"` AND emits `insert_audit(what="autofix.feedback_parse_error")` with `detail=<truncated suggestion_text>[:512]`. Both the row AND the audit must fire (Decision C3 — dual observability + audit integrity). Test: `test_real_success_malformed_feedback_persists_parse_error_and_audits`.
- [ ] Fake claude at `deploy/compose/fixer-runner/test/fake-claude` reads `FAKE_CLAUDE_FEEDBACK` (writes payload verbatim to `${TRANSCRIPT_DIR}/fake-claude-$$.feedback.json`) and `FAKE_CLAUDE_FEEDBACK_MALFORMED=1` (writes intentionally-broken content). Write occurs BEFORE the plan-mode `exit 0` so dry path is testable.
- [ ] Production wiring: `apps/monitor/homelab_monitor/kernel/api/lifespan.py` passes `feedback_repo=RunbookRunFeedbackRepository(repo)` to the `AutoFixOrchestrator` constructor. Backward compat verified: `feedback_repo=None` default preserves existing behavior (test: `test_feedback_repo_none_no_crash_orchestrator_continues`).
- [ ] `runbook_run_feedback` is a SIBLING table — `runbook_runs` schema unchanged. `audit_log` writes for `autofix.ran` / `autofix.exec_error` / `autofix.dry_run_stored` unchanged. `autofix.feedback_parse_error` is additive telemetry, not a replacement. Seven non-negotiables (allow-list trigger, scoped runbook, homelab-fixer identity, audit trail, dry-run + approval, rate-limit + cooldown, kill switch): all PASS-verified by code-reviewer pass 2.
- [ ] UI + list endpoint deferred to STAGE-009-011 (tracked existing stage; Deliverable #3 explicitly reads "Show the runbook_run_feedback items (STAGE-009-009) for the selected run"). Feedback rows are queryable via SQLite CLI in the interim.

## STAGE-009-012 — Regression Items (2026-07-03, Design)

- [ ] **Audit immutability invariant:** `runbook_runs` and `audit_log` MUST NOT have any DELETE endpoint on ANY router in `apps/monitor/homelab_monitor/kernel/api/routers/*.py`. Regression test asserts a delete attempt via any conceivable path returns 404/405. Non-negotiable #4 (audit integrity).
- [ ] **`runbook_hash` per-run invariant:** every `runbook_runs` row MUST have a non-null `runbook_hash`. Regression test asserts SELECT COUNT(*) FROM runbook_runs WHERE runbook_hash IS NULL == 0 after any orchestrator write path (dry + real).
- [ ] **Hash-drift check invariant:** `POST /api/autofix/approvals/{id}/approve` MUST return 409 `runbook_changed_since_plan` when the runbook's `content_hash` has changed since dry-run capture. Regression test: mutate a runbook file, then approve → 409 + `autofix.rejected` audit row + approval marked `rejected`.
- [ ] **Hash-missing-runbook invariant:** `POST /api/autofix/approvals/{id}/approve` MUST return 409 `runbook_missing` when the runbook folder has been deleted since dry-run.
- [ ] **Transcript rotation retains audit row:** rotation MUST prune transcript FILES only. The `runbook_runs` row MUST persist with `transcript_path=NULL` AND `transcript_pruned_at=<utc_iso>` after prune. Regression test asserts row count unchanged, file gone, marker set.
- [ ] **Rotation config configurable via env:** `HOMELAB_MONITOR_FIXER_TRANSCRIPT_ROTATION_MAX_COUNT` and `HOMELAB_MONITOR_FIXER_TRANSCRIPT_ROTATION_MAX_AGE_DAYS` MUST override `FixerRunnerConfig` defaults (100 / 365).
- [ ] **Rotation reuses fixer-runner identity split:** monitor process MUST call `docker exec homelab-fixer-runner rm ...` for actual file deletion; monitor MUST NOT attempt to unlink under `/data/runbook-transcripts` directly (would fail with EROFS; regression test asserts monitor's mount stays `:ro`).
- [ ] **Rotation admin endpoint:** `POST /api/autofix/rotate-transcripts` MUST require an authenticated admin session (401 on unauthenticated; 403 on non-admin session); returns `{files_pruned: int, runs_marked: int, runbooks_scanned: int}`.
- [ ] **Rotation graceful when fixer-runner disabled:** if `homelab-fixer-runner` container is not running, rotation MUST emit `autofix.transcript_rotation_skipped_fixer_disabled` audit row + log WARN, and NOT raise an unhandled exception.
- [ ] **Scheduler tick fires:** the daily 03:00 UTC autofix housekeeping tick MUST be observable via metric or log (regression test hooks the scheduler's tick emission).
- [ ] **Instance-A E2E: DELETE endpoints reject unauthenticated + authenticated:** `curl -X DELETE http://192.168.2.148:29090/api/autofix/runs/<any-uuid>` → 405; same for `/api/audit-log/<any-uuid>`. If ANY future stage adds a delete route on these tables, this test breaks — that's the point (non-negotiable #4).
- [ ] **Instance-A E2E: Rotate endpoint 401 unauthenticated + 200 admin+CSRF:** unauthenticated POST → 401; authenticated admin session POST with `X-CSRF-Token` from cookie → 200 body containing `{files_pruned, runs_marked, runbooks_scanned, skipped_reason}`. If any future refactor drops `require_session` from the endpoint, this catches it.
- [ ] **Instance-A E2E: RunOut/RunDetailOut schema fields present:** `docker exec homelab-monitor python -c "from homelab_monitor.kernel.api.routers.autofix_runs import RunOut, RunDetailOut; print('transcript_pruned_at' in RunOut.model_fields, 'runbook_hash' in RunDetailOut.model_fields)"` → both True. If a future stage removes either field, this catches it.
- [ ] **Instance-A E2E: DB schema present:** `sqlite3` against prod DB `SELECT sql FROM sqlite_master WHERE name='runbook_runs'` contains `transcript_pruned_at TEXT` and `runbook_hash TEXT`. If a future migration accidentally drops either column, this catches it.
- [ ] **Instance-A E2E: Alembic head at 0051 or later:** `alembic_current_revision` returns 0051+. Prevents a rollback below the STAGE-009-012 migration line.

**Owed inheritance re-pointing:**

- The STAGE-009-001 regression item ("STAGE-009-012 follow-up: whole-folder/markdown hash decision") is now owned by **STAGE-009-016** (whole-folder + markdown drift hash). Move / re-point at 016's Refinement.
- The STAGE-009-006 regression item ("Markdown/whole-folder drift detection → STAGE-009-012") is now owned by **STAGE-009-016**. Move / re-point at 016's Refinement.
- The STAGE-009-010 Refinement bug ("`POST /api/runbooks/refresh` does not prune deleted-folder DB rows") is now owned by **STAGE-009-017** (registry row pruning on refresh).

## STAGE-009-010 (Build + Refinement — completed 2026-07-02)

**Design decisions (locked 2026-07-02):**

- Deferred: manual-trigger endpoint + Run-fix button → **STAGE-009-010A** (new stage, owns backend + UI). Rationale: non-negotiable #1 (Trigger allow-list) requires a properly-designed operator-initiated trigger endpoint with its own regression surface.
- Deferred: backend defense-in-depth rejection of `PATCH auto_trigger=true` on `risky` runbooks → **STAGE-009-010A** (bundled). Rationale: today's backend safety net for risky is exec-time only (`dry_run_required` routes to approval); the PATCH itself is permitted. UI enforces the semantic; backend should mirror it.
- Deferred: session-PIN confirm-on-destructive → **STAGE-009-010B** (new stage, replaces typed-phrase across all destructive auto-fix actions). Rationale: STAGE-009-006 regression note; ships as UX polish, not safety-critical.
- Deferred: last-run + success-rate + run-count on catalog cards → **STAGE-009-011** (extended). STAGE-009-011 card updated with a new §3.5 "Runs aggregation endpoint" deliverable feeding both history table and 010 catalog cards.
- Deferred: general-purpose transcript viewer for all runs (dry + real) → **STAGE-009-011** (already in card §2). 010's approval Dialog shows only the plan_text via existing `GET /api/autofix/approvals/{id}/plan`.

**Build + Refinement regression items (2026-07-02):**

### Discovered during Refinement (2026-07-02)

- **Bug (STAGE-009-004 infra gap):** `deploy/compose/docker-compose.yml` monitor service was missing the `/runbooks` bind-mount that the STAGE-009-004 registry loader (`HOMELAB_MONITOR_RUNBOOKS_DIR` default) requires. Fixed in STAGE-009-010 Refinement — added `${HM_RUNBOOKS_SRC:-../../runbooks}:/runbooks:ro`. Symptom: `POST /api/runbooks/refresh` returns `errors: [{path: "/runbooks", message: "runbooks root /runbooks is not a directory"}]`. Origin: STAGE-009-004 shipped without a compose mount; not caught because no earlier stage consumed the endpoint end-to-end. Regression test: ensure `docker exec homelab-monitor ls /runbooks` succeeds after `make dev-prod`.

- **Bug (STAGE-009-004 design gap):** `POST /api/runbooks/refresh` does NOT prune DB rows whose disk folders have been deleted. The refresh implementation iterates disk folders and reconciles them but never inspects DB rows for orphaned entries. Effect: a deleted runbook folder's registry row lingers in the DB (inert — `enabled=false`, `auto_trigger=false`, and the loader can't load it — but visible in `GET /api/runbooks`). Not blocking for STAGE-009-010 sign-off (user approved with knowledge of this behavior). **Owner: file a follow-up in EPIC-009 backlog** — potentially bundled with STAGE-009-012 (audit immutability + rotation) since row pruning has audit implications, OR ship as a small STAGE-009-004 back-patch. Regression test: create runbook folder, POST refresh, delete folder, POST refresh again, confirm GET /api/runbooks no longer lists the deleted runbook.

- **Bug (project-wide):** `apps/ui/src/components/SidebarNav.tsx`'s root `<nav>` had `h-full flex-col` but no `overflow-y-auto`, causing nav items to clip at the bottom of tall lists (both mobile drawer and any short-height desktop viewport). Fixed in STAGE-009-010 Refinement. Regression test: mobile-viewport screenshot with many nav items visible confirms scrollability; and unit test can `render` SidebarNav in a small container and assert the nav element has `overflow-y-auto`.

- **UI coverage flake:** `apps/ui/src/components/crons/CronsToolbar.tsx`'s 250ms `setTimeout` debounce callback flakily contributes ± 1 function to vitest coverage totals depending on wall-clock timing between test render and cleanup, threshold-sensitive at the 80.00% functions boundary. Not fixed in STAGE-009-010 (out of scope). Owner: file a follow-up to add `vi.useFakeTimers()` + explicit timer-advance in `CronsToolbar.test.tsx`. Mitigated in STAGE-009-010 by adding margin via new runbook-file tests.

- **Observation (not a bug):** the amber kill-switch banner on `/runbooks` uses an `AlertTriangle` icon and can be visually confused with an error card by a first-time user. Current wording ("Auto-fix is disabled. Enable it in Settings → Auto-fix to toggle runbooks or approve runs.") is accurate; consider whether an info/warning icon (rather than triangle) would reduce the false-error read. Not blocking. Owner: potential UX polish stage.

- **Regression: the risky-runbook auto-trigger info tooltip.** Verify per Refinement sign-off that hover on the info icon renders the tooltip text "Auto-trigger produces a pending approval (not a real run). Approve to execute." for risky runbooks ONLY (not safe). Vitest test in `__tests__/RunbookCard.test.tsx` covers presence/absence of the info-icon element by risk_tag.

## STAGE-009-010A regression items (added 2026-07-02)

Added at Refinement 2026-07-02. All items derived from STAGE-009-010A Design Notes.

1. **Concurrent operator + alert trigger race** — both paths can enter `_claim_and_exec` for the same runbook simultaneously. Existing per-runbook `asyncio.Lock` serializes; the in-lock re-gate must use the correct `require_auto_trigger` flag for whichever path claimed second. Regression test: two concurrent triggers (one operator, one alert) — one wins, the other gets a cooldown/rate-limit rejection cleanly. Test lives at `tests/test_autofix_orchestrator.py::test_concurrent_operator_and_alert_trigger_race`.
2. **Shared rate-limit + cooldown bucket** — operator + alert share the same per-runbook rate-limit and cooldown; an operator hammering "Run fix" could exhaust the budget and starve a real alert-triggered fix. Kept shared bucket in STAGE-009-010A (documented property). Revisit in a later stage if it becomes a real problem.
3. **`alert_id IS NOT NULL` audit filters** — operator-initiated `runbook_runs` rows have `alert_id=NULL`. Any existing audit or history query that joins `runbook_runs` on `alert_id IS NOT NULL` (e.g., "runs per alert") will now silently exclude operator runs. STAGE-009-011 (history UI) must handle this — display "operator: <username>" when `alert_id IS NULL AND initiated_by='operator'`, rather than filtering the row out. Filed as a design note for STAGE-009-011.
4. **`homelab-fixer` OS user identity is unchanged for operator runs** — non-negotiable #3. Operator trigger still execs as `homelab-fixer` inside the fixer-runner container. Principal is for AUDIT ONLY, NEVER for `sudo -u` selection. Regression check: any exec-path change must not accidentally read the router-thread principal for exec identity.
5. **STAGE-009-011 direct consumer of `initiated_by`** — `initiated_by` column is a first-class schema contract. STAGE-009-011 (history UI) must USE this column directly to filter/display initiator (not synthesize it from audit_log). Filed as a design note on STAGE-009-011.
6. **Principal audit for `dry_run_stored`** — operator-initiated dry runs MUST log `who=<principal>` in the `autofix.dry_run_stored` audit (not `who='system:autofix'`). Verified at Build: `_claim_and_store_dry` now threads `principal` through into `audit_who` selection. Regression check for any future refactor that touches this helper.
7. **Frontend cache invalidation on trigger** — after a successful operator trigger the UI invalidates `runbooksKeys.all` AND `approvalsKeys.pending` (for dry_run_stored outcomes). On real-run outcomes, also invalidate any future stats query STAGE-009-011 adds. If STAGE-009-011 adds new query keys under the runbooks namespace, `useTriggerRunbook`'s `onSuccess` invalidation set must be extended.

## STAGE-009-010B — Session-PIN confirm-on-destructive

**Backend regression items:**

1. **Rate-limiter time-based lockout** — `InProcessPinRateLimiter.check()` compares elapsed time since last failure against the curve tier's `lockout_seconds`. After the tier duration elapses, `check()` returns None even though failures stay in the 15-min sliding window (they still count toward the next tier). Regression test: `tests/test_security_pin.py::test_rate_limiter_check_clears_after_lockout_elapses`. Do NOT revert to "failure-count-in-window" semantics.

2. **Post-cooldown failure escalation** — After a 5s lockout elapses, a 4th consecutive failure jumps directly to the 30s tier. Regression test: `tests/test_security_pin.py::test_rate_limiter_next_failure_after_lockout_escalates_tier`.

3. **HTTPException.headers propagation** — `_handle_http_exception` in `apps/monitor/homelab_monitor/kernel/api/errors.py` copies `exc.headers` onto the response. This is required for `Retry-After` to reach clients on 429s. Any future refactor of the error-envelope handler MUST preserve header forwarding.

4. **Destructive-action audit `credential_type`** — every audit row for approve, kill-switch, trigger, and their orchestrator-side rejection paths MUST include `credential_type: "pin" | "phrase"` in `after={}` when a credential was supplied. dry_run trigger correctly omits (no credential). Regression tests: `test_approve_audit_includes_credential_type`, `test_kill_switch_audit_includes_credential_type`, `test_trigger_audit_includes_credential_type`.

5. **Trigger endpoint server-side phrase gate** — `POST /api/runbooks/{id}/trigger` real-mode requires `confirm_phrase == basename(runbook.path)` (case-insensitive) OR a valid `confirm_pin`. This closed a 010A UI-only gap; direct-API bypass is no longer possible. Regression test: `test_trigger_real_without_credential_returns_400`.

6. **PIN CRUD ceremony** — PIN set requires `current_password`. PIN rotate requires `current_pin` (ticks pin_limiter on failure). PIN delete requires `current_password`. All four write dedicated audit rows (`security.pin_set`, `security.pin_rotated`, `security.pin_verify_succeeded`, `security.pin_verify_failed`, `security.pin_locked`, `security.pin_removed`).

7. **Existence endpoint safety** — `GET /api/settings/security/pin` returns ONLY `{set: bool}`. Never leaks the hash or per-user state. Any future extension MUST NOT return failure count / lockout state / user identity.

**Frontend regression items:**

8. **Unified 429 copy** — `ConfirmPinDialog` renders EXACTLY ONE line during lockout: "Too many wrong attempts. Please wait Ns before trying again." (destructive/red). The errorMessage prop is HIDDEN during countdown (not stacked amber+red). Vitest: `hides errorMessage and shows only the countdown message while retryAfterSeconds > 0`.

9. **Countdown interval cleanup** — `ConfirmPinDialog`'s countdown effect clears its setInterval when the counter reaches 0 (previously ticked forever, causing input to auto-clear every second). Uses `wasLockedRef` for locked→unlocked transition detection. Same fix on `SettingsSecurityPage.tsx` Rotate dialog.

10. **`showError` gate** — after countdown expires naturally, the errorMessage line is suppressed until a NEW submission arrives (prevents stale "too_many_requests" from flashing after cooldown ends). Local state `showError` resets to true on any new `errorMessage` prop value.

11. **RunFixDialog stays open on 429** — the confirm dialog does NOT close when the mutation returns 429; the countdown remains visible. Non-429 errors still close the dialog + show sonner toast (existing behavior).

12. **Trigger endpoint now sends phrase** — `useTriggerRunbook` phrase-branch now sends `confirm_phrase: runbookName` in the request body (was UI-only in 010A). This matches the new server-side gate — do not remove.

13. **`friendlyApproveError` handles `too_many_requests`** — returns empty string for that code to avoid raw backend text leaking through `ConfirmPinDialog.errorMessage`. `ConfirmPinDialog`'s unified copy handles it.

14. **`_per_test_db` conftest re-wire** — `apps/monitor/tests/conftest.py::_per_test_db` MUST re-wire `state.pin_rate_limiter` and `state.app_settings_repo` on every test. Manual mirror of lifespan. Any future stage that adds `app.state.*` must extend this fixture. Without it, endpoint tests return 503 across the board.

## STAGE-009-011 — Auto-fix history UI (filterable runs table + transcript viewer + feedback display)

- [ ] `/autofix/history` page loads without infinite loading. Fix reference: `useMemo` on `filters` in `RunsHistoryPage.tsx` — if a future refactor rebuilds `filters` (or default `since`/`until`) unmemoized per render, the query key churns and `isLoading` never settles. Root cause was `defaultSinceIso()` / `defaultUntilIso()` returning fresh `Date.now()`-based ISO strings on every render → new query key every render → refetch loop.
- [ ] `useSearch({ from: '/protected/autofix/history' })` and `useParams({ from: '/protected/autofix/history/$run_id' })` still use the `/protected/` route-id prefix. The layout route uses `id: 'protected'` (not `path:`), so children's route ids get prefixed with `/protected` even though URLs don't. Removing the prefix silently returns `never` and downstream `.since`/`.until` etc. become TS errors.
- [ ] Transcript viewer path-traversal defense on `GET /api/autofix/runs/{run_id}/transcript` — resolves the stored `transcript_path` and asserts `is_relative_to` the configured base dir. Regression: if a future change to the transcript persistence path or an updated stored value bypasses `Path.resolve()`, path traversal becomes possible. Test lives in `test_autofix_runs_router.py::test_get_transcript_404_when_path_escapes_base_dir`.
- [ ] Non-negotiable #4 (Audit): the history router is READ-ONLY. Regression: if any DELETE/PATCH/POST endpoint gets added to `autofix_runs.py`, code-reviewer must flag it. UI has no delete affordance anywhere (verified by vitest tests in `RunsTable.test.tsx`, `RunDetailPage.test.tsx`, `RunsHistoryPage.test.tsx`).
- [ ] Non-negotiable #5 (Dry-run/approval visual distinction): the detail page's mode banner is REQUIRED — dry_run uses distinct color (yellow) from real (blue). Table row mode badge is always shown. If the banner or badge is removed/hidden, code-reviewer must flag it. Test: `RunDetailPage.test.tsx::"shows a distinct dry-run banner for mode=dry_run"` + `"shows a distinct real-run banner for mode=real"`.
- [ ] `GET /api/runbooks/stats` output feeds BOTH the history page header AND the catalog cards' new `last_run_at` + `success_rate_30d` fields. Regression: if the stats endpoint's response shape changes, verify both surfaces still populate. `RunbookCard.tsx` renders em-dash fallback when null.
- [ ] Outcome derivation logic: `runbook_runs` has NO `outcome` column. Derived from `exit_code` + `killed_at` + `mode` + `ended_at`. Any change to `_derive_status_from_row` in `runs_repository.py` or `_derive_outcome` in `autofix_runs.py` must keep the 5 cases correct: `in_flight` (ended_at NULL AND killed_at NULL), `killed` (killed_at NOT NULL), `success` (real + exit_code=0), `failure` (real + exit_code!=0), `dry_run` (mode='dry_run' + ended_at NOT NULL). Tests in `test_runs_repository_stats.py::test_last_run_status_success_failure_killed_in_flight_dry_run` and `test_autofix_runs_router.py` (killed/dry_run/failure cases).
- [ ] Pagination: offset-based, `?limit=100&offset=N`. `total_count` reported in response. If pagination shape changes to cursor/keyset, `RunsHistoryPage.tsx`'s Pagination component + URL param logic must be updated together.
- [ ] Feedback rows display: `parse_error` `kind` renders with distinct destructive/red badge (visually distinguishes malformed feedback from valid). Other 6 kinds get outline/secondary variants. Regression test: `FeedbackList.test.tsx` covers all 7 kinds.

## STAGE-009-014 — Docker intent gateway

### Non-negotiable #2 (SCOPE — docker dimension) — CRITICAL
- **Intent for a container NOT in envelope → `autofix.intent_denied` with reason `container '<x>' not in envelope`; NO `docker.restart_container` call.** Regression test: `test_one_intent_denied_container_mismatch_no_docker_call` in `apps/monitor/tests/kernel/autofix/test_autofix_intent_gateway.py`.
- **Intent for an action NOT in `allowed_actions` → `autofix.intent_denied` with reason `action '<x>' not in allowed_actions`; NO docker call.** Test: `test_one_intent_denied_action_not_allowed_no_docker_call`.
- **Runbook with NO `docker` in `scoped_capabilities` (SSH-only or empty) → all intents `autofix.intent_denied` with reason `docker not in scoped_capabilities`; NO docker calls.** Test: `test_no_docker_capability_all_intents_denied`.
- **Intent `container` violating docker-name pattern (`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}$`) OR exceeding 256 chars → `autofix.intent_malformed` with reason `invalid_entry`; NO docker call.** Defense-in-depth against path-traversal / control characters / audit-DoS. Tests: `test_parse_intents_container_too_long_raises_invalid_entry`, `test_parse_intents_container_invalid_chars_raises_invalid_entry`. Envelope-side `DockerCapability.container` in `apps/monitor/homelab_monitor/kernel/runbooks/config.py` enforces the same pattern.

### Non-negotiable #3 (Identity — fixer no docker socket)
- **Fixer-runner container must NOT have access to a docker socket.** No mount of `/var/run/docker.sock` into the fixer container. Verified via `docker inspect homelab-fixer-runner` or compose config — sockets are ONLY mounted into the monitor container.
- **All docker mutations run in monitor process via `self._docker.restart_container(...)`.** Regression: no `subprocess.run(['docker', ...])` or similar shell-out in the fixer container's PATH; enforced by fixer container image which excludes the docker CLI.

### Non-negotiable #4 (Audit)
- **Every intent code path (executed / denied / exec_error / malformed / skipped_after_error / kill_switched / gateway_failed / dry_planned) emits a distinct `autofix.intent_*` audit row (8 events).** Tests: all 21 tests in `test_autofix_intent_gateway.py` verify audit `what` values via `_read_intent_audits`.
- **Unhandled exception in `_execute_intents` (both real + dry paths) → SINGLE `autofix.intent_gateway_failed` audit; `_persist_outcome`/completion still runs; run row is NEVER left orphaned (`ended_at IS NOT NULL`).** Test: `test_execute_intents_raises_emits_gateway_failed_audit_real`, `test_execute_intents_raises_emits_gateway_failed_audit_dry`.
- **Audit rows for intent gateway are immutable per STAGE-009-012 invariant.** No `DELETE FROM audit_log WHERE what LIKE 'autofix.intent%'` endpoint exists. Verified by grep for delete endpoints in `apps/monitor/homelab_monitor/kernel/api/routers/`.
- **Malformed intent file → SINGLE `autofix.intent_malformed` audit row, not per-entry denials.** Test: `test_malformed_intent_json_audits_malformed_and_returns`, `test_not_a_list_json_audits_malformed_with_reason`, `test_invalid_entry_audits_malformed`.
- **File-read failure (OSError, UnicodeDecodeError) → `autofix.intent_malformed` with reason `not_json`.** Tests: `test_parse_intents_read_failure_raises_not_json` (OSError) + implicit UnicodeDecodeError coverage via the same catch.
- **Detail strings on `autofix.intent_malformed` audit rows are truncated at 1024 chars + '...' suffix.** Prevents unbounded audit-table field bloat from oversized pydantic error messages or IO error messages. Tests: `test_parse_intents_read_failure_truncates_long_detail`, `test_parse_intents_invalid_entry_truncates_long_detail`.

### Non-negotiable #5 (Dry-run)
- **Dry runs NEVER call `docker.restart_container`.** Test: `test_dry_run_docker_never_called_regardless_of_validation` asserts `restart_calls == []` even with valid intents. Also tests: `test_dry_run_valid_intent_audits_dry_planned_no_docker_call`, `test_dry_run_denied_intent_audits_denied_not_dry_planned`.
- **Dry runs audit valid intents as `autofix.intent_dry_planned` (not `intent_executed`).** Denied intents in dry-run still audit as `intent_denied`.

### Non-negotiable #7 (Kill switch)
- **Kill switch flipped between `_exec_claude` return and `_execute_intents` start → ONE `autofix.intent_kill_switched` audit; ZERO `intent_executed`.** Test: `test_kill_switch_off_skips_all_intents_with_single_audit`.
- **Kill switch check is GATED on intent-file existence. A run with NO intent file emits ZERO audit rows regardless of kill-switch state (no spurious `intent_kill_switched` audit).** Test: `test_empty_intent_list_no_audits` covers the fast-path when file exists but list is empty.
- **Grant resolution failure (envelope None from `_resolve_grants` error) → intent gateway SKIPPED entirely; NO intent audits at all.** Test: `test_grant_resolution_failure_skips_intent_gateway`.

### Executor semantics (D4-C)
- **`DockerSocketConnectionError` (including `DockerExecTimeoutError` subclass) → 1 `intent_exec_error` + remainder `intent_skipped_after_error`; batch halts.** Test: `test_docker_connection_error_halts_batch_with_skipped_audits`, `test_docker_exec_timeout_error_treated_as_connection_error`.
- **`DockerSocketProtocolError` → 1 `intent_exec_error`; batch CONTINUES.** Test: `test_docker_protocol_error_continues_batch`.

### Real-host validation (STAGE-009-014 Refinement, 2026-07-04)
- **Instance A prod redeploy** included the intent gateway code. Verified via `docker exec homelab-monitor python -c "from homelab_monitor.kernel.autofix import intents; print(intents.DockerIntent.model_json_schema())"` — schema exposes `container` + `action=const("restart")` fields.
- **Real container restart** confirmed against a disposable alpine sleep container: `DockerSocketClient.restart_container(...)` called from monitor process, container's `StartedAt` timestamp changed post-call.
- **All 5 `validate_intent` decision paths** confirmed live (accept matching + 3 deny paths + no-docker deny) against real `ResolvedGrants` + `DockerCapability` types.

### Coverage gate
- **`apps/monitor/homelab_monitor/kernel/autofix/intents.py` — 100% branch coverage** including the `except (OSError, UnicodeDecodeError)` branch at lines 113-117 (`test_parse_intents_read_failure_raises_not_json` uses `monkeypatch` on `Path.read_text`).
- **`apps/monitor/homelab_monitor/kernel/autofix/orchestrator.py::_execute_intents` — 100% branch coverage** INCLUDING the try/except at both call sites, the file-existence fast-path, the empty-list fast-path, and the audit-txn-per-intent structure. Tests exist for both real + dry exception handlers.
