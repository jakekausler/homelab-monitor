# EPIC-005: Home Assistant integration (collector + dispatcher channel + bidirectional events + UI panel)

## Status: In Progress (3 / 26 Complete)

## Stages Counter: 3 / 26 Complete

## Current Stage: STAGE-005-004

## Current Phase: STAGE-005-004 Design (Not Started)

## Overview

EPIC-005 is the first full integration-bundle epic and the exemplar whose shape EPICs 006 (Pi-hole) / 007 (Unifi) / 008 (Synology) / 021 (other containers) copy. It lands Home Assistant as a first-class plugin bundle: a real HA client (REST + websocket), collectors for every HA health/issue signal, built-in vmalert rules + a user-customizable metric-threshold capability, a bidirectional dispatcher (HAPushChannel out + webhook ingester in + push-back event firer), and a per-integration UI panel that embeds the EPIC-004 `<LogViewer>`. HA at `http://192.168.2.148:8123`, long-lived bearer token, mobile push via `notify.mobile_app_jake_s_android`. The real vector log `service` label for the HA container is **`homeassistant`** (NOT `home-assistant`).

## Source documents (read before starting any stage)

- Master design spec §2 Q10 (HA decisions: bidirectional, all pull signals, custom HA-fired webhooks, push back to HA), §6.2 (HA-derived metrics), §8.1 (HAPushChannel notify), §5 (plugin/collector/integration_bundle framework + Channel/dispatcher contract).
- Project memory `reference_homelab_inventory.md` (HA URL/token/push pattern; HA in Docker on this host).
- `apps/ui/src/components/logs/README.md` (the LogViewer embedding contract, built by EPIC-004 STAGE-004-003 for this use case).
- Verified-this-session code anchors: `ctx.ha: HomeAssistantClient | None` is an empty scaffold in `apps/monitor/homelab_monitor/kernel/plugins/io.py`; `ctx_factory` passes `ha=None` in `kernel/api/lifespan.py`; `Channel.deliver(AlertEvent)` contract in `kernel/dispatch/types.py` with `InprocDashboardChannel` example; scoped endpoints via `require_user_or_token({Scope.X})` (`kernel/api/dependencies.py`, `Scope` enum in `kernel/auth/scopes.py`); Docker integration UI panel pattern (`apps/ui/src/routes/integrations/`, `/api/integrations/docker/*`, SidebarNav `NAV_ITEMS`, `router.tsx`); `useLogsQuery(expr,start,end,services)` in `apps/ui/src/api/logs.ts`; dashboards `deploy/grafana/dashboards/`, rules `deploy/vmalert/{metrics,logs}/`.

## Brainstormed architecture (2026-06-10)

### HA client = REST + websocket

- **REST for polled state:** `/api/states` (entity availability and state values), `/api/config` (HA version, latitude/longitude, timezone), `/api/error_log` (recent HA startup/integration errors).
- **Websocket for structured events:** `/api/websocket` with auth handshake, subscribe to config-entry / repairs / persistent-notification topics. REST cannot reach these signals (verified: `/api/config/config_entries` → 404, repairs → 404, notifications not in `/api/states`).

### Signal-source matrix (verified against the live install)

- **Entity availability / battery / updates / automation-run-cadence / history:** REST poll of `/api/states` + Recorder API.
- **Automation + script failures + integration-setup failures:** vmalert-LOGS rules over `service:"homeassistant"` (the HA docker stdout already collected by vector contains the full home-assistant.log including `Error while executing automation` / `Error executing script` / `Error ... setup of component` / `ConfigEntryNotReady` — no new mount needed).
- **Config-entry live state + repairs + persistent notifications:** Websocket (not in REST, not usefully in `.storage/` — `core.config_entries` file has only static config, `repairs.issue_registry` is 3-days-stale, notifications not persisted).

### Cardinality cap

- **Per-metric-family series cap (default 500)** lands in HA foundation; a reusable pattern inherited by EPIC-006 / 007 / 008 / 021.
- **MetricsWriter has no cap today;** collector emits safely within limits via the cap layer.

### User-customizable thresholds

- **EPIC-005 extends EPIC-004's user-authored-alert-rule machinery** (the `log_user_rules` / `CreateAlertModal` / `/logs/user-rules` / vmalert-dry-run surface) to author MetricsQL rules (a sibling `metric_user_rules` path).
- **Generic capability built in EPIC-005 foundation (STAGE-005-005), consumed by HA threshold presets + built-in safety-sensor defaults.**

### Routing

- **EPIC-005 adds HAPushChannel + minimal severity-based routing layer** (read `routing_rules` so only error/critical reach HA push).
- **Full rule-builder UI + per-tag overrides deferred to EPIC-012** (noted there).

## Stage decomposition (26 stages, sequential)

Stages MUST be implemented in order. No parallelization. Each stage lands a single small slice and ships independently usable.

### Wave A — Foundation (S01-S05)

| # | Stage | Theme |
|---|---|---|
| STAGE-005-001 | HA REST client + secret + lifespan wiring | ✓ Complete — Real HomeAssistantClient (REST: get_states/call_service/get_error_log/get_config) replacing the empty io.py scaffold; ha_token/ha_url secret+config; wire ctx.ha in lifespan.ctx_factory (currently ha=None); smoke test /api/config. |
| STAGE-005-002 | HA websocket client | ✓ Complete — Persistent /api/websocket client: auth handshake, subscribe, reconnect/backoff; foundation for config-entry/repairs/notifications/automation structured events. |
| STAGE-005-003 | integrations/ bundle skeleton + registration pattern | ✓ Complete — Create plugins/collectors/integrations/homeassistant/; establish the integration-bundle layout + collector registration pattern reused by 006/007/008/021. |
| STAGE-005-004 | Reusable cardinality cap | Per-metric-family series cap (default 500) in the metrics path + over-budget suggestion event; first consumed by entity-availability. |
| STAGE-005-005 | User-authored MetricsQL alert-rule machinery | Extend EPIC-004 user-rule surface (log_user_rules/CreateAlertModal/user-rules mgmt/vmalert dry-run) to a sibling metric_user_rules path so users author+customize numeric metric thresholds (generic; HA presets consume it in 005-016). |

### Wave B — Collectors (S06-S13)

| # | Stage | Theme |
|---|---|---|
| STAGE-005-006 | Entity-availability collector | Poll /api/states; homelab_ha_entity_available{entity_id,domain} 1/0 + homelab_ha_entity_last_changed_seconds. (scenario #1) |
| STAGE-005-007 | Battery-level collector | device_class=battery entities; battery metrics. (#5) |
| STAGE-005-008 | Update-availability collector | update.* entities (state=on=update available; installed/latest version labels). Covers Settings>Updates incl HA core/OS/supervisor/add-ons/HACS. (#13) |
| STAGE-005-009 | Automation/script run-cadence collector | automation.*/script.* last_triggered → run-cadence + last-triggered-age metrics. (#15 run side) |
| STAGE-005-010 | Config-entry state collector (websocket) | Authoritative integration loaded/setup_error/setup_retry state. (#10) |
| STAGE-005-011 | Repairs collector (websocket) | repairs/list_issues → active repair issues. (#11) |
| STAGE-005-012 | Persistent-notifications collector (websocket) | HA notification-bell events. (#12) |
| STAGE-005-013 | History/anomaly z-score collector | Recorder API for slow sensors; rolling-baseline z-score homelab_ha_entity_value_zscore. (#4) [heaviest; clean optional cut point] |

### Wave C — Alert rules (S14-S16)

| # | Stage | Theme |
|---|---|---|
| STAGE-005-014 | vmalert-LOGS rules | Automation/script failures + integration-setup failures + general HA error-rate, over service:"homeassistant". (#15 fail side, #10 log path, #20) |
| STAGE-005-015 | vmalert-METRICS rules | Entity-unavailable-for-long (#1), stale/frozen-entity per-class (#2), battery 20%/10% (#5), update-available roundup (#13), device-down rollup group-by-device (#7), homelab_ha_up reachability+rule (#18), anomaly z-score (#4), automation-didnt-run-when-expected (#16), automation-disabled-unexpectedly (#17). |
| STAGE-005-016 | Built-in safety-sensor rules + HA threshold presets | binary_sensor device_class smoke/gas/carbon_monoxide/moisture(water) firing → critical; door/window-left-open; + seed HA numeric-threshold presets into the 005-005 user-rule machinery. (#3a + #3b) |

### Wave D — Dispatcher channel + routing (S17-S18)

| # | Stage | Theme |
|---|---|---|
| STAGE-005-017 | HAPushChannel | Channel.deliver(AlertEvent) → POST notify/mobile_app_jake_s_android; reuses HA client. (#25) |
| STAGE-005-018 | Minimal severity routing | Read routing_rules so only error/critical → HA push. Full builder deferred to EPIC-012. |

### Wave E — Bidirectional webhook (S19-S20)

| # | Stage | Theme |
|---|---|---|
| STAGE-005-019 | HA webhook ingester | POST /api/integrations/ha/event + new Scope.HA_EVENT_WRITE; pydantic payload → audit_log. (#23) |
| STAGE-005-020 | Push-back-to-HA event firer | On alert fire, optional POST /api/events/<type> to HA (per-alert opt-in) so HA automations react. (#24) |

### Wave F — UI panel (S21-S26)

| # | Stage | Theme |
|---|---|---|
| STAGE-005-021 | Backend HA panel data endpoint(s) | GET /api/integrations/home-assistant/summary (+/entities) returning typed rows (mirrors Docker ContainerRow[]): entity-health counts, battery summary, update count, recent automation-failure count, integration-issue count, last-seen. |
| STAGE-005-022 | Panel shell + sidebar/router registration | "Home Assistant" NAV_ITEMS entry + /integrations/home-assistant route + page scaffold; establishes the per-integration-panel pattern. |
| STAGE-005-023 | Entity-health + battery widgets | Consume 021 data. |
| STAGE-005-024 | Updates + integration-status widgets | Updates-available list + config-entry/repairs/notifications summary. |
| STAGE-005-025 | Embedded LogViewer | Scoped service:"homeassistant" via the EPIC-004 embedding contract; recent automation failures inline. (#20 inline) |
| STAGE-005-026 | Grafana dashboard home-assistant.json | Default dashboard provisioned via deploy/grafana/dashboards/. |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **HA token never logged** — assert in tests.
- **Connection failures handled gracefully** — HA 5xx never propagates as our 5xx; collector marks failed; failure-budget takes over.
- **Cardinality budget respected** — configurable per-family cap; over-budget surfaces a suggestion.
- **All websocket subscriptions reconnect with backoff and never wedge the event loop.**
- **All plugins emit homelab_collector_run_* self-metrics.**
- **All internal timestamps UTC.**

## Out of Scope (explicitly considered and declined; routing for deferred items below)

1. **Full alert-routing rule-builder UI + per-tag overrides** — deferred to EPIC-012. `routing_rules` / `channels` table schema expansion + dispatcher filtering also deferred to EPIC-012.
2. **Numeric user-threshold machinery polish** — machinery built here (STAGE-005-005) but broad cross-integration UX polish iterated later.
3. **Per-service deep-dive of HA-adjacent containers** — matter-server beyond availability deferred to EPIC-018 where applicable.

## Dependencies

- EPIC-001 (kernel, collector framework, dispatcher, alert ingestor, scoped API tokens, secrets, dashboard).
- EPIC-002 (heartbeat — automation runs may emit heartbeats).
- EPIC-003 (Docker monitoring already covers the HA container restart/healthcheck/exit — HA process-down #18 leans on this + homelab_ha_up; #19 restart-loop is EPIC-003-covered).
- EPIC-004 (logs pipeline + LogViewer embedding contract + user-authored-rule machinery this epic extends; the HA docker-log stream service:"homeassistant").

## Notes

- **Real vector service label is `homeassistant`** (no hyphen) — use everywhere.
- **HA push channel handles offline phones gracefully** — HA returns 200; treat as delivered-to-HA.
- **This epic establishes the per-integration UI panel pattern** reused by 006 / 007 / 008 / 018 / 021.
- **concurrency_group="homeassistant" exercised heavily.**
- **Default poll intervals:** entity-availability 30s, history/anomaly 5m, websocket persistent.

## Brainstorming session record

The design was locked in this session (2026-06-10) based on the master design spec §2 Q10 and §5-6 decisions, verified against the live HA installation and the existing codebase scaffolds. Stage Design phases inherit these decisions; do not re-litigate.
