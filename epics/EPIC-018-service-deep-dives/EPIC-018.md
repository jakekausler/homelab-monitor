# EPIC-018: Per-service deep-dive integrations

## Status: Not Started

## Overview

Per spec §2 Q31, this is the dedicated epic for adding modular per-service integration plugins. The kernel, plugin framework, and the four exemplar integrations (HA, Pi-hole, Unifi, Synology) are already proven; this epic uses that foundation to add per-service intelligence for the rest of the homelab.

The list of services is open-ended and grows over time. Each service is a small plugin bundle: collector(s), discoverer (if applicable), default vmalert rules, default Grafana dashboard, optional UI panel. Each lands as its own stage; the epic itself is the umbrella.

## Source documents

- Spec §2 Q31 (dedicated epic for per-service integrations; architecture must support adding them as plugin bundles), §3.4 (discovered targets — partial list), §5 (plugin framework), §11 (`runbooks/` and integration bundle layout).
- Project memory `reference_docker_inventory.md` — the user's full container list. Many of these services need their own integration.

## Candidate stages (each is one service; not exhaustive)

| Likely stage | Service / theme |
|---|---|
| STAGE-018-001 | Mosquitto (MQTT broker) — broker uptime, connected clients, message rate, topic subscription health |
| STAGE-018-002 | Zigbee2MQTT — bridge state, paired-device count, last-seen per device, mesh quality |
| STAGE-018-003 | Z-wave JS UI — controller state, paired-device count, last-seen per device |
| STAGE-018-004 | Plex — server state, library scan status, recent transcode load, remote-access health |
| STAGE-018-005 | Frigate — when enabled: detector model status, per-camera fps, recording-disk health, NVR storage |
| STAGE-018-006 | Foundry VTT — server uptime, world status, active sessions |
| STAGE-018-007 | Music Assistant — server state, library health, source connectivity |
| STAGE-018-008 | Node-RED — flow status, recent error count, deployment timestamp |
| STAGE-018-009 | Grocy — uptime + stale-task count |
| STAGE-018-010 | Host-native MariaDB and MySQL — port probes, simple `SELECT 1` health checks via short-lived TCP, replication status if applicable, tablespace usage; uses dedicated read-only DB user with credentials in secrets |
| STAGE-018-011 | rtlamr2mqtt — process health, last-message-received age (cross-cuts EPIC-002 cron heartbeat for the existing watchdog) |
| STAGE-018-012 | ip-update — process health, last successful Route 53 update timestamp; cross-references EPIC-016's external-IP tracker for sanity |
| STAGE-018-013 | nginx-configuator integration: read `sites-config.yaml` (read-only) to enrich the targets table with public-domain mappings; "this internal target maps to public domain X" surfaced in inventory drill-downs |
| STAGE-018-014 | Reolink camera direct probes — beyond Surveillance Station's view: per-camera ICMP + RTSP keepalive |
| STAGE-018-015 | UPS (when present) — `nut_exporter` sidecar; battery %, runtime estimate, on-battery state |
| STAGE-018-016 | Surveillance Station deep-dive — per-camera recording rate, retention status, motion-detection event volume (some overlap with EPIC-008; this stage refines) |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Each service's plugin bundle follows the EPIC-005 template** — collector(s) + default rules + default Grafana dashboard + per-service UI panel, all in `apps/monitor/homelab_monitor/plugins/collectors/integrations/<service>/`.
- **No service in this epic is required for the public release to function.** Each is purely additive.
- **Disabled containers (compose `profiles: ["disabled"]`) are handled gracefully** — when disabled, the per-service integration becomes inert (no probes, no alerts) but the integration UI panel surfaces "this service is currently disabled — re-enable to monitor".

## Dependencies

- EPICs 001–017 should be in good shape before this epic — it leans on every kernel, framework, and observability piece.
- The user's compose file may evolve during this epic; the integration bundles must accommodate compose-level changes (e.g., a service that moves from a bind mount to a volume).

## Notes

- This epic is the longest by stage count and has the loosest sequencing — each stage is independent of the others and can be tackled in any order based on user priority. STAGE-018-001 is whatever the user starts with first.
- The order in the table above is a *suggested* priority based on how user-facing the service is (Mosquitto is foundational for HA; Zigbee + Z-wave + rtlamr feed HA; etc.). The user may reorder.
- "Auto-fix runbooks" for these services live in EPIC-009's runbook system; some services may earn their own runbook here if a clear failure-and-recovery pattern emerges during integration work.
