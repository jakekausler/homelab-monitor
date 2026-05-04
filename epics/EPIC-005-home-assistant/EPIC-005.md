# EPIC-005: Home Assistant integration (collector + dispatcher channel)

## Status: Not Started

## Overview

First full integration epic. Land Home Assistant as a first-class plugin bundle: a collector that pulls signals from HA (entity availability, history-derived anomalies, batteries, automation/script failures, integration health), a dispatcher channel that pushes alerts back to HA so HA can run scenes / announce on speakers / fire automations, and a webhook endpoint that accepts arbitrary HA-fired events as a push source. The integration ships with a default Grafana dashboard and a default vmalert rule set.

This is the "exemplar" integration epic — its shape becomes the template for EPIC-006 (Pi-hole), EPIC-007 (Unifi), EPIC-008 (Synology).

## Source documents (read before starting any stage)

- Spec §2 Q10 (HA decisions: bidirectional integration, all listed pull signals, custom HA-fired webhooks accepted, push back to HA), §6.2 (HA-derived metrics from Recorder API or websocket), §8.1 (HAPushChannel — uses `notify.mobile_app_jake_s_android`).
- Project memory `reference_homelab_inventory.md`:
  - HA at `http://192.168.2.148:8123`
  - Long-lived bearer token authentication
  - Mobile push pattern via `/api/services/notify/mobile_app_jake_s_android`
  - HA runs in Docker on the same host with `network_mode: host`
- Existing pattern: `/storage/scripts/on-demand/claude_ready.sh` is the user's reference for the push payload shape.

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-005-001 | HA REST + websocket client wrapper; secret-store key for the long-lived token; smoke connectivity test |
| STAGE-005-002 | Entity-availability collector: poll `/api/states`, emit `homelab_ha_entity_available{entity_id, domain}` (1/0) and `homelab_ha_entity_last_changed_seconds`; default vmalert rule `EntityUnavailableForLong` |
| STAGE-005-003 | Battery-level collector: filter entities matching `*_battery_level` or unit `%` on battery devices; emit metrics; default rules at 20%/10% thresholds |
| STAGE-005-004 | History/anomaly collector: query Recorder API for slow-moving sensors (temps, humidities); compare to rolling baseline; emit `homelab_ha_entity_value_zscore` for anomaly rules |
| STAGE-005-005 | Automation + script run-status: subscribe to HA's `automation_triggered` and `automation_failed` events via websocket; record into `audit_log` and emit metrics; rule fires when failure rate > N/hour |
| STAGE-005-006 | Integration health: poll `/api/config/config_entries` (or similar) to detect disconnected integrations / restart loops |
| STAGE-005-007 | HAPushChannel: dispatcher channel implementation calling `/api/services/notify/mobile_app_jake_s_android`; routing rules updated so critical alerts route here by default |
| STAGE-005-008 | HA webhook ingester: `POST /api/integrations/ha/event` with API token scope `ha:event:write`; HA automations post here when something interesting happens; payload schema validated; flows into the alert ingestor or dedicated event log |
| STAGE-005-009 | "Push back to HA" event firer: when an alert fires, optionally fire a corresponding HA event (configured per alert via routing rules); HA can listen and run automations |
| STAGE-005-010 | HA integration UI panel ("Home Assistant" in Integrations sidebar): entity health grid, battery summary, recent automation failures, integration status |
| STAGE-005-011 | Default Grafana dashboard `home-assistant.json` + default vmalert rules `home-assistant.yaml` |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **HA token never logged.** Assert in tests.
- **Connection failures handled gracefully.** A 5xx from HA does not propagate as a 5xx in our API; collector marks itself failed and the failure budget logic from STAGE-001-008 takes over.
- **Cardinality budget.** HA has hundreds of entities. Each metric family must respect a configurable cap (e.g., `homelab_ha_entity_available` allows up to N=500 series); over-budget surfaces a suggestion.

## Dependencies

- EPIC-001 (kernel, dispatcher, alert ingestor, dashboard).
- EPIC-002 (heartbeat receiver — automation runs may produce heartbeats).
- The `concurrency_group="homeassistant"` is exercised heavily here.

## Notes

- Default polling interval for the entity-availability collector = 30s. Slower for history (5m). Configurable.
- The HA push channel handles offline phones gracefully (HA returns 200 even when the device hasn't received the push); we treat it as "delivered to HA, may not have reached the device".
- This epic is the first to add a per-integration UI panel; the panel rendering pattern (each integration registers a React component referenced by the Integrations sidebar) is established here and reused by EPICs 006/007/008/018.
