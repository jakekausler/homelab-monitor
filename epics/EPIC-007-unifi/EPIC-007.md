# EPIC-007: Unifi integration

## Status: Not Started

## Build order + client-identity ownership (IMPORTANT — added 2026-06-16)

**EPIC-007 (Unifi) is built BEFORE EPIC-006 (Pi-hole)** (epic numbers unchanged; only the build sequence
is swapped — decided during the 2026-06-16 Pi-hole brainstorm). Rationale: **Unifi is the authoritative
source of client identity** (reliable MAC ↔ current-IP ↔ stable device for every associated client).
Pi-hole can only reliably supply IP (its network table has MAC for ~19% of active clients — host-mode ARP
visibility is sparse), so Pi-hole must NOT own client-object creation.

**Therefore EPIC-007 OWNS client-object creation** (the canonical client/device model: MAC↔IP↔identity,
hostname, AP/switch-port, signal, bandwidth, online/offline, DHCP lease — the UDM is the DHCP server here),
and **EPIC-006 (Pi-hole, built second) OWNS the unified "Network → Clients" merged view** that joins
Pi-hole DNS behavior onto Unifi's client identities. EPIC-007 builds every Unifi-centric view it can on its
own; the cross-source merge lives in EPIC-006 (its STAGE-006-027, tentative — the exact seam may move here
and is finalized in the **Unifi brainstorm**, which has NOT run yet).

**Client-join contract (load-bearing — shared with EPIC-006):**
- Join is **time-windowed by IP-at-time-of-observation**, NEVER a static IP→device map (DHCP IPs rotate).
  Unifi is the stable identity anchor; Pi-hole's IP is the lookup key into Unifi's station table. Pi-hole-
  supplied MAC (the ~19%) is a corroborating secondary key only.
- **The host `192.168.2.148` is a FIRST-CLASS, top-tier device** in the unified view (it runs the monitor,
  Pi-hole, HA, Plex, Foundry, many host-mode containers — a major network actor). Pi-hole attributes its
  own loopback DNS (`127.0.0.1`/`::`/`pi.hole`) to this host; the unified view must treat it as a real
  device, never hide/exclude it.
- **Investigate (in the Unifi brainstorm) the DNS force/fallback rules** that make Pi-hole-down critical:
  the user's firewall has "Pi-Hole Redirect to DNS" (forces clients to Pi-hole :53), "Drop DNS to UDM"
  (blocks the UDM's own resolver at 192.168.2.1:53), one device bypassing Pi-hole, and a thin client-side
  DNS fallback list (Pi-hole → 1.1.1.1 → 8.8.8.8). The two port lists named "Pi-Hole DNS" and "DNS Port"
  (Port type, value 53) are part of this. EPIC-006 records these only to justify alert severity; EPIC-007
  owns understanding/monitoring them.

The **Unifi brainstorm is the next planning session** (same format as the Pi-hole one) and may amend any of
the above + the EPIC-006 decisions. Stage files for BOTH epics are created together after that brainstorm.

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
- **EPIC-006 (Pi-hole) is built AFTER this epic and CONSUMES its client identities** (see the Build-order
  banner). EPIC-006 owns the unified Network → Clients merged view (joins Pi-hole DNS behavior onto the
  client identities this epic creates). DHCP-leases monitoring is THIS epic's (the UDM is the DHCP server,
  not Pi-hole).

## Notes

- The user's UDM is the controller; some Unifi installations have a separate Network Application elsewhere. The collector config supports both topologies via a configurable controller URL.
- WAN-down detection from Unifi's perspective is the *primary* detector for ISP outages; EPIC-016 (ISP/WAN) adds independent corroboration via WAN reachability probes.
- **DHCP leases + per-device DNS-force/fallback rules + the unified client model land here** (added
  2026-06-16). A future stage (decided in the Unifi brainstorm) builds: client-object creation (the
  canonical MAC↔IP↔device model EPIC-006 joins onto), DHCP-lease monitoring, and investigation of the
  firewall DNS-force rules. The unified Network → Clients merged VIEW is EPIC-006's (STAGE-006-027,
  tentative) — but its seam with this epic is finalized in the Unifi brainstorm and may move here.
- **This epic's stage list above is the PRE-brainstorm sketch.** The Unifi brainstorm (next planning
  session) will re-decompose it EPIC-005-style (fine, session-sized stages) the same way the Pi-hole
  brainstorm re-decomposed EPIC-006 from its 6-stage sketch into 27 stages. Do NOT take the 9-stage sketch
  above at face value when that brainstorm runs.
