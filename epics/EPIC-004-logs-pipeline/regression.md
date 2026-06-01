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

## STAGE-004-007 — Cursor pagination

- **Cursor pagination — no data loss at same-ns boundaries (CRITICAL):** paging a window where lines share an exact `_time` (ns) must NOT drop or duplicate lines. VL's `limit=N` truncates mid-same-ns-group; the `[GROUP-COMPLETE]` branch in `paginate_older` (pagination.py) re-queries the full group at the boundary ns so a page never splits a group. Regression test: `tests/test_pagination.py::test_paginate_mid_group_truncation_no_data_loss`. If pagination ever drops lines, check this branch + that `has_more` uses `result.truncated` (NOT `len(fetched)==fetch_limit`).
- **VL `end` bound is INCLUSIVE** at ns precision (confirmed against the binary). The cursor's `effective_end = _ns_to_iso(t)` relies on this. If VL behavior changes to exclusive, the fix is `_ns_to_iso(t + 1)` (noted inline in pagination.py).
- **"Load older" PREPENDS (oldest-first display):** viewers must flatten infinite-query pages in REVERSE page order (`pages.slice().reverse().flatMap(p => p.lines)`) so older pages render at the TOP. Tests: `renders older pages above newer pages in multi-page load` in DockerContainerLogsViewer.test.tsx + CronRunLogViewer.test.tsx. status/truncated/header derive from `pages[0]` (newest).
- **Pagination is additive — short content shows no "Load older":** a viewer whose content fits in one page must NOT show the button (`next_cursor=null`/`hasMore=false`). Verify both a >page_size viewer (button appears, paginates, disables on exhaustion) and a ≤page_size viewer (no button).
- **Cron run log viewer migrated to `<LogViewer>`:** the cron viewer renders via the shared component; verify badges/anomaly-flags/duration/running-refresh/expired-notice/metadata-header still present after any LogViewer change.
- **OpenAPI regen after response-shape changes:** the 3 log endpoints carry `cursor` param + `next_cursor`/`has_more`. After any change, `make openapi-export` + `bash scripts/generate-ui-types.sh`.

## STAGE-004-008 — Custom datetime range picker

1. **Custom datetime range — validation:** On the docker container logs viewer, open the time-range control → Custom range. Verify validation rejects (inline error, no apply): start ≥ end; a future-dated provided bound; span > 30 days. Verify a valid past window reloads logs and the URL gains `?start=…&end=…`.
2. **Custom range — open bounds:** Leave end empty → label shows "… → Now", logs load up to present, Refresh extends the window. Leave start empty → "Earliest → …" (resolves to now−30d). Both empty → "last 30 days up to now". URL reflects only provided bound(s).
3. **Docker endpoint start/end (backend):** `GET /api/integrations/docker/containers/{name}/logs?start=<ISO>&end=<ISO>` returns 200 with the requested window; start≥end/bad-ISO/>30d/future → 400; both since and start/end → 422; partial pair → 422; bare call → 200 (default 15m). Shared with /api/logs/query via `parse_and_validate_window`.
4. **Cron run log — bounded narrowing:** On a cron run log viewer with a long run, the bounded time-range control narrows displayed lines (client-side filter) within [run start, run end]; open bounds fall back to the run's edges; cannot exceed the run window.
5. **Preset compatibility:** All 6 presets (5m/15m/1h/6h/24h/7d) still work on docker viewer and set `?since=…`. Clearing both custom bounds on docker reverts to the 15m preset (intended).
6. **KNOWN FLAKY (pre-existing, not STAGE-008):** `apps/monitor/tests/test_api_cron_events.py::test_bmode_two_cmds_exit_closes_most_recent` intermittently fails with `assert 'unknown' == 'running'` (B-mode cron run-state timing). Observed during STAGE-008 Refinement: failed once, passed on unchanged re-run. Unrelated to the logs-pipeline work (no cron-event code touched). Flagged for a future dedicated fix; re-run verify if it fails.

## STAGE-004-009 — Local-time timestamp rendering with UTC toggle

1. **Local-time timestamp rendering:** Open a log viewer (docker container logs or cron run log). By default, timestamps render in configured local time (America/New_York) as `YYYY-MM-DD HH:MM:SS EDT/EST` (seconds only, no milliseconds; zone label reflects DST). NOT browser-local, NOT UTC by default.
2. **UTC toggle:** The "UTC" toggle in the viewer header flips ALL timestamps (per-row AND the docker "Last:" header timestamp) to `YYYY-MM-DD HH:MM:SS UTC` and back. Both surfaces convert together.
3. **Tooltip (other format):** Hovering a timestamp shows the OTHER format via native title tooltip (hover a local stamp → see the UTC equivalent, and vice versa). Both formats are seconds-only (no ms).
4. **Persistence:** Toggle to UTC, navigate away and back (and across docker↔cron viewers) — the preference persists (localStorage `homelab-monitor:timezone`, shared across all log viewers).
5. **Format consistency:** Local and UTC formats both show seconds only (no milliseconds). `formatLogTimestamp(raw)` with no opts still returns the UTC fast-path (back-compat for non-viewer callers).
6. **Downstream:** The Logs Explorer (STAGE-004-010) consumes `<LogViewer>` and inherits this toggle automatically — no extra timezone work there.

## STAGE-004-010 — Logs Explorer skeleton (/logs)

- Navigate to `/logs` (sidebar "Logs" entry) → search box + TimeRangeControl + LogViewer render.
- First load (no params) shows last-1h match-all (`*`) recent lines.
- Type a whole-word term present in logs (e.g. `zigbee2mqtt`) + Search → results filter (translated to `_msg:"<term>"`).
- Clear (×) button stays visible while a committed filter is active, even after the input is emptied; clicking it returns to match-all.
- "Load older" paginates (cursor pagination from STAGE-004-007); UTC + Wrap toggles work.
- Deep-link `/logs?q=connection%20refused&since=24h` reproduces the view; custom range via `?start&end`.
- Empty search omits `?q`. Whole-word matching (substrings do not match).
- Unit/route tests: logsQlTranslate.test.ts, LogsExplorerPage.test.tsx.

## STAGE-004-011 — LogsQL advanced mode + syntax highlighting

- `/logs` → "Advanced (LogsQL)" toggle swaps the plain input for a CodeMirror editor with basic syntax highlighting (keywords/fields/strings/numbers/operators).
- CodeMirror editor: visible caret; clicking anywhere in the box positions the cursor; focus ring on focus; Enter submits, Shift+Enter inserts newline.
- Toggling modes preserves BOTH the plain-text and LogsQL text.
- Advanced mode sends the raw LogsQL as `expr` (NOT `_msg:"…"`-wrapped); plain mode still translates.
- URL: advanced → `?logsql=…` (omit `q`); plain → `?q=…` (omit `logsql`). Deep-link `/logs?logsql=service%3Afoo` opens advanced mode + queries raw.
- Mobile (narrow viewport): editor falls back to a plain textarea (no CodeMirror), Enter submits.
- Editor lazy-loads as a separate chunk (LogsQlEditorImpl) only when advanced is first enabled on desktop.
- Clock-skew: a 1h-preset query with end==now must return 200 (not range_in_future); end=now+1h must still 400. (Regression for the STAGE-011 Refinement clock-skew fix.)
- Backend: `apps/monitor/tests/test_time_window.py` covers the 5s FUTURE_SKEW_GRACE boundary (within-grace allowed, beyond-grace rejected).
