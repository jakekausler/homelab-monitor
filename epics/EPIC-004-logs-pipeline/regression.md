# Regression Checklist - EPIC-004: Logs pipeline

(Items added per stage during Refinement.)

## STAGE-004-001 — Vector multi-line log stitching

- Run `make integration` — `test_vector_multiline.py` must pass all 11 tests (9 languages stitch into single VL records with tail fragments present; 2 negative controls stay separate).
- `make uv ARGS="--directory apps/monitor pytest tests/test_vector_template.py"` — multiline structural + regex tests pass; `test_test_fixture_multiline_matches_template` (drift guard) confirms `deploy/compose/test-fixtures/vector.toml` multiline block is byte-identical to `deploy/vector/vector.toml.template`.
- Vector config must use `mode = "continue_through"` (NOT halt_before) on `[sources.docker_logs.multiline]`. NO `[sources.journald.multiline]` block (Vector's journald source rejects it → crash-loops the whole config).
- After any vector.toml.template change in prod: recreate monitor with `--force-recreate` so the boot-time render writes the new config; confirm `docker exec homelab-vector cat /etc/vector/vector.toml` shows the change AND vector is `Up` (not Restarting).
- Vector patterns MUST contain no lookarounds (`(?!`, `(?=`, `(?<!`, `(?<=`) — Rust `regex` crate rejects them. Enforced by `_assert_no_lookarounds` in test_vector_template.py.

## STAGE-004-002 — LogLine shape convergence

- All 3 log endpoints (`/api/integrations/docker/containers/{name}/logs`, `/api/crons/{fp}/runs/{run_id}/log`, `/api/logs/query`) MUST return `.lines: list[LogLine]` where `LogLine = {timestamp, message, stream, severity, host, service, fields}`. NO `entries` outer field; NO `ContainerLogLine`/`RunLogLine`/`LogsQueryEntry` inner types.
- `make uv ARGS="--directory apps/monitor pytest tests/test_logline_model.py"` — mapper branch matrix (severity numerics/aliases/canonical/unknown→info/None, host/service extraction+fallback, severity_raw preservation, input-not-mutated) must pass at 100%.
- Severity is normalized at the mapper to canonical lowercase: debug/info/notice/warn/error/critical/alert/emergency. Raw value preserved in `fields.severity_raw`.
- `make integration` — `test_vector_to_vl_path` + `test_vector_multiline` (×11) must read `.lines[].message` from real VL data without shape errors. A field rename here breaks both the frontend component mocks AND these integration assertions — update all when changing the shape.
- After ANY change to the LogLine shape or the 3 response models: regenerate `make openapi-export` + `bash scripts/generate-ui-types.sh`, and grep `apps/ui/src/**/__tests__` for stale `entries:`/`line:` mock fields.

## STAGE-004-003 — `<LogViewer>` extraction + cron/docker viewer refactor

- Docker container logs view + cron run log view both render through shared `<LogViewer>`/`LogLineList` primitives — verify they still render lines (timestamp column + divider, monospace).
- ANSI: a docker log line containing `\x1b[31m...\x1b[0m` renders colored text, not raw escape codes; malformed/unterminated ANSI does not crash the viewer.
- Friendly UTC timestamps: row + header `Last:` show `YYYY-MM-DD HH:MM:SS UTC` (no nanoseconds, no T/Z); non-ISO timestamps pass through unchanged. (NOTE: STAGE-004-009 will add UTC→local conversion to BOTH row and header.)
- Wrap toggle (labeled "Wrap"): toggling switches long lines between wrap and horizontal-scroll; default is nowrap. Present in both docker + cron viewers.
- Severity tint: warn → yellow, error/critical/alert/emergency → red, else none; null severity → no tint (docker logs currently default to info until STAGE-004-004A lands).
- Backend test harness: the suite boots a session-scoped shared app (not per-test). Regression risk = test-isolation leaks; if a test starts failing only in-suite (passes in isolation), suspect a missing per-test reset in conftest `_per_test_db`. Real-lifespan tests (test_lifespan_e2e.py / test_api_lifespan.py) must set their own env (don't inherit shared-app env). Run the full suite (xdist) to confirm 3264 passed / 0 fail.

## STAGE-004-004 — Container label enrichment

- Vector docker_enrich: docker container logs in VictoriaLogs have top-level queryable fields compose_project, compose_service, image_name, image_tag (image_digest/image_revision when present). Verify `compose_project:homelab-monitor` LogsQL returns real lines.
- Raw `.label.*` bag is KEPT alongside promoted fields (no del(.label)).
- journald/systemd lines are NOT docker-enriched (exists(.label)/exists(.image) guards): bare systemd lines have empty compose_*/image_*.
- docker_enrich VRL must pass authoritative `vector validate` (NOT --no-environment, which masks VRL compile errors). VRL array index requires an integer literal, not a computed expression (use for_each, not segs[length-1]).
- Deployment: vector config is render-on-boot; after a template change, restart the monitor container (re-render) THEN the vector container (reload). Vector does not hot-reload.

## STAGE-004-004A — Docker log severity-level extraction

- Vector docker_severity_extract: docker container logs with leading severity tokens (ERROR, WARN, CRITICAL, FATAL, etc.) have `.severity` extracted in VL and queryable in LogsQL. Verify `severity:error AND compose_service:homeassistant` returns HA error lines.
- Anchored patterns: optional ANSI escape + optional ISO timestamp prefix allowed before the level token. ANSI-wrapped `\x1b[31m...ERROR` lines AND plain `ERROR ...` lines both promote.
- Guards: `exists(.label)` excludes journald/systemd lines from docker_severity_extract (their severity reflects journald PRIORITY, not message-text parsing). `severity == "info" || is_null(.severity)` ensures lines with an already-set severity (from .level/.PRIORITY in add_labels) are not overwritten.
- Canonical values: every emitted severity is in the 8-set (debug/info/notice/warn/error/critical/alert/emergency). FATAL→critical (NOT error); PANIC→emergency; ERR→error; CRIT→critical; WARNING→warn.
- Authoritative gate: the slow @pytest.mark.slow `test_rendered_template_passes_vector_validate` runs plain `vector validate` (NOT --no-environment) against the rendered template. Skips locally when vector binary absent; runs in CI. Closes the STAGE-004-004 false-green gap.
- VRL `match()` is infallible — `match!` is rejected by Vector with E620. Use plain `match()` for regex match calls in VRL.

## STAGE-004-005 — Cron fingerprint enrichment

- After any monitor rebuild + wrapper re-apply, verify a fresh hmrun cron line in VictoriaLogs carries a top-level `cron_fingerprint` (full 64-char) AND `run_id`, with a CLEAN `_msg` (no `HM_FP=`/`HM_RUN=` prefix) and raw `HM_FP`/`HM_RUN` fields absent. Quick check:
  `curl -s 'http://127.0.0.1:19428/select/logsql/query' --data-urlencode 'query=service:hmrun AND cron_fingerprint:* AND _time:30m' --data-urlencode 'limit=3'`
- Verify the VL `cron_fingerprint` matches `cron_runs.cron_fingerprint` for the same cron (cross-correlation invariant — consumed by Drain STAGE-004-025 model key `cron:<fingerprint>`, deep-links STAGE-004-021, failure correlation STAGE-004-034).
- Verify the legacy fallback: old-format hmrun lines (text-prefix `HM_RUN=<uuid> `, no HM_FP) still get `run_id` extracted and do not break the `hmrun_shaped` transform.
- Wrapper invariant: `logger --journald` per-line emission must remain best-effort (`|| true`) — a logger failure must NEVER break the wrapped cron; the original output line still goes to real stdout regardless.
- PRIORITY=5 (notice) is the chosen severity for hmrun structured-field lines — if a future change alters logger emission, confirm severity stays "notice" in VL.
- **Boundary markers must carry the fingerprint too:** HM_RUN_START / HM_RUN_END lines must arrive in VL with `_TRANSPORT=journal` and a populated `cron_fingerprint` (NOT empty). They are emitted via `logger --journald` in the wrapper's `log_marker()` (NOT `logger --tag`, which would produce `_TRANSPORT=syslog` with no structured fields). Quick check: `curl -s 'http://127.0.0.1:19428/select/logsql/query' --data-urlencode 'query=service:hmrun AND _time:30m AND ("HM_RUN_START" OR "HM_RUN_END")' --data-urlencode 'limit=10'` → every START/END record has non-empty `cron_fingerprint`.
- **Version-coupled tests reference the constant:** wrapper-format-version tests must reference `WRAPPER_FORMAT_VERSION` (not hardcode a literal) so a version bump doesn't break the suite. If a future bump breaks tests asserting `== "x.y.z"`, change them to reference the constant. (The two `_parse_semver(...)` parser unit tests legitimately use literals.)
- **WRAPPER_FORMAT_VERSION bump → re-apply wrapper:** changing the wrapper template format requires bumping `WRAPPER_FORMAT_VERSION` AND re-applying the host wrapper (`/usr/local/bin/cron-with-heartbeat.sh`) for the change to take effect on live crons.

## STAGE-004-006 — Redaction pipeline

- **No secret reaches VL (security-critical):** after any redaction-config change + redeploy, plant the 5-pattern corpus (bearer ≥20-char token, jwt, password-in-url, aws AKIA key, api_key=) on a real ingest path; direct-search VL for each raw secret value → must return ZERO. Quick: `curl -s 'http://127.0.0.1:19428/select/logsql/query' --data-urlencode 'query=<raw-secret>' --data-urlencode 'limit=1'` → empty.
- **Metric reaches VictoriaMetrics:** `curl -s 'http://127.0.0.1:18428/api/v1/query' --data-urlencode 'query=vector_redactions_total'` → series with `pattern_type` labels (the 5 names), counts>0, NEVER secret text in a label. If empty: check `field does not exist` errors in `docker logs homelab-vector` (the log_to_metric markers must be ALWAYS-set integers, increment_by_value=true) and the vector:9598 scrape target health.
- **No vector ERROR storm:** `docker logs --since 60s homelab-vector | grep -c "Field does not exist\|ParseFloatError"` → 0 (post-restart). The redact markers MUST be integer 0/1 always-set (NOT boolean, NOT match-only) — log_to_metric errors+drops on absent fields and float-parse-errors on booleans.
- **bearer_token floor:** the bearer pattern uses `{20,}` (not `+`) to avoid over-redacting `Bearer <word>`. If lowering the floor, confirm no English-word false positives AND no real-token under-redaction (under-redaction = secret leak).
- **rdt_* markers stripped before VL:** VL hmrun/docker records must NOT carry `rdt_*` fields (strip_markers_main/strip_markers_hmrun del() them). Query a redacted line and confirm no `rdt_` keys.
- **Audit counts-only:** `audit_log` rows with `what='logs.redaction_counts'` carry only {pattern_type:{delta,cumulative}} — NEVER matched secret values. Grep audit_log for any known secret pattern → ZERO.
