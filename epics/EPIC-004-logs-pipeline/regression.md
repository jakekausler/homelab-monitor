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

## STAGE-004-012 — Stream picker sidebar (distinct service values with line counts; click-to-filter)

- "Logs Explorer: the `/logs` page shows a left 'Services' sidebar listing distinct services with line counts (sorted desc) for the current time window."
- "Clicking a service row filters logs to that service (chip appears above the search box; results narrow); clicking a second service ORs them; chip × removes a service; the URL reflects `&services=a,b`."
- "Service filtering works in BOTH plain search and advanced LogsQL modes (the selected services AND with whatever expr the active mode produces; user's expr never mutated)."
- "Changing the time range refetches the services list/counts (counts are window-scoped, independent of expr/selection)."
- "Sidebar shows a 'Show more'/truncated affordance when the distinct-service count exceeds the limit (default 100)."
- "On mobile (≤767px), the sidebar is an overlay drawer toggled by a button; selected-service chips still show above the search box."
- "Backend GET /api/logs/services?start&end&limit returns {services:[{service,count}], truncated} sorted desc; rejects bad ISO / inverted / >30d / far-future windows; 30s cache."
- "Service values containing special chars (double-quote, backslash) filter correctly via the /api/logs/query `services` param (logsql_quote_phrase escaping) with no 500."

## STAGE-004-012A — Service source_type field + grouped/collapsible stream picker

- Vector ingest sets `source_type` on every log line: docker container logs → 'docker', systemd/journald units (with `_SYSTEMD_UNIT`) → 'systemd', cron (SYSLOG_IDENTIFIER CRON/crond or cron.service, plus hmrun) → 'cron', else 'unknown'. Cron is checked BEFORE systemd (raw cron.service journald lines classify as cron, not systemd).
- GET /api/logs/services returns one ServiceCount per (service, source_type) IDENTITY — a service name present under two source_types appears as two rows, each with its own count + source_type; sorted desc.
- The /api/logs/query `services` filter is identity-qualified: `services=<source_type>:<service>` ANDs source_type with service `(service:"x" AND source_type:"docker")`, OR'd across selections. Filtering docker:svc excludes systemd:svc lines of the same name. Special chars in either half are quoted via logsql_quote_phrase.
- The Logs Explorer stream-picker sidebar groups services into COLLAPSIBLE SECTIONS by source_type (order: docker, cron, systemd, others, unknown-last), each with a collapse toggle + per-section select-all/none (tri-state). A service under two types shows in both sections. Selection writes identity-qualified `&services=type:service` to the URL.
- SUPERSEDES STAGE-004-012: the `&services=` URL param changed from bare-name CSV (`a,b`) to identity-qualified CSV (`docker:nginx,cron:hmrun`). 012's bare-name `&services=a,b` regression expectation is replaced by the identity-qualified format.

## STAGE-004-014 — Query history (last 20 executed queries)

- Run several distinct Explorer searches; open Filter sidebar → "Recent" tab → entries appear most-recent-first with relative timestamp + query preview.
- Run the same query twice consecutively → no duplicate row (consecutive dedupe; top entry timestamp updates).
- Run 20+ distinct queries → list caps at 20, oldest roll off.
- Click a Recent entry → restores query text/services/range/mode into the Explorer and re-runs (deep-state restore).
- "Clear history" empties the list → empty state "No recent queries yet. Run a search to populate." shows.
- Reload the page → history persists (localStorage key `homelab-monitor:logs-query-history`).
- Recent and Saved tabs are independent (history entries don't appear in Saved, and vice versa).
- (Known churn note) Recording is at the writeUrl choke-point, so identity-toggle churn into history is expected v1 behavior; a skip-identity-toggles predicate is the planned mitigation if it becomes annoying.

## STAGE-004-015 — Explorer state persistence (last query / range / scroll position)

- Run a search (query + services + range + mode), navigate away to another route, navigate back to /logs → query/services/range/mode all restored.
- Run a query with many lines, scroll down substantially, navigate away + back → results pane scrolls back to roughly the saved position (NOT top). [Real-browser only — jsdom cannot test this. The scroll container is `data-log-scroll-container` within LogsExplorerBody, NOT the page-level <main>.]
- Deep-link with URL params (e.g. /logs?q=error&since=1h) while persisted state exists → URL wins (URL's query shown, persisted ignored). ALL-OR-NOTHING precedence.
- Reload the page (F5) with a query + scroll set → state + scroll survive (localStorage key 'homelab-monitor:logs-explorer-state').
- Fresh profile / cleared localStorage + no URL params → default empty Explorer state.
- TTL: persisted state older than 7 days → treated as absent → default empty (loadExplorerState returns null when last_visited_at is >7d old).
- Mobile: scroll-restore + state-restore work on the narrow/mobile layout (same shared scroll container).
- (Look-ahead) STAGE-004-018B (configurable columns → variable row heights) may invalidate the pixel scrollTop → future line-anchor restore. STAGE-004-024 (live tail auto-scroll) must suppress scroll restore while tailing.

## STAGE-004-016A — LogsQL structured field filters (Add-to-filter uses structured operators)

- **Field inspector Add-to-filter behavior per field type:**
  - `host` field: clicking "Add to filter" composes a structured LogsQL clause `host:"<value>"` into the advanced-mode query; results refilter to lines matching that host.
  - `severity` field: clicking "Add to filter" composes `severity:"<value>"` (e.g. `severity:"error"`).
  - `fields`-bag entries (e.g., `compose_service`): clicking "Add to filter" composes `<key>:"<value>"` for the bag key.
  - `stream` field: clicking "Add to filter" composes `_msg:"<value>"` substring (stream is NOT a directly queryable VictoriaLogs field; it maps from builtin `_stream_id`).
  - `message` field: clicking "Add to filter" composes `_msg:"<substring>"` (substring is the correct semantic for free-text message).
  - `service` field: clicking "Add to filter" toggles the existing identity-chip mechanism (unchanged from STAGE-016).
  - `timestamp` field: Copy button only; no Add-to-filter.

- **Multiple field filters AND together:** applying `host:"x"` then `severity:"y"` composes both into the LogsQL expr as `host:"x" severity:"y"` (space-separated AND clauses); results filter to lines matching both conditions.

- **LogsQL advanced editor — SINGLE-LINE enforcement:**
  - Pressing Enter executes the search and inserts NO newline and NO space into the editor.
  - Repeated Enter does NOT cause oscillation (no newline appears/deletes cycle).
  - Multi-line paste into the LogsQL editor is blocked (inserts nothing).

## STAGE-004-016 — Field inspector (click a line → side panel with parsed fields)

- **Field inspector interaction:** Click any log line in the Explorer → right-side panel opens showing that line's fields. Click the same line again OR click the panel × → closes. Click a different line → panel swaps contents (no close/reopen flicker). Inspected line is highlighted.
- **Field inspector opt-in:** Feature enabled in Explorer via `<LogViewer fieldInspectorEnabled={true} />`. Docker and Cron log viewers do NOT show click-to-inspect affordances (no behavior change for those viewers; LogViewer receives no onLineClick/isSelected when not opted in).
- **Empty-string / whitespace-only / null / undefined field values omitted:** Field inspector rows render only for fields with non-empty values. Omit rows where `raw == null || (typeof raw === 'string' && raw.trim() === '')`. Zero, false, and non-empty-string values remain visible (tested with all canonical severity values, 0-valued metrics, false booleans).
- **Copy button:** Click Copy on any field value → copies that value to clipboard + toast "Copied" (or "Copy failed" on navigator.clipboard absence). Works for all field types (strings, booleans, numbers, 0, false).
- **Add-to-filter buttons:**
  - On `service` field: adds an identity chip, handles toggle via existing selectedIdentities state + onToggleIdentity callback.
  - On `host`, `severity`, `stream`, `message`, and any `fields[*]` dict entry: appends `_msg:"<value>"` to the current query via appendMsgFilter helper; routes through writeUrl(false, …) so it respects persistence + history.
  - On `timestamp` field: Copy button only (no add-to-filter).
- **Desktop layout (>767px):** Right `<aside>` inline push, mirroring the left Filter sidebar; LogsExplorerBody flex row reflows when panel opens/closes. Scroll persistence targets `data-log-scroll-container` (STAGE-015 re-pointed).
- **Mobile layout (≤767px):** Right-side Sheet overlay; selected line highlighted in the background logs.
- **Independent scroll containers:** Filter / Logs / Inspector panels each scroll independently to viewport bottom (new `data-log-scroll-container` wrapping LogViewer's results pre). App header + each panel header (filter tabs row, logs control bar, inspector header with line details + ×) stay static while content scrolls. Enabled via opt-in `fillHeight` prop on LogViewer (default false; non-opted viewers [Docker, Cron] keep page-level scroll).
- **Regression: Docker container log viewer + Cron run log viewer:** Both still render through shared `<LogViewer>` primitive; still scroll at page level (LogViewer's `fillHeight` not opted in). Click-to-inspect not wired (onLineClick/isSelected not passed). No visual or behavior change from STAGE-004-003.
- **Line selection state:** Line identity = `${line.timestamp}-${index}` (timestamp + message alone not unique; index disambiguates within current render). Selection may drift/clear on "Load older" — acceptable for a transient selection.

## STAGE-004-016B — JSON message drill-down in field inspector (recursive collapsible tree)

- **Field inspector: JSON message rendering:** Click a log line in the Explorer whose `message` is valid JSON (e.g., a collector log like `{"collector":"docker_socket","event":"scheduled","level":"info","tick_id":"abc"}`). The message field renders as a COLLAPSIBLE TREE (NOT a flat string), with the top-level object/array nodes EXPANDED by default and deeper levels collapsed. Tree nodes show their keys (for objects) or indices (for arrays) and are expandable/collapsible via chevron toggles (aria-expanded).
- **Field inspector: JSON tree interaction:** Clicking a chevron toggle expands/collapses that node's children; toggled state persists within the current inspector panel session (toggling the same node multiple times retains the latest open/close state). Nested objects/arrays render as further-nested rows with indentation.
- **Field inspector: JSON tree copy:** The message field's Copy button copies the PRETTY-PRINTED JSON (`JSON.stringify(parsed, null, 2)`, with newlines and indentation), not the original raw string. Toast reads "message copied".
- **Field inspector: non-JSON message fallback:** A message field containing plain text (non-JSON or JSON primitives like numbers or quoted strings) renders as flat text exactly as before — NO tree, NO special formatting. JSON array messages render a tree but suppress nothing (see suppression rule below).
- **Field inspector: bag-row suppression for JSON objects:** When the message is a JSON OBJECT, the field inspector suppresses any bag rows (`fields` dict entries) whose keys match TOP-LEVEL keys in the parsed JSON. Example: a message `{"collector":"docker_socket","event":"scheduled","level":"info"}` has top-level keys {collector, event, level, info}; if the bag contains rows for `fields.collector`, `fields.event`, `fields.level`, those rows are hidden (the tree shows them already). Non-matching bag rows remain visible. **NO suppression occurs for JSON arrays** (arrays have no named keys to conflict). **Core fields NEVER suppressed:** timestamp, severity, service, host, stream always appear as separate rows regardless of message content.
- **Field inspector: JSON tree depth/size caps:** Very large or deeply-nested JSON does not hang or crash. Caps: max depth 10, per-container child cap 1000 (elements beyond 1000 collapse to "… (M more)" indicator), global node budget 5000 (deep subtrees may truncate). Over-limit subtrees render a truncation indicator instead of full expansion, but do not crash the inspector.
- **Field inspector: Add-to-filter in tree mode:** No per-leaf "Add-to-filter" buttons on tree nodes. Leaf/subtree copy DEFERRED (out of scope); only the entire-tree Copy affordance is present.

## STAGE-004-017 — Generic nested-field extraction at ingest (full nested JSONL → flat fields)

- "Nested JSON flattening: run `make integration`; `test_vector_nested_json_extraction.py` must PASS — a planted nested-JSON line surfaces with `json.context.user_id` / `json.context.request.path` / `json.context.request.latency_ms` flattened fields via `/api/logs/query`."
- "Vector pipeline integrity: `test_vector_to_vl_path.py` + `test_vector_multiline.py` (11) must PASS under `make integration` (json_flatten transform must not break the docker_enrich → json_flatten → docker_severity_extract chain)."
- "Render: `make verify` must show the new template structural tests green (transforms.json_flatten present, type=remap, inputs=[\"docker_enrich\"], docker_severity_extract.inputs=[\"json_flatten\"]) and render tests for the `${HOMELAB_MONITOR_LOG_JSON_MAX_DEPTH}`/`_MAX_FIELDS` substitutions (defaults 8/100)."
- "Prod render (manual, on Vector/template change): `make dev-prod` → monitor logs `vector.render.success`, Vector container boots with no VRL compile error, rendered `/var/vector-config/vector.toml` `[transforms.json_flatten]` shows caps as integer literals; real container JSON shows `json.*` dotted fields in `/api/logs/query`. Tear down with `make dev-down`."
- "Integration query window: integration tests must keep `end = now.isoformat()` (NOT future) or `/api/logs/query` returns 400 `range_in_future` (5s future-skew grace)."

## STAGE-004-018 — Filter-scope-aware field discovery (Available fields panel, sample-based)

- "Scope-aware field discovery: in the Logs Explorer, open the 'Fields' sidebar tab with a filter set (e.g. a service over 1h) → panel lists only fields in that scope, each with a coverage% badge, type hint, and sample-value chips. `make verify` must keep `FieldsDiscoveryPanel.test.tsx` + `test_logs_fields.py` + `test_api_logs_fields.py` green."
- "Endpoint: `GET /api/logs/fields?expr=&start=&end=&services=&sample_n=` returns `{fields:[{name,sample_values,coverage,type_hint}], sampled_lines, truncated}`, coverage = field.hits/_msg.hits (exact), 2 fixed VL calls (field_names + bounded query), 30s cache, 502 on VL error, sample_n clamp 1..2000, builtins (_msg/_time/_stream_id) excluded."
- "One-click injection: clicking a sample-value chip adds a `field:\"value\"` filter via appendFieldFilter and refreshes results."
- "Type-hint inference (`infer_type_hint`): numeric/bool/object/array/string/mixed/unknown — keep the unit table green (incl. `['true','5']` → mixed, empty → unknown)."
- "Prod-render smoke (manual, on UI change): `rm -rf apps/ui/dist && make dev-prod`, confirm served bundle hash changed + contains `data-testid=\"fields-discovery\"`; `GET /api/logs/fields` 200 with real data. Tear down `make dev-down`."

## STAGE-004-019 — Histogram of line counts (stacked-by-severity bar chart above results)

- "Histogram density: in the Logs Explorer, a stacked-by-severity bar chart appears above results (red=error+, yellow=warn, gray=info); buckets scale with range; `make verify` must keep `HistogramChart.test.tsx` + `test_logs_histogram.py` + `test_api_logs_histogram.py` green."
- "Endpoint: `GET /api/logs/histogram?expr=&start=&end=&services=&buckets=` returns `{buckets:[{start_ts,counts_by_severity:{error,warn,info},total}], bucket_duration_ms}`; exactly N start-aligned buckets (re-binned from VL's epoch-aligned `/hits` timestamps); coarse severity via the public `normalize_severity`; 30s cache; buckets clamp 1..500 → 422; 502 on VL error."
- "VL `/hits` field-grouping (MANDATORY — mocks can't prove it): `make integration` → `test_histogram_hits.py` must PASS, confirming real VL v0.30.0 `/select/logsql/hits?field=severity` returns per-severity grouped series. If this breaks (VL upgrade changes `/hits` behavior), the histogram loses its severity stack."
- "Click-to-narrow: clicking a bar narrows the range to `[bucket.start_ts, +bucket_duration_ms)` and refreshes results (via writeUrl)."
- "Bucket math: `compute_step_ms`/`assign_bucket` keep the unit table green (ts==start→bucket 0, ts==end→last bucket, epoch-aligned VL timestamps re-binned, span-not-divisible). `end` is INCLUSIVE in VL v0.30.0."
- "Prod-render smoke (manual, on UI change): `rm -rf apps/ui/dist && make dev-prod`, confirm served bundle contains `data-testid=\"histogram-chart\"`; `GET /api/logs/histogram` 200 with per-severity buckets. Tear down `make dev-down`."

## STAGE-004-020 — Log-line export (Download matching lines as .txt or .json with streamed backend + cap)

- "Logs Explorer: an 'Export' button appears in the control bar (Row C, after Save Query). Clicking it opens a dialog with a txt/json format radio (default txt), a max-lines numeric input (default 10000, min 1, max 100000, enforced by clamp), and Download/Cancel buttons. testids: `logs-export-button`, `logs-export-modal`, `export-format-txt`, `export-format-json`, `export-max-lines`, `export-download-button` all present in the served bundle."
- "Download triggers a hidden anchor navigation to `/api/logs/export?expr=<expr>&start=<ISO>&end=<ISO>&format=txt|json&max=<N>&services=<csv>` with cookie-auth GET; browser downloads the file. URL omits `services` param when no service filter selected."
- "GET /api/logs/export (authenticated) accepts params: `expr` (LogsQL, >4096 chars → 400), `start`/`end` (ISO, validated window, invalid/future/>30d → 400), `format` (txt|json, default txt, invalid → 422), `max` (integer 1..100000, default 10000, out-of-range → 422), `services` (CSV). Reuses `parse_and_validate_window` + `_compose_services_expr`."
- "GET /api/logs/export?format=txt returns HTTP 200, Content-Type `text/plain; charset=utf-8`, Content-Disposition `attachment; filename=\"logs_YYYY-MM-DD_HHmmss.txt\"` (UTC timestamp). Body: up to `max` lines, each formatted `<timestamp> [<severity>|unknown>] <service|empty>: <message>`. Empty result is an empty body (HTTP 200, 0 bytes). Cap enforced: max=1 returns ≤1 line, max=100000 is the hard limit."
- "GET /api/logs/export?format=json returns HTTP 200, Content-Type `application/json`, Content-Disposition `attachment; filename=\"logs_YYYY-MM-DD_HHmmss.json\"`. Body: valid JSON array of up to `max` `LogLine` objects (comma-separated, no trailing comma). Empty result is `[]` (HTTP 200). Cap enforced: max=1 returns ≤1 object, max=100000 is the hard limit."
- "Error cases: format=csv (or invalid format) → 422 with `detail`. max=0 or max=100001 (or invalid int) → 422. expr >4096 chars → 400 `invalid_expr`. Invalid/unparseable start/end, future window, >30d span → 400 from `parse_and_validate_window`. Upstream VL errors (non-200, transport failure) → 502 `upstream_unavailable` (caught in pre-flight sentinel before streaming starts)."
- "Streaming: backend streams the response line-by-line (O(1) memory), never buffering the whole result. Mid-stream VL failures (rare, after 200 response) truncate the download (accepted streaming limitation, no retry)."
- "`make verify` must pass: backend tests `test_logs_export.py` (formatters: txt/json framing, None severity/service placeholders, empty results, cap enforcement), `test_api_logs_export.py` (happy paths + all error codes, headers, authenticated/401, services composition), `test_victorialogs_client.py` (new `stream_query` method + blank/malformed line handling); UI tests `ExportButton.test.tsx` (buildExportUrl clamping/omit-services/URL-correctness, dialog render/open/Download). 100% kernel coverage required."
- "Prod-render smoke (manual, on UI change): `rm -rf apps/ui/dist && make dev-prod`, confirm served bundle contains testids; curl test `GET /api/logs/export?expr=*&start=<ISO>&end=<ISO>&format=txt&max=10 | head` returns partial txt with Content-Type headers. Tear down `make dev-down`."

## STAGE-004-021 — "Open in Explorer" deep-link from Docker + Cron viewers

- "Docker container log viewer: an 'Open in Explorer' button (data-testid=open-in-explorer) appears in the header actions row. Clicking it SPA-navigates to /logs with the query pre-filled to `service:"<container-name>"` and the viewer's current time range (preset → since; custom → start/end). Verified on prod rig (127.0.0.1:29090) with fresh bundle; button present in production assets."
- "Cron run log viewer: same button; clicking navigates to /logs pre-filled with `cron_fingerprint:"<fp>" AND run_id:"<run-id>"` and the run's window (user-narrowed search.start/end → runMin..runMax ±1s → running-run open-ended → 1h preset fallback). Navigation is SPA (TanStack <Link>) — no full page reload."
- "buildExplorerUrl (apps/ui/src/lib/explorerLink.ts) emits only the keys in EXPLORER_URL_KEYS (logsql/q/since/start/end/services) — these MUST remain a subset of the /logs route's validateSearch accepted keys (router.tsx). The guard test in explorerLink.test.ts fails if they drift."
- "Deep-link contract verified: /api/logs/query accepts both `service:"..."` Docker and `run_id:"..."` Cron expressions returning 200. /logs SPA route accepts query params and opens Explorer with pre-filled query + range applied; curl testing confirmed pre-filled params are preserved in rendered page."

## STAGE-004-023 — Backend SSE endpoint for live tail (server-side streaming from VL)

- `GET /api/logs/tail?expr=<LogsQL>&services=<csv>` returns `text/event-stream`; new VL log lines appear as `event: line` SSE events within ~1s latency (true end-to-end streaming with real VL v0.30.0).
- Connection cap: opening more than HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS (default 5) concurrent tails → 6th returns HTTP 503 + `Retry-After: 60`.
- Structural-bad LogsQL (e.g. `expr=|limit 10`) → HTTP 422 `invalid_logsql`; phrase-garbage (e.g. `expr={{{{x`) → 200 empty stream (VL treats it as a phrase filter — matches VL's real contract, no client-side LogsQL validation).
- VL unavailable on the pre-flight probe → 502 `upstream_unavailable` (before the stream opens).
- Metrics emitted: `homelab_log_tail_active_connections` (gauge), `homelab_log_tail_lines_streamed_total`, `homelab_log_tail_lines_dropped_total`, `homelab_log_tail_errors_total{kind}`.
- Integration test: `make integration` runs `apps/monitor/tests/integration/test_logs_tail_integration.py` (4 scenarios) against the real VL rig. Clean rig volumes first: `docker compose -f deploy/compose/docker-compose.test.yml down -v` (a stale `shared_rig_secrets`/`rig` token can break bootstrap with a UNIQUE-constraint error).

## STAGE-004-024 — Frontend tail mode (Explorer consumes SSE; toggle button + windowed pager)

- Logs Explorer "Live tail" toggle button: clicking turns it GREEN (the only live indicator; no status bar); already-loaded historical lines are kept and live lines append below; auto-scroll-to-bottom sticky (scroll up disengages → "Resume auto-scroll" button appears; scroll to bottom re-engages).
- Stop tail (click the green toggle again): streamed lines stay frozen on screen; the custom end time is set to the stop-moment; reverts to historical paginated view.
- Bidirectional 1000-line windowed pager (applies to normal browsing too): "Load older" (top) trims newest off the bottom over 1000 + turns OFF tailing; "Load newer" (bottom, hidden while tailing) trims oldest off the top over 1000; when an end is trimmed a "…lines removed" banner shows at that end (top=older, bottom=newer).
- Tail errors (503 over-cap / 422 invalid LogsQL / 502 VL down) surface as an inline error message below the controls (no status bar).
- Known limitation: "Load newer" returns the latest ~500 lines in [newestShown, end]; if >500 new lines accumulated, the oldest of the gap are skipped (converges via repeat Load-newer or tail).
- Frontend tests: `apps/ui/src/lib/__tests__/useWindowedLogs.test.ts` (windowed FIFO reducer), `apps/ui/src/lib/logsTail.test.ts` (slim EventSource hook), `apps/ui/src/routes/logs/__tests__/LogsExplorerPage.test.tsx` (page integration). Manual: prod rig `make dev-prod` (rm -rf apps/ui/dist first), test Desktop + Mobile in the Logs Explorer.

## STAGE-004-022 — Global retention settings UI (VL retention + disk thresholds)

- "Sidebar Settings entry + /settings navigation: SidebarNav "Settings" entry (previously disabled "Coming soon") is enabled → clicking navigates to `/settings` (parent route). The /settings route redirects to `/settings/logs`. Navigation works via SPA TanStack Link — no full page reload."
- "Settings → Logs page renders: `/settings/logs` route loads SettingsLogsPage. Page displays three Card-based sections: Retention (current + source + pending + restart-required banner + numeric input + Save) + Disk usage (used GB + % vs budget, warn/crit coloring) + Per-stream-caps info card."
- "GET /api/settings/logs/retention (authenticated) returns: {retention_days (effective), pending_retention_days (null if none), disk_used_pct, disk_used_gb, warn_pct (70), crit_pct (85), retention_source ('env'|'runtime'|'default'), restart_required (bool)}. Unauth request → 401."
- "Retention source display: 'Current: 30 days (source: env)' / '(source: runtime)' / '(source: default)'. Source chip reflects the effective retention origin."
- "Pending retention display: When a runtime override exists and differs from effective, 'Pending: 14 days' appears below current; when pending == effective or no override set, pending row hidden. Numeric input clamps display to pending value if set, else effective."
- "PATCH /api/settings/logs/retention {retention_days} (authenticated): validates 1≤days≤365 (out-of-range → 422 with detail). Persists runtime override to SQLite app_settings kv table. Returns updated GET response with restart_required=true (VL retention is startup-only, so no live-apply). PATCH back to the effective value clears the pending override (restart_required=false)."
- "Restart-required banner: Appears when restart_required=true in GET response — styled warning box ('⚠ Restart required for change to apply'). Disappears when user PATCHes back to effective value (or if no override pending). User must manually `docker compose up -d --force-recreate victorialogs` to apply the change."
- "Disk usage card: displays 'Used: 0.32 GB (2.13% of 15 GB)' and 'Warn at 70% / Crit at 85%'. Text color reflects usage level: normal (gray) at <70%, warning (orange/yellow) at 70–84%, critical (red) at ≥85%. Disk budget is computed as `sum(disk_used_bytes{slot=vl})` / `disk_budget_bytes{slot=vl}` from self_disk collector metrics (loaded via load_disk_budget_config, default 50GB total with vl_ratio=0.30 → 15GB VL budget)."
- "Per-stream-caps info card: displays 'Configured in homelab-monitor.yaml — edit file + restart to change.' Read-only informational card (no UI controls)."
- "Numeric input validation (frontend): input field clamped 1–365 via min/max HTML attributes. Typing outside range → input shows clamped value. Submit disabled until input differs from effective value (no pointless saves)."
- "Save button behavior: Click Save with a new retention_days value → triggers PATCH. On success (200), pending-retention banner updates to show restart_required=true (if changed) or clears if value == effective. On 422 (out-of-range) or 401 (unauth), toast error message displays; input state unmodified. On 500, toast error + detail; retry available."
- "Mobile (≤767px) layout: Retention/Disk/Per-stream cards stack vertically. Numeric input + Save button layout reflows for narrow viewport. Section headings + Card structure preserved."
- "Migration 0032 (app_settings kv table): SQL table with columns (key TEXT PRIMARY KEY, value TEXT, updated_at ISO TEXT). Applies at boot under AUTO_MIGRATE=1. Repository (AppSettingsRepository) provides get/set/delete/upsert; used by vl_retention to persist runtime override under key `'vl_retention_days'`."
- "Prod-render smoke (manual, on UI change): `rm -rf apps/ui/dist && make dev-prod`, confirm served bundle contains `data-testid=\"settings-logs-page\"` and `data-testid=\"retention-save\"`; curl test `GET /api/settings/logs/retention` (authenticated via session) returns 200 with all 7 fields. Tear down `make dev-down`."
- "`make verify` must pass: backend tests `tests/test_vl_retention.py` (reconcile/resolve/persist/disk logic), `tests/test_api_settings_logs.py` (GET default/env/runtime sources, PATCH validates/persists/restart-required, unauthenticated 401, out-of-range 422), `tests/test_db_migrations.py` (app_settings table present + schema correct); frontend tests SettingsLogsPage.test.tsx (card render, input clamp, Save calls), settingsLogs.test.ts (GET/PATCH hooks success+error), SettingsLayout.test.tsx (heading + Outlet). **100% backend kernel coverage required** (vl_retention.py, settings_logs.py, app_settings_repository.py all 100%)."
