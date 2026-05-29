# EPIC-007: Unifi integration

## Status: Not Started

## Overview

Land Unifi as a plugin bundle. Three integration paths per spec Q11:

- **A (baseline):** `unifi-poller` / `unpoller` Prometheus exporter — drop-in container that scrapes the UDM and exposes everything we need as metrics. Easiest. Covers most signals.
- **C (baseline):** UDM syslog forwarded to our VL ingest. For events: DPI/IDS/IPS alerts, port-flapping, gateway/AP disconnects, firmware update events.
- **B (opt-in):** direct UDM API for state queries that the exporter doesn't expose well, and for any custom checks.

All listed Unifi signals are in scope per Q11: device offline (AP, switch, PDU port), new client, WAN status / ISP outage, DPI/IDS/IPS, bandwidth anomalies, PoE port draw anomalies, firmware updates.

## Source documents

- Spec §2 Q11 (Unifi decisions), §3.4 (discovered targets includes UDM + switches/APs/PDU).
- Project memory `reference_homelab_inventory.md`: UDM, USP PDU Pro, USW-48-PoE, 2× USW Flex, USW-Lite-16-PoE, 2× U7-Pro-Wall. Controller runs on the UDM itself.

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-007-001 | `unifi-poller` sidecar: pinned image, config templated from secrets (UDM URL + credentials); scrape config addition; smoke verification that metrics flow |
| STAGE-007-002 | UDM credentials in secrets store: dedicated read-only API user on the UDM (instructions for the user to create the account); rotation flow |
| STAGE-007-003 | Default vmalert rules for Unifi: `DeviceOffline` (per AP/switch/PDU port), `WanDown`, `BandwidthAnomaly`, `PoePortDrawAnomaly` |
| STAGE-007-004 | UDM syslog ingest: instructions for configuring UDM to forward syslog to our vector listener; vector source addition; new-client and DPI events parsed and emitted as alerts. **MUST include the following EPIC-004 follow-ups (brainstormed 2026-05-28):** (a) add UDM-specific bearer-token / API-token / session-token patterns to the redaction pipeline (`homelab-monitor.yaml` under `logs.redact:`) so syslog content is redacted at ingest before reaching VL; default v1 (EPIC-004 STAGE-004-006) ships only generic patterns. (b) Set `service` label on UDM syslog source (e.g., `service="udm-firewall"`, `service="udm-dpi"` per facility) so the per-`service` Drain models from EPIC-004 STAGE-004-025+ automatically partition. (c) Embed `<LogViewer>` (from EPIC-004 STAGE-004-003) on the per-device drill-down (STAGE-007-008) with a pre-filled `service:"udm-*"` filter — uses the documented embedding contract; drop-in, no custom log infrastructure. |
| STAGE-007-005 | New-client detection: when a MAC not in our `targets` table connects, emit a suggestion ("New device: 'iPhone-of-X' joined wifi — track?") |
| STAGE-007-006 | Direct UDM API client (B-mode) for custom checks: port-flapping detection, switch fan/temperature, PoE per-port stats not exposed by the exporter |
| STAGE-007-007 | Firmware update detector: poll UDM for available firmware updates; emit `homelab_unifi_firmware_update_available{device}`; rule fires at info severity |
| STAGE-007-008 | Unifi integration UI panel: topology view (UDM at center, switches, APs, PDU), per-device drill-down with metrics + connected clients, syslog feed pre-filtered by device. **MUST consume `<LogViewer>` from EPIC-004 STAGE-004-003 with a caller-provided `useLogs` hook scoped to the device's syslog stream** (e.g., `service:"udm-*" AND device_id:"<id>"`). Use the embedding contract documented in `apps/ui/src/components/logs/README.md`. Per the brainstorming session 2026-05-28, EPIC-004 explicitly designs the embedding contract for this use case. |
| STAGE-007-009 | Default Grafana dashboard `unifi.json` |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Concurrency group `unifi`** for everything that touches the UDM controller. Every collector that hits the UDM (poller, direct API) joins this group so we never DDoS the controller.
- **UDM credentials are read-only** — the integration setup docs require the user to create a dedicated read-only account; verify on connect that we lack write permissions (or document the risk if the user uses an admin account).
- **PSK fingerprints, full config dumps, and other ultra-sensitive UDM data** not surfaced in our UI even if the API would return them.

## Dependencies

- EPIC-001.
- EPIC-004 (logs pipeline) — UDM syslog parsing benefits from Drain signatures.

## Notes

- The user's UDM is the controller; some Unifi installations have a separate Network Application elsewhere. The collector config supports both topologies via a configurable controller URL.
- WAN-down detection from Unifi's perspective is the *primary* detector for ISP outages; EPIC-016 (ISP/WAN) adds independent corroboration via WAN reachability probes.
