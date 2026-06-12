# Regression Checklist - EPIC-005: Home Assistant

(Items added per stage during Refinement.)

## STAGE-005-001 (HA REST client)

- **STAGE-005-001:** With the prod rig up and `ha_token` set, the HomeAssistantRestClient must reach real HA: `get_config()` returns a non-empty HA version + time_zone (not an HaError); a bad/missing token yields `HaError(reason="auth")` (HTTP 401) with the token never appearing in the error message. `load_ha_config()` reads `HOMELAB_MONITOR_HA_URL` (default `http://192.168.2.148:8123`). Validate by constructing the client inside the monitor container (no HA collector/endpoint exists until later stages).

## STAGE-005-002 (HA websocket client)

- **STAGE-005-002 (HA websocket client):** With the prod rig up and `ha_token` set, the `HomeAssistantWebsocketClient` must reach real HA `/api/websocket`: the auth handshake completes (`connected` becomes True), `send_command("get_config")` returns the HA version (not an HaError), and `subscribe("subscribe_events", event_type="state_changed")` yields at least one real event. `homelab_ha_websocket_connected` gauge=1.0 + `homelab_ha_websocket_reconnect_total>=1` after connect. `stop_task()` stops cleanly with no hang. Validate via an in-container async snippet (no WS collector/endpoint until stages 010/011/012). **Constraint:** never use a WS `get_states` command for bulk entity fetch — it exceeds HA's 1MB frame limit (1009 close); use the REST client's `get_states()` instead.

## STAGE-005-003 (HA integration bundle)

- **STAGE-005-003 (HA integration bundle):** With a backend booted via the REAL lifespan, `GET /api/collectors` must list the `ha_up` collector (`interval_seconds=30`, `concurrency_group="homeassistant"`), proving the bundle's `register_all(loader)` is wired into lifespan. After a tick, `homelab_ha_up` appears in the metrics snapshot (0.0 when no `ha_token`, 1.0 when HA reachable) and `homelab_collector_run_success_total{name="ha_up"}` increments (the probe is `ok=True` even when HA is down — the metric carries the down signal). The `_per_test_db` test fixture (conftest.py) registers the HA bundle so the test app mirrors production — any new HA collector added to `_HA_COLLECTORS` flows into both automatically. **Copyability invariant:** a new integration = a directory of one-class collector modules + `__init__.py` exposing `register_all` + one lifespan line; a new collector within a bundle = one module + two lines (`from .x import X`; append `X` to the bundle's collector list).

## STAGE-005-004 (Reusable cardinality cap)

- **STAGE-005-004 (Reusable cardinality cap):** Run the cardinality-cap unit suite: `make uv ARGS="--directory apps/monitor pytest tests/test_cardinality.py"` — all CardinalityCapper + CappedEmitter tests pass. Cap mechanism invariant: feeding > cap distinct label-sets for a family to `CappedEmitter.emit_family(family, cap, observations)` must (a) write exactly `cap` survivors to the MetricsWriter, (b) keep the SAME survivors across repeated calls with reordered input (deterministic, no flapping), (c) emit `homelab_metric_family_dropped_series{family}` = dropped count (0 when under cap), and (d) append exactly one `SuggestionEvent(severity="warning")` per over-budget family. Config: `load_cardinality_caps_config().cap_for(family)` returns the per-family YAML override or the `cardinality_caps.default` (500), with `HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT` overriding the default. When the first consumer (entity-availability collector, STAGE-005-006) lands, confirm the cap sits BEFORE the MultiplexMetricsWriter fan-out so dropped series reach neither the snapshot sink nor the VM scrape.

## STAGE-005-005 (User-authored MetricsQL alert-rule machinery + Alerts page)

- User-rule backend suite: `make uv ARGS="--directory apps/monitor pytest tests/test_expr_validate.py tests/test_log_user_rules_repo.py tests/test_log_user_rules_api.py tests/test_api_metric_names.py"` — all pass.
- MetricsQL authoring: from Alerts → Manage Rules → "New rule" (data-testid user-rules-new), the Rule type selector is ENABLED (logsql/metricsql); selecting Metrics + Simple shows metric (autocomplete from /api/metrics/metric-names) + comparison + threshold fields; Advanced shows a plain MetricsQL textarea (NOT the LogsQL CodeMirror) with no "Uses LogsQL" link.
- Validation floor: a bare metricsql selector (e.g. `up` with no comparison) is rejected; `up > 0`, `absent(up)` accepted.
- `error` severity selectable end-to-end (info/warning/error/critical).
- Audit: each user-rule create/patch/delete/enable/disable writes an audit_log row.
- Backend `GET /api/metrics/metric-names` proxies VM `/api/v1/label/__name__/values`, returns `{names:[...]}`, 502 on VM unreachable, session-required.
- Alerts page: `/alerts` redirects to `/alerts/active` (Karma); `/alerts/manage` hosts rule management; the Logs page has NO "Alert Rules" tab.
- End-to-end: a created metricsql rule renders to the `user-rules-metrics` vmalert group (`/etc/vmalert/metrics-user/<name>.yaml`), loads healthy in vmalert-metrics, and fires when its expr matches.

## STAGE-005-006 (Entity-availability collector)

- Collector suite: `make uv ARGS="--directory apps/monitor pytest tests/test_ha_entity_available_collector.py"` — all pass.
- `ha_entity_available` collector polls `/api/states`, emits `homelab_ha_entity_available{entity_id,domain}` (1.0 real / 0.0 unavailable/unknown/empty) + `homelab_ha_entity_last_changed_seconds{entity_id,domain}` (now−last_changed, ≥0), applies the 005-004 cardinality cap. Domain allow-list (`DEFAULT_DOMAIN_ALLOW`) is first-line control; cap is safety net.
- Built-in default cap for the two HA entity families is **2500** (not 500) via `_DEFAULT_CARDINALITY_FAMILIES` in `kernel/config.py`; user YAML `cardinality_caps.families.*` overrides per-family, global `default` stays 500. Verify: `load_cardinality_caps_config().cap_for("homelab_ha_entity_available") == 2500` with no YAML.
- Real-HA behavior: on a ~2000-entity HA, the domain filter + 2500 cap emit the full filtered set with 0 drops + 0 suggestions; an install >2500 filtered entities fires the over-cap suggestion (raise cap or narrow filter).
- Unparseable `last_changed` → availability still emitted, staleness skipped, `homelab_ha_entity_last_changed_parse_errors{}` counts it (only when >0). HaError / ctx.ha None → failed CollectorResult, no crash.
- Both `ha_up` + `ha_entity_available` registered in the HA bundle (`register_all`).

## STAGE-005-007 (Battery-level collector)

- `HaBatteryCollector` emits `homelab_ha_battery_level{entity_id,domain}` (percent 0-100) for HA
  entities with `device_class=="battery"` AND `unit_of_measurement=="%"`.
- Battery-classed entities with non-`%` unit (e.g. `binary_sensor.*_battery_low`) must be EXCLUDED.
- Unavailable/unknown/non-numeric battery entities must be SKIPPED (not emitted as 0).
- Always-emitted drop gauge `homelab_metric_family_dropped_series{family="homelab_ha_battery_level"}`.
- Real HA battery cardinality ~25 (well under 500 cap). Verify via prod rig: collector run `ok=True`,
  metric live in VM.
- `_shared.py` helpers (`extract_domain`, `get_states_or_error`, `parse_float_state`) shared by 006 +
  007 — 006 (entity-availability) behavior must remain byte-identical.

## STAGE-005-008 (Update-availability collector)
- `HaUpdateCollector` emits `homelab_ha_update_available{entity_id,title}` = 1.0 (state "on" = update
  available) / 0.0 (state "off" = up-to-date) for `update.*` entities. EMIT the 0s (stable denominator).
- Unavailable/unknown update entities must be SKIPPED (not emitted as 0).
- NO version labels (installed_version/latest_version are panel-only per Option C — 021's concern).
- `title` label from attributes, defaults to "" when missing/non-str (real HACS entities have
  title=None → title="").
- Always-emitted drop gauge `homelab_metric_family_dropped_series{family="homelab_ha_update_available"}`.
- Per-family cap 150 in `_DEFAULT_CARDINALITY_FAMILIES`. Real HA update cardinality ~106 (under cap).
- Verify via prod rig: collector run `ok=True`, ~41 on / ~55 off, metric live in VM.

## STAGE-005-009 (Automation/script run-cadence collector)
- `HaCadenceCollector` emits `homelab_ha_automation_last_triggered_seconds{entity_id}`,
  `homelab_ha_script_last_triggered_seconds{entity_id}` = max(now − last_triggered, 0), and
  `homelab_ha_automation_enabled{entity_id}` = 1.0 (on) / 0.0 (off).
- Never-triggered (null/missing last_triggered) → SKIPPED (no series, NOT a parse error).
  Present-but-unparseable → counted in `homelab_ha_cadence_last_triggered_parse_errors`.
- Scripts get last_triggered ONLY (no enabled metric).
- NO threshold/expected-cadence config in the collector (015's concern — rule YAML + 005-005 user rules).
- ISO parsing via shared `parse_iso_or_none` in `_shared.py` (006 refactored onto it — behavior
  byte-identical, 006 tests green).
- Real HA: ~80 automations + ~20 scripts (under 500 cap). Verify via prod rig: collector ok=True,
  null-triggered skipped with 0 parse errors, all 3 families live in VM.

## STAGE-005-010 (Config-entry state collector — websocket)
- `HaConfigEntryCollector` emits `homelab_ha_config_entry_loaded{domain,title}` (1=loaded/0) +
  `homelab_ha_config_entry_setup_error{domain,title}` (1 iff state ∈ {setup_error,setup_retry,
  migration_error,failed_unload} / 0). not_loaded/setup_in_progress/unknown → both 0.
- First WS collector: `self._ws` injected by lifespan (app.state.ha_ws_client); per-tick
  `send_command("config_entries/get")` snapshot (NO subscription). _ws None/not-connected/HaError →
  ok=False. reason panel-only (not a label).
- WS-client contract (STAGE-005-002 fix from this stage): `send_command`/`_result_payload` pass through
  LIST results (HA config_entries/get returns a top-level array) — NOT collapsed to {}. Return type is
  `dict[str,object] | list[object] | HaError`. Regression risk: if `_result_payload` is ever narrowed
  back to dict-only, this collector silently emits ZERO entries. Test
  `test_send_command_success_list_result_returns_list` guards this.
- Real HA: ~189 config entries (under 500 cap). 015 #10 rule consumes `homelab_ha_config_entry_loaded
  == 0`. Verify via prod rig: collector ok=True, ~189 gauges/family, any setup_error==1 = broken
  integration, live in VM.

## STAGE-005-011 (Repairs collector — websocket)
- `HaRepairsCollector` emits `homelab_ha_repair_issue{domain,issue_id,severity}` (1.0 for each active, non-ignored repair issue) + always-emitted drop gauge `homelab_metric_family_dropped_series{family="homelab_ha_repair_issue"}` (0 when under cap).
- Repairs via websocket: per-tick `send_command("repairs/list_issues")` snapshot (NO subscription). Real HA returns a **dict** `{"issues": [...]}` (NOT a bare list) — defensive `_extract_issues` handles both shapes + empty. _ws None/not-connected/HaError → ok=False.
- Real HA shape verified (2026-06-12): `repairs/list_issues` response = dict with `issues` key; each issue has `domain`/`issue_id`/`severity` literal keys; `severity` enum = critical|error|warning (matches collector); `ignored` present (computed from `dismissed_version is not None`); `active` NOT serialized (HA pre-filters to active-only). Collector's `active is False` identity guard (emit when absent) is correct and tested.
- Label filter: `active is True and ignored is not True` → emit (skip-via-None); dismissed issues drop out via stale-series TTL.
- Real HA validation: 0 active repair issues (was 43 at card-authoring; issues clear over time). Collector run ok=True, metrics_emitted=1 (drop gauge only). Cardinality 0 → global 500 cap ample; no per-family override. No code changes during Refinement (wiring confirmed, no bugs found).

## STAGE-005-012 (Persistent-notifications collector — websocket)
- `HaPersistentNotificationCollector` emits `homelab_ha_persistent_notification{notification_id}` = 1.0 for each active HA persistent notification (the notification "bell") + always-emitted drop gauge `homelab_metric_family_dropped_series{family="homelab_ha_persistent_notification"}` (0 when under cap).
- Persistent notifications via websocket: per-tick `send_command("persistent_notification/get")` snapshot (NO subscription). Real HA returns a **bare list** (NOT a dict) — defensive `_extract_notifications` handles both bare-list and dict-wrapped `{"notifications":[...]}` shapes + empty. _ws None/not-connected/HaError → ok=False.
- Real HA shape verified (2026-06-12): `persistent_notification/get` response = bare list of notification dicts; each notification has `notification_id`/`title`/`message`/`created_at` literal keys. `notification_id` is a stable human-readable slug (observed: `http-login`, `invalid_config`), not a uuid. Real homelab: 2 active persistent notifications at test time.
- **PRIVACY regression (load-bearing, validated end-to-end against REAL data):** `notification_id` is the ONLY metric label. Title/message/created_at NEVER appear in metric labels, events, or logs. Test validation: created a sentinel notification with title `HMTEST-TITLE-SENTINEL` and body `HMTEST-BODY-SENTINEL-XYZ` on live HA; drove collector against real WS; ALL FOUR assertions PASSED: (a) gauge emitted for `notification_id="homelab_monitor_e2e_test"`=1.0 with ONLY the `notification_id` label key; (b) NO metric label value contained sentinel; (c) `events=[]` — no event payload carried sentinel; (d) monitor container logs contained ZERO sentinel occurrences. Dismissed test notification; HA left clean.
- Cardinality: notifications are tens at most; global 500 cap ample; no per-family override.
- Real HA validation: 3 active notifications at test time. Collector run ok=True, metrics_emitted=4 (3 notification gauges + 1 drop gauge), errors=[]. When 0 active notifications: collector run ok=True, metrics_emitted=1 (drop gauge only). No code changes during Refinement (wiring confirmed, collector handles real WS shape, privacy invariants validated).

## STAGE-005-013 (History/anomaly z-score collector)
- `HaAnomalyZscoreCollector` emits `homelab_ha_entity_value_zscore{entity_id}` = (current − rolling_mean) / rolling_std for eligible slow-moving numeric sensors (state_class=="measurement" AND device_class ∈ {temperature, humidity, pressure, power, carbon_dioxide, ...} AND parseable float) + always-emitted drop gauge `homelab_metric_family_dropped_series{family="homelab_ha_entity_value_zscore"}` (0 when under cap).
- REST collector (not websocket): per-tick (5m interval) calls `get_states()` snapshot via `ctx.ha` (`get_states_or_error`); maintains per-entity rolling window `deque(maxlen=window_samples=48)` as instance var (scheduler constructs once, persists state across ticks). Computes population std (`statistics.pstdev`) + emits z-score once `len(window) >= min_samples` (12; ~1h warmup at 5m). Zero-variance case (std < epsilon=1e-9): SKIP emission (no series, no NaN/inf) to avoid false "normal" signal on frozen sensors (DEFERRED to staleness rule 015 on `homelab_ha_entity_last_changed_seconds`).
- Config via subclassed `CollectorConfig`: `window_samples=48`, `min_samples=12`, `zero_variance_epsilon=1e-9`, `extra_entity_ids=[]` (force-include), `excluded_entity_ids=[]` (force-exclude). Env overrides: `HOMELAB_MONITOR_HA_ZSCORE_WINDOW_SAMPLES`, `HOMELAB_MONITOR_HA_ZSCORE_MIN_SAMPLES`, `HOMELAB_MONITOR_HA_ZSCORE_EPSILON`, `HOMELAB_MONITOR_HA_ZSCORE_DEVICE_CLASSES` (comma-sep list to replace default), `HOMELAB_MONITOR_HA_ZSCORE_EXCLUDED_ENTITY_IDS`, `HOMELAB_MONITOR_HA_ZSCORE_EXTRA_ENTITY_IDS`.
- Eligibility heuristic: select entities with state_class=="measurement" + device_class ∈ default set + parseable float; allow-list + deny-list overrides. Cap 500 (global default; no per-family override in `_DEFAULT_CARDINALITY_FAMILIES`). Emits `homelab_collector_run_*` self-metrics.
- 3a (dev rig, mock history): stable sensor → z≈0 ✓, spike → high |z| ✓, frozen sensor (zero-variance) → SKIP (no NaN/inf) ✓, insufficient-samples (< min_samples) → no emit ✓, UTC time + window math confirmed.
- 3b (prod rig, real HA sensors, load-bearing): real `get_states()` returned 2496 states; heuristic selected **118 eligible sensors** (24% of 500 cap — well within). Device-class breakdown: power(73), temperature(36), humidity(7), pressure(1), signal_strength(1) — heuristic confirmed sane against reality. Informational: 26 `battery`-class measurement sensors excluded by default (user can opt in via `HOMELAB_MONITOR_HA_ZSCORE_DEVICE_CLASSES`/`_EXTRA_ENTITY_IDS`). Real-poll loop of 14 live ticks: NO z-scores before tick 12 (warmup gate correct); after warmup, all 118 hit zero-variance SKIP because real sensors barely move within seconds (CORRECT — genuine z-scores emerge after ~1h of real 5m ticks). Controlled sequence on real eligible entity (`sensor.franklinwh_home_load`, dc=power): pre-warmup gate (0 z-scores < min_samples) ✓, jittered window → finite z ✓, spike (+10) → high |z| ✓, 14 identical values → zero-variance SKIP ✓, HaError → ok=False ✓, ctx.ha=None → ok=False ✓. Metric shape: family=`homelab_ha_entity_value_zscore`, entity_id the only label, all values finite, drop gauge present, metrics_emitted = survivors + 1.
- VERDICT: no bugs; collector works correctly against real HA sensors. No code changes during Refinement.
