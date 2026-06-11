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
